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


def slice_function(script, name):
    """One shell function's body, so a failure names the function, not the file.

    A plain assertIn against the whole script dumps 3,000 lines into the failure
    message and buries the point.
    """
    start = script.index("%s() {" % name)
    return script[start:script.index("\n}", start)]


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

    def test_nothing_in_the_migration_itself_is_deleted(self):
        """Config and data are never destroyed.

        The old cookies.txt used to be deleted here. It is not any more: Phase 9
        retired the device code and YouTube plays as a signed-in browser session,
        so that file is a usable sign-in rather than dead weight. What happens to
        it now lives in migrate_youtube_cookie_file and is pinned below.
        """
        migrate = self.script[
            self.script.index("migrate_one_bot() {"):self.script.index("migrate_restored_bots() {")
        ]
        self.assertNotIn("rm -rf", migrate)
        # Removing the temporary file a failed jq wrote is not deleting the
        # bot's data. Nothing under $dir is removed.
        self.assertNotRegex(migrate, r"rm\s+(-\w+\s+)*\"?\$dir/")

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
    """cookiefile_path is a config key the bridge stopped reading in Phase 2.

    Note this is not about cookies being unused. Phase 9 brought them back as
    the way YouTube signs in. It is about the *setting*, which nothing reads
    and which pointed at the wrong location anyway.
    """

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


class RestoredYouTubeSessionTests(TestCase):
    """A cookies.txt arriving in a backup is this bot's YouTube sign-in.

    This reversed twice and the messages went stale with it. The old TTMediaBot
    played YouTube from a cookies.txt; Phase 2 replaced that with an OAuth device
    code, so the migration deleted the file and told people to sign in with a
    code; Phase 9 then retired the device code, because YouTube answers 400 to
    OAuth-authenticated player requests. Cookies are how it signs in again, so
    deleting a restored one threw away a working account, and the advice printed
    beside it described a flow that no longer exists.
    """

    def setUp(self):
        self.script = read_script("streamerbot.sh")
        self.migrate = slice_function(self.script, "migrate_youtube_cookie_file")

    def test_the_migration_calls_it(self):
        one_bot = self.script[
            self.script.index("migrate_one_bot() {"):self.script.index("migrate_restored_bots() {")
        ]
        self.assertIn("migrate_youtube_cookie_file", one_bot)

    def test_a_usable_session_is_staged_rather_than_deleted(self):
        self.assertIn("youtube_auth/imported_cookies.txt", self.migrate)
        self.assertIn('mv "$dir/cookies.txt"', self.migrate)

    def test_it_is_not_written_straight_to_the_file_the_bridge_reads(self):
        """cookies.txt and this bot's Chrome profile have to agree. A session
        present only in the file answers "not signed in" when the keep-alive
        asks Chrome, so a working session would be marked expired on the first
        refresh. The bot imports it instead, which loads it into the profile."""
        self.assertNotRegex(
            self.migrate,
            r'(mv|cp)\s+"\$dir/cookies\.txt"\s+"\$dir/youtube_auth/cookies\.txt"',
        )

    def test_a_live_session_is_never_replaced_by_the_restored_one(self):
        """The staged file came out of a backup, so it can only be the older."""
        self.assertIn('[ -f "$dir/youtube_auth/cookies.txt" ]', self.migrate)

    def test_the_staged_file_is_not_left_world_readable(self):
        self.assertIn("chmod 600", self.migrate)

    def test_a_file_carrying_no_sign_in_is_not_kept(self):
        self.assertIn("cookie_file_carries_google_session", self.migrate)

    def test_the_check_requires_both_halves_of_a_google_session(self):
        """Mirrors bot/auth/cookies.py::has_google_session. SAPISID is what the
        request signature is computed from and SID is the session itself; either
        alone is not signed in, so a file with only one must be refused."""
        check = slice_function(self.script, "cookie_file_carries_google_session")

        self.assertIn("SAPISID", check)
        self.assertIn("__Secure-3PAPISID", check)
        self.assertIn("__Secure-1PSID", check)
        self.assertIn("youtube", check)
        # Two separate greps, so one cookie name cannot satisfy both halves.
        self.assertEqual(2, check.count("grep -qE"))

    def test_nothing_still_tells_people_to_sign_in_with_a_code(self):
        """The device code is gone. Sending a user to a screen that no longer
        exists is worse than saying nothing at all."""
        self.assertNotIn("signs in with a code", self.script)

    def test_the_port_conflict_note_does_not_claim_youtube_is_unaffected(self):
        """YouTube sign-in is a portal page now: user_commands.py mints a
        /connect/yt or /import/yt link. A portal that cannot bind does stop it,
        so saying otherwise sent people looking in the wrong place."""
        note = slice_function(self.script, "disable_portal_for_port_conflict")

        self.assertNotIn("YouTube and Spotify sign-in are unaffected", note)
        self.assertNotIn("those use a code in the channel rather than the portal", note)
        # Spotify genuinely is unaffected, and that distinction is the point.
        self.assertIn("Spotify", note)


class PortalAccessIsAskedForTests(TestCase):
    """auth_portal.host decides whether anything but this machine can open the
    portal. It existed and worked, and the only way to reach it was editing JSON
    by hand over SSH -- while the bot itself printed "set auth_portal.host to
    0.0.0.0" whenever it detected the problem."""

    def setUp(self):
        self.script = read_script("streamerbot.sh")
        self.bulk = self.script[
            self.script.index("bulk_update_config() {"):self.script.index("duplicate_bot() {")
        ]
        self.create = self.script[
            self.script.index("create_bot() {"):self.script.index("list_bots() {")
        ]

    def test_there_is_one_question_shared_by_both_callers(self):
        """Two copies would drift, and the sentence about what exposing the
        portal costs is the half most likely to be dropped from the copy."""
        self.assertEqual(1, self.script.count("ask_portal_host() {"))

    def test_creating_a_bot_asks(self):
        self.assertIn("ask_portal_host", self.create)

    def test_creating_a_bot_writes_the_answer(self):
        self.assertIn("--arg portal_host", self.create)
        self.assertIn(".auth_portal.host = $portal_host", self.create)

    def test_editing_bots_can_change_it(self):
        self.assertIn("ask_portal_host", self.bulk)
        self.assertIn(".auth_portal.host = ", self.bulk)

    def test_editing_shows_what_the_bots_currently_have(self):
        self.assertIn("current_portal_host", self.bulk)

    def test_the_everything_option_still_covers_every_field(self):
        """"Everything" moved from 7 to 8 when this was added. A branch left on
        the old number silently stops being part of Everything."""
        for field in ("1", "2", "3", "4", "5", "6", "7"):
            self.assertIn(
                'if [[ "$choice" == "%s" || "$choice" == "8" ]]; then' % field,
                self.bulk,
                "field %s is not part of Everything" % field,
            )

    def test_the_menu_offers_both_numbers(self):
        self.assertIn('echo "7. Account portal access', self.bulk)
        self.assertIn('echo "8. Everything"', self.bulk)
        self.assertIn("1|2|3|4|5|6|7|8)", self.bulk)

    def test_only_the_two_known_addresses_can_be_written(self):
        """The value reaches a jq program by string concatenation, so it must
        never be free text somebody typed."""
        ask = slice_function(self.script, "ask_portal_host")

        self.assertIn('PORTAL_HOST="$PORTAL_HOST_ANY"', ask)
        self.assertIn('PORTAL_HOST="$PORTAL_HOST_LOCAL"', ask)
        self.assertNotIn('PORTAL_HOST="$answer"', ask)

    def test_opening_it_up_says_what_that_costs(self):
        """The portal has no login page by design and speaks plain HTTP. Asking
        someone to expose it without saying so is asking them to agree to
        something they were never told."""
        ask = slice_function(self.script, "ask_portal_host")

        self.assertIn("firewall", ask)
        self.assertRegex(ask, r"unencrypted|plain HTTP")

    def test_the_default_is_still_loopback(self):
        """Pressing Enter must never open a portal to the network."""
        self.assertIn('PORTAL_HOST_LOCAL="127.0.0.1"', self.script)
        self.assertIn('PORTAL_HOST_ANY="0.0.0.0"', self.script)
        for name in ("config.json", "config_default.json"):
            self.assertEqual("127.0.0.1", read_config(name)["auth_portal"]["host"], name)


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
