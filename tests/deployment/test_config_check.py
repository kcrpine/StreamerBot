"""The manager must check configurations before it starts anything.

Host-only, like its neighbours: it reads streamerbot.sh, which .dockerignore
keeps out of the runtime image.

The ordering is the whole point. A configuration the bot rejects produces a
container that is up and never joins a channel, which is indistinguishable from
a bot waiting on an unreachable server. Saying so at creation time is the
difference between a sentence and an afternoon.
"""

import unittest
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]


def read_script(name):
    path = ROOT / name
    if not path.is_file():
        raise unittest.SkipTest(f"{name} is not present; host-only test skipped")
    return path.read_text(encoding="utf-8", errors="replace")


class TheCheckerIsAskedNotReimplementedTests(TestCase):
    """The rules live in Python. The shell asks; it does not restate them.

    Restating them is exactly how config.json came to declare version 2 while
    ConfigManager still understood 1.
    """

    def setUp(self):
        self.script = read_script("streamerbot.sh")

    def test_the_function_exists(self):
        self.assertIn("validate_bot_configs() {", self.script)

    def test_it_runs_the_python_checker(self):
        func = self.script[self.script.index("validate_bot_configs() {"):]
        func = func[: func.index("\n}\n")]
        self.assertIn("tools/check_config.py", func)

    def test_it_does_not_reimplement_the_version_rule_in_jq(self):
        func = self.script[self.script.index("validate_bot_configs() {"):]
        func = func[: func.index("\n}\n")]
        self.assertNotIn("config_version", func)

    def test_the_bots_mount_is_read_only(self):
        """A running bot holds a lock on its own config.json."""
        func = self.script[self.script.index("validate_bot_configs() {"):]
        func = func[: func.index("\n}\n")]
        self.assertIn('"${BOTS_ROOT}:/bots:ro"', func)

    def test_an_older_image_is_reported_not_crashed_into(self):
        func = self.script[self.script.index("validate_bot_configs() {"):]
        func = func[: func.index("\n}\n")]
        self.assertIn("predates the configuration check", func)


class CreationChecksEveryBotTests(TestCase):
    def setUp(self):
        self.script = read_script("streamerbot.sh")
        self.create = self.script[self.script.index("create_bot() {"):]
        self.create = self.create[: self.create.index("\n# Function: List Bots")]

    def test_creation_validates_before_starting(self):
        validate_at = self.create.index("validate_bot_configs")
        start_at = self.create.index("docker start")
        self.assertLess(validate_at, start_at)

    def test_it_checks_every_bot_not_only_the_new_one(self):
        """A config copied in by hand never passed through any migration."""
        func = self.script[self.script.index("validate_bot_configs() {"):]
        func = func[: func.index("\n}\n")]
        self.assertIn("--bots-root", func)

    def test_a_failure_is_stated_after_the_bots_start(self):
        self.assertIn("will not connect until their", self.create)


class RestoreChecksAfterMigratingTests(TestCase):
    def setUp(self):
        self.script = read_script("streamerbot.sh")
        self.migrate = self.script[self.script.index("migrate_restored_bots() {"):]
        self.migrate = self.migrate[: self.migrate.index("\nrestore_bots() {")]

    def test_the_check_runs_after_the_migration(self):
        migrate_at = self.migrate.index("migrate_one_bot")
        validate_at = self.migrate.index("validate_bot_configs")
        self.assertLess(migrate_at, validate_at)

    def test_restore_still_migrates_before_creating_containers(self):
        restore = self.script[self.script.index("restore_bots() {"):]
        self.assertLess(
            restore.index("migrate_restored_bots"), restore.index("recreate_bot_containers")
        )


if __name__ == "__main__":
    unittest.main()
