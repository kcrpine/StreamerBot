"""The configuration version has to move on both sides at once.

config.json declared config_version 2 while ConfigManager still declared 1, so
every newly created bot exited at startup with "invalid config_version value"
before it ever reached TeamTalk. Nothing in the suite read the migrator, so CI
stayed green. These tests read it.
"""

import json
import os
import tempfile
import unittest

from bot.config import ConfigManager
from bot.migrators import config_migrator


def write_config(directory, data):
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="UTF-8") as f:
        json.dump(data, f)
    return path


def read_config(path):
    with open(path, "r", encoding="UTF-8") as f:
        return json.load(f)


class ConfigVersionTests(unittest.TestCase):
    def test_the_shipped_config_is_a_version_the_bot_understands(self):
        """The file every new bot is created from must be readable by the bot.

        This is the exact failure: a shipped config_version above
        ConfigManager.version is rejected outright, so the bot exits at startup
        and the container restart-loops without ever connecting.
        """
        for name in ("config.json", "config_default.json"):
            with self.subTest(name=name):
                shipped = read_config(os.path.join(os.path.dirname(__file__), name))
                self.assertLessEqual(shipped["config_version"], ConfigManager.version)

    def test_every_version_the_bot_writes_has_a_migration(self):
        """ConfigManager.version and the migration table cannot drift apart.

        Bumping one without the other is what broke this: a config written at
        the new version has no way back to a bot that stops at the old one.
        """
        self.assertEqual(max(config_migrator.migrate_functs), ConfigManager.version)
        self.assertEqual(
            sorted(config_migrator.migrate_functs),
            list(range(1, ConfigManager.version + 1)),
            "every version from 1 upwards needs an entry",
        )


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def load(self, path):
        manager = ConfigManager(path)
        self.addCleanup(manager.close)
        return manager

    def test_a_current_config_loads_and_keeps_its_version(self):
        path = write_config(
            self.tmp.name,
            {"config_version": ConfigManager.version, "teamtalk": {"hostname": "ttt"}},
        )
        manager = self.load(path)
        self.assertEqual(manager.config.config_version, ConfigManager.version)
        self.assertEqual(manager.config.teamtalk.hostname, "ttt")

    def test_a_v1_config_is_migrated_and_written_back(self):
        path = write_config(
            self.tmp.name,
            {
                "config_version": 1,
                "general": {"cache_file_name": "TTMediaBotCache.dat"},
                "logger": {"file_name": "TTMediaBot.log"},
                "teamtalk": {"hostname": "ttt", "nickname": "kept"},
            },
        )
        manager = self.load(path)
        self.assertEqual(manager.config.config_version, 2)
        # The inherited names are still valid strings, so only a migration
        # replaces them; a merge against the defaults would keep them.
        self.assertEqual(manager.config.general.cache_file_name, "StreamerBotCache.dat")
        self.assertEqual(manager.config.logger.file_name, "StreamerBot.log")
        # The server details are what identifies a bot. A migration that moves
        # them brings the bot back as somebody else.
        self.assertEqual(manager.config.teamtalk.hostname, "ttt")
        self.assertEqual(manager.config.teamtalk.nickname, "kept")
        self.assertEqual(read_config(path)["config_version"], 2)

    def test_a_config_written_before_versioning_is_migrated_all_the_way(self):
        path = write_config(
            self.tmp.name,
            {
                "general": {"cache_file_name": "TTMediaBotCache.dat"},
                "teamtalk": {"hostname": "ttt"},
            },
        )
        manager = self.load(path)
        self.assertEqual(manager.config.config_version, ConfigManager.version)
        self.assertEqual(manager.config.general.cache_file_name, "StreamerBotCache.dat")
        self.assertEqual(manager.config.teamtalk.hostname, "ttt")

    def test_a_config_from_a_newer_bot_says_which_side_is_behind(self):
        path = write_config(
            self.tmp.name,
            {"config_version": ConfigManager.version + 1, "teamtalk": {}},
        )
        with self.assertRaises(SystemExit) as caught:
            ConfigManager(path)
        message = str(caught.exception)
        self.assertIn(str(ConfigManager.version + 1), message)
        self.assertIn(str(ConfigManager.version), message)

    def test_a_non_numeric_version_is_rejected_by_name(self):
        path = write_config(self.tmp.name, {"config_version": "two", "teamtalk": {}})
        with self.assertRaises(SystemExit) as caught:
            ConfigManager(path)
        self.assertIn("two", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
