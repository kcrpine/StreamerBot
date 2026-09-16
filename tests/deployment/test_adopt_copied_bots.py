"""Adopting a bot folder that was copied into bots/ by hand.

Backup and Restore is the supported route, but people copy a folder straight in
over scp or a file manager because it is the obvious thing to do when the folder
is right there. Nothing noticed: every menu item works from
"docker ps -a -f label=role=streamerbot", so a folder with no container was
absent from all of them, with no error anywhere, because no code ever ran for
it.

These run the real shell functions rather than reading the script for a line,
because what matters is what the code does to a folder. The one exception is the
ordering test, which is about where a call sits relative to another.

Host-only: .dockerignore keeps streamerbot.sh out of the runtime image on
purpose, so these skip there rather than failing on a file absent by design.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]

# Everything adopt_copied_bots leans on. migrate_one_bot and its helpers are
# pulled in whole rather than stubbed: the promise being tested is that a copied
# folder goes through the same migration a restored one does, and a stub would
# pass while that stopped being true.
ADOPT_FUNCTIONS = [
    "bot_name_is_valid",
    "adoptable_bot_dirs",
    "find_adoptable_config",
    "lift_adopted_bot_data",
    "inspect_adoption_candidate",
    "bot_dir_is_legacy",
    "bot_identity_fingerprint",
    "streamerbot_config_defaults",
    "bot_claimed_ports",
    "port_claimed_by_another_bot",
    "port_free_for_new_bot",
    "next_free_port",
    "disable_portal_for_port_conflict",
    "reenable_portal_after_repair",
    "assign_unique_bot_ports",
    "migrate_one_bot",
]


def require_tools():
    for tool in ("bash", "jq"):
        if not shutil.which(tool):
            raise unittest.SkipTest(f"{tool} is not available on this host")


def read_script():
    path = ROOT / "streamerbot.sh"
    if not path.is_file():
        raise unittest.SkipTest("streamerbot.sh is not present; host-only test skipped")
    return path.read_text(encoding="utf-8", errors="replace")


def extract_function(script, name):
    """Pull one shell function out of streamerbot.sh.

    The script runs its menu at top level, so it cannot simply be sourced. Style
    is consistent enough that a closing brace in column zero ends a function.
    """
    opening = f"{name}() {{"
    start = script.find(f"\n{opening}")
    if start == -1:
        raise unittest.SkipTest(f"{name} is not in streamerbot.sh")
    lines = script[start + 1:].split("\n")
    body = []
    for line in lines:
        body.append(line)
        if line == "}":
            return "\n".join(body)
    raise unittest.SkipTest(f"{name} has no closing brace in column zero")


def extract_assignment(script, name):
    start = script.find(f"\n{name}=")
    if start == -1:
        raise unittest.SkipTest(f"{name} is not in streamerbot.sh")
    return script[start + 1:].split("\n")[0]


class AdoptHarness(TestCase):
    def setUp(self):
        require_tools()
        self.script = read_script()
        self.tmp = Path(tempfile.mkdtemp())
        self.bots = self.tmp / "bots"
        self.bots.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_shell(self, body, *, containers=""):
        """Run shell against a fake docker.

        `containers` is what "docker ps -a -q" should print, so a test can say
        which bots already have one. Nothing here talks to a real daemon.
        """
        preamble = [
            "set -u",
            f'BOTS_ROOT="{self.bots.as_posix()}"',
            "DEFAULT_PORTAL_PORT=4419",
            "DEFAULT_LIBRESPOT_API_PORT=3678",
            "DEFAULT_STREAM_PROXY_PORT=4420",
            extract_assignment(self.script, "BOT_IDENTITY_FIELDS"),
            extract_assignment(self.script, "BOT_NAME_PATTERN"),
            # Named containers, matched the way the real filter does.
            f'EXISTING_CONTAINERS="{containers}"',
            'docker() {',
            '    local arg name',
            '    for arg in "$@"; do',
            '        case "$arg" in',
            '            name=^/*) name="${arg#name=^/}"; name="${name%$}" ;;',
            '        esac',
            '    done',
            '    [ -n "${name:-}" ] || return 0',
            '    for existing in $EXISTING_CONTAINERS; do',
            '        [ "$existing" = "$name" ] && { echo "id-$name"; return 0; }',
            '    done',
            '    return 0',
            '}',
            # The real ones write to the manager log and need root.
            "log_line() { :; }",
            "chown() { :; }",
        ]
        for name in ADOPT_FUNCTIONS:
            preamble.append(extract_function(self.script, name))
        preamble.append(body)
        return subprocess.run(
            ["bash", "-c", "\n".join(preamble)],
            capture_output=True, text=True, timeout=120,
        )

    def make_folder(self, name, config=None, files=(), subdir=None):
        """A folder as it arrives: no container, whatever files were copied."""
        directory = self.bots / name
        target = directory / subdir if subdir else directory
        target.mkdir(parents=True, exist_ok=True)
        if config is not None:
            (target / "config.json").write_text(
                json.dumps(config) if isinstance(config, dict) else config,
                encoding="utf-8",
            )
        for filename in files:
            (target / filename).write_text("x", encoding="utf-8")
        return directory

    def config_of(self, name):
        return json.loads(
            (self.bots / name / "config.json").read_text(encoding="utf-8")
        )


# A config as the old TTMediaBot wrote one: its own cache and log names, its own
# service keys, and a default_service this bot has never had.
TTMEDIABOT_CONFIG = {
    "config_version": 2,
    "general": {"cache_file_name": "TTMediaBotCache.dat"},
    "logger": {"file_name": "TTMediaBot.log"},
    "services": {"default_service": "vk", "vk": {"enabled": True}},
    "teamtalk": {
        "hostname": "tt.example.org",
        "tcp_port": 10333,
        "udp_port": 10333,
        "nickname": "OldBot",
        "username": "olduser",
        "password": "oldpass",
        "channel": "/music",
        "channel_password": "chanpass",
    },
}


class ScanFindsOnlyFoldersWithNoContainer(AdoptHarness):
    """The scan is the feature. A folder that already has a container is a
    running bot, and adopting it would destroy and rebuild it."""

    def test_a_copied_folder_is_found(self):
        self.make_folder("copied", TTMEDIABOT_CONFIG)

        result = self.run_shell("adoptable_bot_dirs")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("copied", result.stdout)

    def test_a_folder_that_already_has_a_container_is_left_alone(self):
        self.make_folder("running", TTMEDIABOT_CONFIG)

        result = self.run_shell("adoptable_bot_dirs", containers="running")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("running", result.stdout)

    def test_a_stopped_bot_is_not_a_candidate(self):
        """docker ps -a lists stopped containers too. Treating a deliberately
        stopped bot as an orphan would rebuild it behind the user's back."""
        self.make_folder("stopped", TTMEDIABOT_CONFIG)

        result = self.run_shell("adoptable_bot_dirs", containers="stopped")

        self.assertNotIn("stopped", result.stdout)

    def test_the_two_are_told_apart_in_one_pass(self):
        self.make_folder("copied", TTMEDIABOT_CONFIG)
        self.make_folder("running", TTMEDIABOT_CONFIG)

        result = self.run_shell("adoptable_bot_dirs", containers="running")

        self.assertIn("copied", result.stdout)
        self.assertNotIn("running", result.stdout)


class NamesThatCannotBeBotsAreRefused(AdoptHarness):
    """The folder name becomes the container name and the bot_id. The bot_id
    regex is the containment boundary that stops one bot reaching another bot's
    YouTube session, so a folder name must meet it rather than it being relaxed
    to accept a folder name."""

    def test_a_name_with_a_space_is_refused(self):
        result = self.run_shell('bot_name_is_valid "My Bot" && echo yes || echo no')
        self.assertIn("no", result.stdout)

    def test_a_name_starting_with_a_dash_is_refused(self):
        """Also a docker argument-injection shape, quite apart from bot_id."""
        result = self.run_shell('bot_name_is_valid "-rm" && echo yes || echo no')
        self.assertIn("no", result.stdout)

    def test_a_path_separator_is_refused(self):
        """bot_id is joined under BOTS_ROOT by the bridge."""
        for name in ("../escape", "a/b"):
            with self.subTest(name=name):
                result = self.run_shell(
                    f'bot_name_is_valid "{name}" && echo yes || echo no'
                )
                self.assertIn("no", result.stdout)

    def test_ordinary_names_are_accepted(self):
        for name in ("mybot", "my-bot", "my_bot", "my.bot", "bot2"):
            with self.subTest(name=name):
                result = self.run_shell(
                    f'bot_name_is_valid "{name}" && echo yes || echo no'
                )
                self.assertIn("yes", result.stdout)

    def test_the_scan_refuses_such_a_folder_and_says_why(self):
        self.make_folder("My Bot", TTMEDIABOT_CONFIG)

        result = self.run_shell(
            'inspect_adoption_candidate "$BOTS_ROOT/My Bot" && echo ADOPTABLE'
        )

        self.assertNotIn("ADOPTABLE", result.stdout)
        self.assertIn("Error.", result.stdout)
        # Names the actual cause rather than only the rule.
        self.assertIn("Spaces", result.stdout)


class FoldersThatCannotBeAdopted(AdoptHarness):
    def test_a_folder_with_no_config_is_refused(self):
        self.make_folder("empty", None, files=("notes.txt",))

        result = self.run_shell(
            'inspect_adoption_candidate "$BOTS_ROOT/empty" && echo ADOPTABLE'
        )

        self.assertNotIn("ADOPTABLE", result.stdout)
        self.assertIn("no config.json", result.stdout)

    def test_a_config_that_is_not_json_is_refused_rather_than_migrated(self):
        """jq would fail mid-migration and leave the folder half done."""
        self.make_folder("broken", "{ not json")

        result = self.run_shell(
            'inspect_adoption_candidate "$BOTS_ROOT/broken" && echo ADOPTABLE'
        )

        self.assertNotIn("ADOPTABLE", result.stdout)
        self.assertIn("not valid JSON", result.stdout)

    def test_several_configs_are_refused_rather_than_guessed_between(self):
        directory = self.make_folder("install", TTMEDIABOT_CONFIG, subdir="botA")
        (directory / "botB").mkdir()
        (directory / "botB" / "config.json").write_text(
            json.dumps(TTMEDIABOT_CONFIG), encoding="utf-8"
        )

        result = self.run_shell(
            'inspect_adoption_candidate "$BOTS_ROOT/install" && echo ADOPTABLE'
        )

        self.assertNotIn("ADOPTABLE", result.stdout)
        self.assertIn("Error.", result.stdout)


class ReportingBeforeAnythingChanges(AdoptHarness):
    """The scan reports, and the report is read aloud before a decision. It must
    not change the folder it is describing."""

    def test_a_copied_folder_is_reported_as_adoptable(self):
        self.make_folder("copied", TTMEDIABOT_CONFIG)

        result = self.run_shell(
            'inspect_adoption_candidate "$BOTS_ROOT/copied" && echo ADOPTABLE'
        )

        self.assertIn("ADOPTABLE", result.stdout)

    def test_it_names_the_nickname_and_server_it_found(self):
        """What identifies the bot, so the user can tell which one this is."""
        self.make_folder("copied", TTMEDIABOT_CONFIG)

        result = self.run_shell('inspect_adoption_candidate "$BOTS_ROOT/copied"')

        self.assertIn("OldBot", result.stdout)
        self.assertIn("tt.example.org", result.stdout)

    def test_it_says_an_old_configuration_will_be_brought_up_to_date(self):
        self.make_folder("copied", TTMEDIABOT_CONFIG)

        result = self.run_shell('inspect_adoption_candidate "$BOTS_ROOT/copied"')

        self.assertIn("older version", result.stdout)

    def test_scanning_writes_nothing(self):
        self.make_folder("copied", TTMEDIABOT_CONFIG)
        before = (self.bots / "copied" / "config.json").read_bytes()

        self.run_shell('inspect_adoption_candidate "$BOTS_ROOT/copied"')

        self.assertEqual(
            (self.bots / "copied" / "config.json").read_bytes(), before
        )
        self.assertFalse((self.bots / "copied" / "secrets").exists())


class ForeignLineageIsRecognised(AdoptHarness):
    """Version numbers are only comparable within one lineage, so lineage is
    decided by shape. A TTMediaBot config claiming version 2 used to sail
    through untouched, keeping a default_service ServiceManager dies on."""

    def test_a_ttmediabot_config_claiming_the_current_version_is_still_legacy(self):
        self.make_folder("copied", TTMEDIABOT_CONFIG)

        result = self.run_shell(
            'bot_dir_is_legacy "$BOTS_ROOT/copied" && echo LEGACY || echo CURRENT'
        )

        self.assertIn("LEGACY", result.stdout)

    def test_a_foreign_service_key_alone_is_enough(self):
        """No TTMediaBot in any filename, only the service keys."""
        config = {
            "config_version": 2,
            "general": {"cache_file_name": "StreamerBotCache.dat"},
            "logger": {"file_name": "StreamerBot.log"},
            "auth_portal": {"enabled": True, "port": 4419},
            "services": {"default_service": "yam", "yam": {"enabled": True}},
            "teamtalk": {"nickname": "Bot", "hostname": "tt.example.org"},
        }
        self.make_folder("copied", config)

        result = self.run_shell(
            'bot_dir_is_legacy "$BOTS_ROOT/copied" && echo LEGACY || echo CURRENT'
        )

        self.assertIn("LEGACY", result.stdout)

    def test_a_current_config_is_not_called_legacy(self):
        config = {
            "config_version": 2,
            "general": {"cache_file_name": "StreamerBotCache.dat"},
            "logger": {"file_name": "StreamerBot.log"},
            "auth_portal": {"enabled": True, "port": 4419},
            "services": {"default_service": "yt"},
            "teamtalk": {"nickname": "Bot", "hostname": "tt.example.org"},
        }
        self.make_folder("current", config)

        result = self.run_shell(
            'bot_dir_is_legacy "$BOTS_ROOT/current" && echo LEGACY || echo CURRENT'
        )

        self.assertIn("CURRENT", result.stdout)


class AdoptingUpgradesTheConfiguration(AdoptHarness):
    """A copied folder goes through the same migration a restored one does."""

    def setUp(self):
        super().setUp()
        self.make_folder("copied", TTMEDIABOT_CONFIG,
                         files=("TTMediaBotCache.dat", "TTMediaBot.log"))
        result = self.run_shell('migrate_one_bot "$BOTS_ROOT/copied"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.migrated = self.config_of("copied")

    def test_the_identity_is_untouched(self):
        """The whole point. A bot that comes back as somebody else is worse
        than one that does not come back."""
        self.assertEqual(self.migrated["teamtalk"], TTMEDIABOT_CONFIG["teamtalk"])

    def test_the_new_sections_are_added(self):
        for section in ("auth_portal", "audio_description", "sound_devices"):
            with self.subTest(section=section):
                self.assertIn(section, self.migrated)

    def test_the_new_services_are_added(self):
        for service in ("sp", "nf", "dp", "am", "az"):
            with self.subTest(service=service):
                self.assertIn(service, self.migrated["services"])

    def test_a_default_service_this_bot_does_not_have_is_replaced(self):
        """ServiceManager looks this up in a plain dict, so "vk" is a KeyError
        during startup -- a traceback rather than anything a user can act on."""
        self.assertEqual(self.migrated["services"]["default_service"], "yt")

    def test_the_cache_and_log_names_are_renamed(self):
        self.assertEqual(
            self.migrated["general"]["cache_file_name"], "StreamerBotCache.dat"
        )
        self.assertEqual(self.migrated["logger"]["file_name"], "StreamerBot.log")

    def test_the_cache_file_is_carried_across_rather_than_lost(self):
        """Renamed, not deleted: it holds the bot's favourites."""
        self.assertTrue((self.bots / "copied" / "StreamerBotCache.dat").is_file())
        self.assertFalse((self.bots / "copied" / "TTMediaBotCache.dat").exists())

    def test_the_original_is_kept(self):
        self.assertTrue(
            (self.bots / "copied" / "config.json.pre-migration").is_file()
        )

    def test_the_credential_directories_are_created(self):
        for sub in ("secrets", "browser", "youtube_auth", "librespot"):
            with self.subTest(sub=sub):
                self.assertTrue((self.bots / "copied" / sub).is_dir())

    def test_the_version_is_current(self):
        self.assertEqual(self.migrated["config_version"], 2)


class AdoptedBotsGetTheirOwnPorts(AdoptHarness):
    """Containers run with --network host, so a copied folder arriving with the
    ports it had elsewhere takes them from whichever bot already has them."""

    def test_a_copied_folder_is_moved_off_a_port_another_bot_holds(self):
        existing = {
            "config_version": 2,
            "auth_portal": {"enabled": True, "port": 4419, "host": "127.0.0.1"},
            "services": {"default_service": "yt", "sp": {"api_port": 3678}},
            "player": {"stream_proxy_port": 4420},
            "teamtalk": {"nickname": "First", "hostname": "tt.example.org"},
        }
        self.make_folder("first", existing)
        self.make_folder("copied", dict(TTMEDIABOT_CONFIG,
                                        auth_portal={"enabled": True, "port": 4419,
                                                     "host": "127.0.0.1"}))

        result = self.run_shell('migrate_one_bot "$BOTS_ROOT/copied"')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config_of("first")["auth_portal"]["port"], 4419)
        self.assertNotEqual(self.config_of("copied")["auth_portal"]["port"], 4419)

    def test_all_three_listening_ports_are_made_unique(self):
        self.make_folder("first", {
            "config_version": 2,
            "auth_portal": {"enabled": True, "port": 4419, "host": "127.0.0.1"},
            "services": {"default_service": "yt", "sp": {"api_port": 3678}},
            "player": {"stream_proxy_port": 4420},
            "teamtalk": {"nickname": "First", "hostname": "tt.example.org"},
        })
        self.make_folder("copied", TTMEDIABOT_CONFIG)

        self.run_shell('migrate_one_bot "$BOTS_ROOT/copied"')

        copied = self.config_of("copied")
        self.assertNotEqual(copied["auth_portal"]["port"], 4419)
        self.assertNotEqual(copied["services"]["sp"]["api_port"], 3678)
        self.assertNotEqual(copied["player"]["stream_proxy_port"], 4420)


class WholeInstallationsCopiedIn(AdoptHarness):
    """People copy the entire old install directory, not just the bot's data
    folder, and then config.json is down among the source code."""

    def test_a_nested_config_is_found_and_reported_rather_than_refused(self):
        self.make_folder("install", TTMEDIABOT_CONFIG, subdir="TTMediaBot")

        result = self.run_shell(
            'inspect_adoption_candidate "$BOTS_ROOT/install" && echo ADOPTABLE'
        )

        self.assertIn("ADOPTABLE", result.stdout)
        self.assertIn("TTMediaBot/config.json", result.stdout)

    def test_only_the_bot_files_are_lifted_not_the_source_tree(self):
        """Hoisting everything would mount a second copy of the bot's own source
        over the container's data directory."""
        directory = self.make_folder(
            "install", TTMEDIABOT_CONFIG, subdir="TTMediaBot",
            files=("TTMediaBotCache.dat",),
        )
        nested = directory / "TTMediaBot"
        (nested / "bot").mkdir()
        (nested / "bot" / "__init__.py").write_text("x", encoding="utf-8")
        (nested / "requirements.txt").write_text("x", encoding="utf-8")

        result = self.run_shell(
            'lift_adopted_bot_data "$BOTS_ROOT/install/TTMediaBot" "$BOTS_ROOT/install"'
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((directory / "config.json").is_file())
        self.assertTrue((directory / "TTMediaBotCache.dat").is_file())
        self.assertFalse((directory / "bot").exists())
        self.assertFalse((directory / "requirements.txt").exists())

    def test_it_never_overwrites_a_file_already_at_the_top(self):
        directory = self.make_folder("install", TTMEDIABOT_CONFIG, subdir="TTMediaBot")
        (directory / "config.json").write_text('{"kept": true}', encoding="utf-8")

        self.run_shell(
            'lift_adopted_bot_data "$BOTS_ROOT/install/TTMediaBot" "$BOTS_ROOT/install"'
        )

        self.assertEqual(
            json.loads((directory / "config.json").read_text(encoding="utf-8")),
            {"kept": True},
        )


class TheFlowIsWiredUp(TestCase):
    """Ordering and wiring, which are about where a call sits rather than what
    it does to a folder."""

    def setUp(self):
        self.script = read_script()

    def test_the_menu_offers_it(self):
        manage = self.script[self.script.index("manage_bots() {"):]
        manage = manage[: manage.index("\n}\n")]
        self.assertIn("Adopt Bot Folders Copied Into bots/", manage)
        self.assertIn("adopt_copied_bots", manage)

    def test_every_manage_menu_number_is_reachable(self):
        """Adding an item above Return renumbers it, and a case arm left on the
        old number makes Return silently reprint the menu instead."""
        manage = self.script[self.script.index("manage_bots() {"):]
        manage = manage[: manage.index("\n}\n")]
        listed = set(
            int(number)
            for number in re.findall(r'^\s*echo "(\d+)\.\s', manage, re.MULTILINE)
        )
        handled = set(
            int(number)
            for number in re.findall(r"^\s*(\d+)\)\s*$", manage, re.MULTILINE)
        )
        self.assertTrue(listed, "no numbered menu items were found")
        self.assertEqual(listed, handled)

    def test_the_configuration_is_updated_before_a_container_is_created(self):
        """A container built from an unmigrated config starts a bot whose new
        settings exist only as in-memory defaults."""
        adopt = self.script[self.script.index("adopt_copied_bots() {"):]
        adopt = adopt[: adopt.index("\n}\n")]
        self.assertLess(adopt.index("migrate_one_bot"), adopt.index("docker create"))

    def test_the_configuration_is_checked_before_a_container_is_created(self):
        """A bot with a broken config restarts in a loop, which reads as the
        adoption having gone wrong rather than the config it arrived with."""
        adopt = self.script[self.script.index("adopt_copied_bots() {"):]
        adopt = adopt[: adopt.index("\n}\n")]
        self.assertLess(
            adopt.index("validate_one_bot_config"), adopt.index("docker create")
        )

    def test_nothing_is_changed_without_being_agreed_to(self):
        adopt = self.script[self.script.index("adopt_copied_bots() {"):]
        adopt = adopt[: adopt.index("\n}\n")]
        confirm_at = adopt.index('read -p "Type adopt to continue')
        self.assertLess(confirm_at, adopt.index("migrate_one_bot"))
        self.assertLess(confirm_at, adopt.index("lift_adopted_bot_data"))

    def test_the_container_gets_the_same_flags_every_other_bot_gets(self):
        """A bot created here and one created by Create Bot must be the same
        kind of container, or it drops out of every menu that filters on the
        label and loses its isolation."""
        adopt = self.script[self.script.index("adopt_copied_bots() {"):]
        adopt = adopt[: adopt.index("\n}\n")]
        for flag in (
            "--network host",
            'label "role=streamerbot"',
            "--restart always",
            "TTBOT_INSTANCE=",
            "YOUTUBE_BRIDGE_URL=",
            "/home/streamer/StreamerBot/data",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, adopt)

    def test_the_config_check_mounts_read_only(self):
        """A running bot holds a lock on its own config.json, and a check must
        never write to what it is inspecting."""
        validate = self.script[self.script.index("validate_one_bot_config() {"):]
        validate = validate[: validate.index("\n}\n")]
        self.assertIn(":ro", validate)

    def test_the_config_check_mounts_the_bot_under_its_own_name(self):
        """check_config.py takes the name it reports from the config file's
        parent directory, so a fixed mount point reports on a bot named after
        the mount -- useless in a run adopting several folders at once."""
        validate = self.script[self.script.index("validate_one_bot_config() {"):]
        validate = validate[: validate.index("\n}\n")]
        self.assertIn('/bots/${name}:ro', validate)
        self.assertIn('/bots/${name}/config.json', validate)

    def test_a_copied_folder_is_mentioned_at_startup(self):
        """The failure was invisible precisely because nothing said anything."""
        self.assertIn("notice_adoptable_bots", self.script)
        tail = self.script[self.script.index("# Main Menu"):]
        self.assertIn("notice_adoptable_bots", tail)

    def test_the_startup_notice_only_reports(self):
        """Like the update check: adopting starts bots, which is not something
        to do to somebody on the way to the menu."""
        notice = self.script[self.script.index("notice_adoptable_bots() {"):]
        notice = notice[: notice.index("\n}\n")]
        self.assertNotIn("docker create", notice)
        self.assertNotIn("migrate_one_bot", notice)
        self.assertNotIn("adopt_copied_bots", notice)


if __name__ == "__main__":
    unittest.main()
