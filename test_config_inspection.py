"""Catching a configuration that will not start, before it is started.

A bot whose configuration the bot itself rejects looks identical from outside to
a bot that is merely waiting for a server: the container is up. That is how
config_version 2 against a ConfigManager that understood 1 went unnoticed, and
how a config copied from TTMediaBot dies on a KeyError for a service named "vk".
"""

import json
import os
import tempfile
import unittest

from bot.config import ConfigManager
from bot.config.inspection import (
    ERROR,
    NOTE,
    WARNING,
    inspect_config_data,
    inspect_config_file,
)
from bot.migrators.config_migrator import is_foreign_lineage, migrate_functs

# Trimmed from gumerov-amir/TTMediaBot's own config_default.json. The service
# set is the giveaway: vk and yam have no equivalent here.
TTMEDIABOT_CONFIG = {
    "config_version": 0,
    "general": {"cache_file_name": "TTMediaBotCache.dat", "language": "en"},
    "logger": {"file_name": "TTMediaBot.log"},
    "teamtalk": {"hostname": "tt.example.org", "nickname": "Inherited"},
    "services": {
        "default_service": "vk",
        "vk": {"enabled": True, "token": ""},
        "yam": {"enabled": True, "token": ""},
        "yt": {"enabled": True},
    },
}


def codes(findings, severity=None):
    return {f.code for f in findings if severity is None or f.severity == severity}


class ForeignLineageTests(unittest.TestCase):
    def test_a_ttmediabot_config_is_recognised(self):
        self.assertTrue(is_foreign_lineage(TTMEDIABOT_CONFIG))

    def test_our_own_config_is_not(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "config.json"), encoding="utf-8") as f:
            self.assertFalse(is_foreign_lineage(json.load(f)))

    def test_lineage_is_decided_by_shape_not_by_the_version_number(self):
        """The hole this closes.

        A fork that reached its own version 2 means something different by it.
        migrate() returns early when the number matches, so such a file used to
        pass through untouched, keeping a default_service the bot dies on.
        """
        foreign = dict(TTMEDIABOT_CONFIG, config_version=ConfigManager.version)
        self.assertTrue(is_foreign_lineage(foreign))

        migrated = foreign
        for ver in sorted(migrate_functs):
            migrated = migrate_functs[ver](migrated)
        self.assertEqual(migrated["services"]["default_service"], "yt")
        self.assertEqual(migrated["general"]["cache_file_name"], "StreamerBotCache.dat")
        self.assertEqual(migrated["logger"]["file_name"], "StreamerBot.log")

    def test_the_inherited_server_details_are_never_touched(self):
        migrated = dict(TTMEDIABOT_CONFIG)
        for ver in sorted(migrate_functs):
            migrated = migrate_functs[ver](migrated)
        self.assertEqual(migrated["teamtalk"]["hostname"], "tt.example.org")
        self.assertEqual(migrated["teamtalk"]["nickname"], "Inherited")


class InspectionTests(unittest.TestCase):
    def test_a_good_config_reports_nothing_serious(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "config.json"), encoding="utf-8") as f:
            data = json.load(f)
        data["teamtalk"]["hostname"] = "tt.example.org"
        data["teamtalk"]["nickname"] = "Fine"
        self.assertEqual(codes(inspect_config_data(data), ERROR), set())
        self.assertEqual(codes(inspect_config_data(data), WARNING), set())

    def test_a_version_from_the_future_is_an_error_naming_both_numbers(self):
        data = {"config_version": ConfigManager.version + 5, "teamtalk": {}}
        findings = inspect_config_data(data)
        self.assertIn("version_too_new", codes(findings, ERROR))
        message = next(f.message for f in findings if f.code == "version_too_new")
        self.assertIn(str(ConfigManager.version + 5), message)
        self.assertIn(str(ConfigManager.version), message)

    def test_a_version_that_is_not_a_number_is_an_error(self):
        findings = inspect_config_data({"config_version": "two", "teamtalk": {}})
        self.assertIn("version_not_a_number", codes(findings, ERROR))

    def test_a_ttmediabot_config_warns_but_does_not_block(self):
        """Migration repairs it, so it must not be reported as fatal.

        Reporting a problem that starting the bot would have fixed teaches
        people to ignore the report.
        """
        findings = inspect_config_data(TTMEDIABOT_CONFIG)
        self.assertIn("foreign_lineage", codes(findings, WARNING))
        self.assertEqual(codes(findings, ERROR), set())

    def test_a_service_name_migration_cannot_repair_is_an_error(self):
        """A typo in a current config is not a lineage problem, so it stands."""
        data = {
            "config_version": ConfigManager.version,
            "services": {"default_service": "youtube"},
            "teamtalk": {"hostname": "tt.example.org", "nickname": "x"},
        }
        self.assertIn("unknown_default_service", codes(inspect_config_data(data), ERROR))

    def test_a_server_on_this_same_box_is_not_a_complaint(self):
        """Containers are created with --network host, so localhost inside the
        container is the host. Running the TeamTalk server on the same machine
        is an ordinary deployment, not a mistake."""
        for hostname in ("localhost", "127.0.0.1", "::1"):
            with self.subTest(hostname=hostname):
                data = {
                    "config_version": ConfigManager.version,
                    "teamtalk": {
                        "hostname": hostname,
                        "username": "streamer",
                        "nickname": "Local Bot",
                    },
                }
                findings = inspect_config_data(data)
                self.assertEqual(codes(findings, WARNING), set())
                self.assertEqual(codes(findings, ERROR), set())
                self.assertIn("local_server", codes(findings, NOTE))

    def test_a_local_server_with_no_account_is_still_fine(self):
        """Some servers take a guest login. An empty username alone proves
        nothing, so it must not be enough to accuse anybody."""
        data = {
            "config_version": ConfigManager.version,
            "teamtalk": {"hostname": "localhost", "username": "", "nickname": "Mine"},
        }
        self.assertEqual(codes(inspect_config_data(data), WARNING), set())

    def test_the_untouched_template_is_flagged(self):
        """Server, account and nickname all still at their defaults: this is a
        bot nobody finished creating, which is a different thing from a bot
        pointed at a local server."""
        import json as _json
        import os as _os

        here = _os.path.dirname(_os.path.abspath(__file__))
        with open(_os.path.join(here, "config.json"), encoding="utf-8") as f:
            data = _json.load(f)
        self.assertIn("never_configured", codes(inspect_config_data(data), WARNING))

    def test_no_server_at_all_is_flagged(self):
        data = {"config_version": ConfigManager.version,
                "teamtalk": {"hostname": "", "username": "u", "nickname": "n"}}
        self.assertIn("no_server", codes(inspect_config_data(data), WARNING))

    def test_a_missing_nickname_is_flagged(self):
        data = {"config_version": ConfigManager.version,
                "teamtalk": {"hostname": "tt.example.org", "username": "u",
                             "nickname": ""}}
        self.assertIn("no_nickname", codes(inspect_config_data(data), WARNING))

    def test_a_wrongly_typed_value_is_reported_not_raised(self):
        data = {"config_version": ConfigManager.version,
                "teamtalk": {"hostname": "tt.example.org", "nickname": "x",
                             "tcp_port": "not a port"}}
        self.assertIn("invalid_value", codes(inspect_config_data(data), ERROR))


class FileHandlingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_broken_json_is_reported_rather_than_raised(self):
        self.write('{ "config_version": 2, "teamtalk": {')
        self.assertIn("unreadable", codes(inspect_config_file(self.path), ERROR))

    def test_a_missing_file_is_reported(self):
        self.assertIn("missing", codes(inspect_config_file(self.path), ERROR))

    def test_inspection_never_writes_to_the_file(self):
        """A running bot owns its config.json. Inspection is a reader."""
        self.write(json.dumps(TTMEDIABOT_CONFIG))
        with open(self.path, "rb") as f:
            before = f.read()
        mtime = os.stat(self.path).st_mtime_ns
        inspect_config_file(self.path)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)

    def test_a_config_locked_by_a_running_bot_can_still_be_inspected(self):
        """ConfigManager takes an exclusive portalocker lock for the bot's whole
        run. A check that cannot run while the bots are up is a check nobody
        runs, so inspection must not go anywhere near that lock."""
        import portalocker

        self.write(json.dumps(TTMEDIABOT_CONFIG))
        lock = portalocker.Lock(
            self.path, timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB
        )
        lock.acquire()
        try:
            findings = inspect_config_file(self.path)
        finally:
            lock.release()
        self.assertIn("foreign_lineage", codes(findings, WARNING))


if __name__ == "__main__":
    unittest.main()
