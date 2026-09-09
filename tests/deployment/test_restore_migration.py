"""The restore-path migration for backups from the old TTMediaBot.

These are host-only: they read the shell scripts and the config templates, both
of which .dockerignore keeps out of the runtime image.

What is pinned here is the promise that matters. A restore must never change what
identifies a bot to a TeamTalk server, and must never lose the startup commands
that make a bot play a stream the moment it connects. Getting either wrong turns
a restore into a bot that looks like someone else's, and no amount of new
features compensates for that.
"""

import json
import re
import unittest
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]


def read_script(name):
    path = ROOT / name
    if not path.is_file():
        raise unittest.SkipTest(f"{name} is not present; host-only test skipped")
    return path.read_text(encoding="utf-8", errors="replace")


def read_config(name):
    path = ROOT / name
    if not path.is_file():
        raise unittest.SkipTest(f"{name} is not present; host-only test skipped")
    return json.loads(path.read_text(encoding="utf-8"))


class MigrationRunsBeforeContainersTests(TestCase):
    """Ordering is the whole point: a container built from an unmigrated config
    starts with the new settings existing only as in-memory defaults."""

    def setUp(self):
        self.script = read_script("streamerbot.sh")

    def test_the_migration_function_exists(self):
        self.assertIn("migrate_restored_bots()", self.script)

    def test_restore_calls_it_before_creating_anything(self):
        restore = self.script[self.script.index("restore_bots() {"):]
        migrate_at = restore.index("migrate_restored_bots")
        recreate_at = restore.index("recreate_bot_containers")
        create_service_at = restore.index("create_shared_youtube_service")

        self.assertLess(migrate_at, recreate_at)
        self.assertLess(migrate_at, create_service_at)

    def test_it_refuses_rather_than_guessing_when_jq_is_missing(self):
        migrate = self.script[self.script.index("migrate_restored_bots() {"):]
        self.assertIn("command -v jq", migrate)


class IdentityIsPreservedTests(TestCase):
    def setUp(self):
        self.script = read_script("streamerbot.sh")

    def test_every_identifying_field_is_fingerprinted(self):
        """These are what make a bot the same bot to the people on that server."""
        fields = self.script[self.script.index("BOT_IDENTITY_FIELDS="):]
        fields = fields[: fields.index("\n")]

        for field in (
            "nickname", "username", "password", "status",
            "channel", "channel_password", "hostname", "tcp_port",
        ):
            self.assertIn(field, fields, field)

    def test_the_migration_aborts_if_identity_would_change(self):
        migrate = self.script[self.script.index("migrate_one_bot() {"):]

        self.assertIn('[ "$before" != "$after" ]', migrate)
        # And on mismatch it keeps the original rather than writing anyway.
        self.assertIn("kept", migrate)

    def test_the_merge_lets_existing_values_win(self):
        """"$d * ." keeps the bot's own values; ". * $d" would overwrite them
        with defaults, silently resetting a nickname."""
        migrate = self.script[self.script.index("migrate_one_bot() {"):]

        self.assertIn("($d * .)", migrate)
        self.assertNotIn("(. * $d)", migrate)

    def test_nothing_in_the_migration_writes_to_the_teamtalk_section(self):
        migrate = self.script[
            self.script.index("migrate_one_bot() {"):self.script.index("migrate_restored_bots() {")
        ]
        # Assignments look like ".teamtalk.x =" in the jq program.
        self.assertNotRegex(migrate, r"\.teamtalk\.[a-z_]+\s*=")

    def test_the_original_config_is_kept_as_a_way_back(self):
        migrate = self.script[self.script.index("migrate_one_bot() {"):]

        self.assertIn("config.json.pre-migration", migrate)

    def test_nothing_is_deleted_except_the_obsolete_cookie_file(self):
        """Config and data are never destroyed. The old cookies.txt is the one
        deliberate exception: nothing has read it since sign-in moved to device
        codes, and leaving it behind means a stale YouTube session sitting in
        plaintext in a directory that gets tarred into every backup."""
        migrate = self.script[
            self.script.index("migrate_one_bot() {"):self.script.index("migrate_restored_bots() {")
        ]
        self.assertNotIn("rm -rf", migrate)
        self.assertRegex(migrate, r"rm -f \"\$dir/cookies\.txt\"")

    def test_the_config_is_never_deleted(self):
        migrate = self.script[
            self.script.index("migrate_one_bot() {"):self.script.index("migrate_restored_bots() {")
        ]

        self.assertNotRegex(migrate, r"rm\s+(-\w+\s+)*\"?\$dir/config\.json")


class StartCommandsSurviveTests(TestCase):
    """start_commands is how a bot plays a stream the moment it connects. It is
    the setting most likely to be noticed missing and least likely to be
    noticed in a diff."""

    def test_the_migration_never_touches_it(self):
        script = read_script("streamerbot.sh")
        migrate = script[
            script.index("migrate_one_bot() {"):script.index("migrate_restored_bots() {")
        ]

        self.assertNotRegex(migrate, r"start_commands\s*=")

    def test_the_defaults_do_not_declare_it(self):
        """Declaring it in the defaults would be harmless today, but would reset
        it the moment the merge direction was ever changed."""
        script = read_script("streamerbot.sh")
        defaults = script[script.index("streamerbot_config_defaults() {"):]
        defaults = defaults[: defaults.index("DEFAULTSJSON\n}")]

        self.assertNotIn("start_commands", defaults)

    def test_new_bots_get_it_too(self):
        for name in ("config.json", "config_default.json"):
            config = read_config(name)
            self.assertIn("start_commands", config["general"], name)
            self.assertIsInstance(config["general"]["start_commands"], list, name)


class TemplatesAreCurrentTests(TestCase):
    """A new bot must get the same sections a migrated one gets, or the two
    diverge and only one of them can be configured by hand."""

    def test_every_service_is_present(self):
        for name in ("config.json", "config_default.json"):
            services = read_config(name)["services"]
            for code in ("yt", "ytm", "sp", "nf", "dp", "am", "az"):
                self.assertIn(code, services, f"{name} missing {code}")

    def test_the_new_sections_are_present(self):
        for name in ("config.json", "config_default.json"):
            config = read_config(name)
            self.assertIn("auth_portal", config, name)
            self.assertIn("audio_description", config, name)
            self.assertIn("output_device_name", config["sound_devices"], name)

    def test_the_version_was_bumped(self):
        for name in ("config.json", "config_default.json"):
            self.assertGreaterEqual(read_config(name)["config_version"], 2, name)

    def test_no_template_still_names_the_old_project(self):
        for name in ("config.json", "config_default.json"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("TTMediaBot", text, name)

    def test_the_migration_defaults_and_the_template_agree_on_services(self):
        """Two lists of services that can drift is a bug waiting to happen."""
        script = read_script("streamerbot.sh")
        defaults = script[script.index("streamerbot_config_defaults() {"):]
        defaults = defaults[: defaults.index("DEFAULTSJSON\n}")]

        template_services = set(read_config("config.json")["services"])
        for code in ("sp", "nf", "dp", "am", "az"):
            self.assertIn(f'"{code}"', defaults, f"migration defaults missing {code}")
            self.assertIn(code, template_services, f"template missing {code}")


class CookieRemovalTests(TestCase):
    """Phase 2 replaced the cookie file with a device code. Anything still
    asking for one is asking for a file the bot ignores."""

    def test_creation_no_longer_forces_a_cookie_path_into_the_config(self):
        # A plain assertNotIn would dump the whole 2000-line script into the
        # failure message and bury the point.
        script = read_script("streamerbot.sh")
        needle = '.services.yt.cookiefile_path = "data/cookies.txt"'

        self.assertFalse(needle in script, f"streamerbot.sh still writes {needle}")

    def test_templates_do_not_declare_a_cookie_path(self):
        for name in ("config.json", "config_default.json"):
            self.assertNotIn("cookiefile_path", read_config(name)["services"]["yt"], name)


class NoCookieFileIsEverWrittenTests(TestCase):
    """A cookies.txt in a bot folder is a stale credential nothing reads. It must
    not be created, copied between bots, or mounted into a container."""

    def setUp(self):
        self.script = read_script("streamerbot.sh")

    def test_nothing_copies_a_cookie_file_into_a_bot_folder(self):
        # Checked as plain substrings: the shell forms that would copy a cookie
        # file into a bot directory, without a regex that needs escaping.
        for form in (
            'cp "$cookies_path" "$CURRENT_BOT_DIR/cookies.txt"',
            'cp "$SOURCE_BOT_DIR/cookies.txt" "$CURRENT_BOT_DIR/cookies.txt"',
            'cp "$d/cookies.txt"',
        ):
            self.assertFalse(form in self.script, f"streamerbot.sh still does: {form}")

    def test_nothing_creates_an_empty_one(self):
        self.assertNotRegex(self.script, r'touch "\$[A-Za-z_]*(BOT_DIR|d)/cookies\.txt"')

    def test_no_container_mounts_one(self):
        needle = "/home/streamer/StreamerBot/data/cookies.txt"

        self.assertFalse(
            needle in self.script, f"streamerbot.sh still mounts {needle}"
        )

    def test_creation_removes_one_it_finds(self):
        """Only reachable when a directory is reused, but a leftover credential
        should not survive a fresh bot being created on top of it."""
        self.assertIn('rm -f "$CURRENT_BOT_DIR/cookies.txt"', self.script)


class CreationIsLoggedTests(TestCase):
    """Creating a bot touches Docker, jq, the filesystem and the network. When it
    fails the detail has usually scrolled away or was sent to /dev/null."""

    def setUp(self):
        self.script = read_script("streamerbot.sh")

    def test_there_is_a_log_file_outside_the_bots_directory(self):
        """A creation that fails early may never get a bot directory, which is
        exactly the case worth having a record of."""
        self.assertIn('MANAGER_LOG="${SCRIPT_DIR}/logs/manager.log"', self.script)

    def test_container_creation_no_longer_discards_its_output(self):
        create = self.script[self.script.index("log_say \"Creating the container.\""):]
        create = create[: create.index("fi")]

        self.assertIn("log_run", create)
        self.assertNotIn("> /dev/null 2>&1", create)

    def test_the_log_records_what_was_asked_for(self):
        self.assertRegex(self.script, r'log_line "Creating bot \$current_bot_name')

    def test_passwords_are_not_written_to_the_log(self):
        """A log that records a password is a log that leaks it."""
        line = next(
            l for l in self.script.splitlines()
            if l.strip().startswith('log_line "Creating bot')
        )

        self.assertNotIn("$password", line)
        self.assertNotIn("$channel_password", line)


if __name__ == "__main__":
    unittest.main()
