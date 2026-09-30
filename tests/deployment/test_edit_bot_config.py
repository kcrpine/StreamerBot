"""Manage Bots > Edit Bot Configuration.

Create Bot's questions, asked again for one existing bot with its own current
values as the defaults. These run the real shell function with typed answers on
stdin, because what matters is what lands in config.json, not whether a line is
present in the script.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import TestCase

from tests.deployment.test_port_allocation import extract_function, require_tools

ROOT = Path(__file__).resolve().parents[2]

# Answer order: bot number, rename (inserted by run_edit), server, TCP, UDP, encrypted, username, password,
# nickname, startup command, channel, channel password, delete timer, portal
# access, confirm, restart.
KEEP_ALL = ["1"] + [""] * 12

BASE_CONFIG = {
    "general": {"start_commands": ["u http://radio.example/live.mp3"],
                "delete_uploaded_files_after": 300},
    "teamtalk": {
        "hostname": "tt.example.org", "tcp_port": 10333, "udp_port": 10333,
        "encrypted": False, "nickname": "Radio", "username": "radio",
        "password": "old-secret", "channel": "/Music/", "channel_password": "",
    },
    "auth_portal": {"host": "127.0.0.1", "port": 4421},
    "player": {"stop_when_solo": False},
}


class EditBotConfigTests(TestCase):
    def setUp(self):
        require_tools()
        script_path = ROOT / "streamerbot.sh"
        if not script_path.is_file():
            raise unittest.SkipTest("streamerbot.sh is not present")
        self.script = script_path.read_text(encoding="utf-8", errors="replace")
        self.tmp = Path(tempfile.mkdtemp())
        self.bots = self.tmp / "bots"
        (self.bots / "radio").mkdir(parents=True)
        self.config_path = self.bots / "radio" / "config.json"
        self.config_path.write_text(json.dumps(BASE_CONFIG, indent=4), encoding="utf-8")
        self.log = self.tmp / "log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_edit(self, answers, running=False, rename=None, create_fails=False):
        answers = answers[:1] + (["y", rename] if rename else [""]) + answers[1:]
        preamble = [
            "set -u",
            f'BOTS_ROOT="{self.bots.as_posix()}"',
            'RED=""; GREEN=""; YELLOW=""; NC=""',
            'PORTAL_HOST_LOCAL="127.0.0.1"; PORTAL_HOST_ANY="0.0.0.0"; PORTAL_HOST="127.0.0.1"',
            "header() { :; }",
            "chown() { :; }",
            f'log_line() {{ echo "$*" >> "{self.log.as_posix()}"; }}',
            f'ufw_sync_portal_ports() {{ echo ufw >> "{self.log.as_posix()}"; }}',
            # docker ps names the container only when the test says it runs.
            'docker() { if [ "$1" = ps ]; then [ "$2" = -q ] && %s; return 0; fi; '
            'echo "docker $*" >> "%s"; [ "$1" = create ] && return %d; return 0; }'
            % ("echo abc123" if running else ":", self.log.as_posix(), 1 if create_fails else 0),
            # The real one logs and runs; the command is what is under test.
            'log_run() { shift; "$@"; }',
            'BOT_IMAGE="streamerbot:test"; YOUTUBE_BRIDGE_URL="http://b"; YOUTUBE_PROXY_URL=""',
            "BOT_NAME_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'",
            extract_function(self.script, "bot_name_is_valid"),
            extract_function(self.script, "rename_bot_container"),
            extract_function(self.script, "ask_portal_host"),
            extract_function(self.script, "edit_bot_config"),
            "edit_bot_config",
        ]
        return subprocess.run(
            ["bash", "-c", "\n".join(preamble)],
            input="\n".join(answers) + "\n" * 4,
            capture_output=True, text=True, timeout=60,
        )

    def config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def log_text(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_pressing_enter_throughout_changes_nothing(self):
        before = self.config_path.read_bytes()
        result = self.run_edit(KEEP_ALL)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Nothing changed", result.stdout)
        self.assertEqual(before, self.config_path.read_bytes())

    def test_the_current_startup_command_is_listed(self):
        # read -p prints its prompt only to a terminal, so the "[Current: ...]"
        # prompts cannot be seen from here; the echoed lines can.
        result = self.run_edit(KEEP_ALL)
        self.assertIn("This bot has 1 startup command:", result.stdout)
        self.assertIn("u http://radio.example/live.mp3", result.stdout)
        self.assertIn("Encrypted? Currently no.", result.stdout)

    def test_one_change_touches_only_that_field(self):
        answers = ["1", "", "", "", "", "", "", "Radio Two"] + [""] * 5 + ["y"]
        result = self.run_edit(answers)
        self.assertEqual(0, result.returncode, result.stderr)
        expected = json.loads(json.dumps(BASE_CONFIG))
        expected["teamtalk"]["nickname"] = "Radio Two"
        self.assertEqual(expected, self.config())

    def test_a_password_with_quotes_is_written_literally(self):
        """Bulk Update builds its jq program from strings, and a quote there
        ends the value early. This one must not."""
        secret = 'pa"ss\\word $HOME'
        answers = ["1", "", "", "", "", "", secret] + [""] * 6 + ["y"]
        self.run_edit(answers)
        self.assertEqual(secret, self.config()["teamtalk"]["password"])

    def test_passwords_are_never_shown_or_logged(self):
        answers = ["1", "", "", "", "", "", "new-secret", "", "", "", "chan-secret", "", "", "y"]
        result = self.run_edit(answers)
        for secret in ("old-secret", "new-secret", "chan-secret"):
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, self.log_text())
        self.assertEqual("chan-secret", self.config()["teamtalk"]["channel_password"])

    def test_a_period_clears_and_removes(self):
        answers = ["1", "", "", "", "", "", ".", "", ".", "", "", "", "", "y"]
        self.run_edit(answers)
        self.assertEqual("", self.config()["teamtalk"]["password"])
        self.assertEqual([], self.config()["general"]["start_commands"])

    def test_invalid_numbers_keep_the_current_value(self):
        answers = ["1", "", "99999", "abc", "", "", "", "", "", "", "", "-5", "", "y"]
        result = self.run_edit(answers)
        self.assertIn("Nothing changed", result.stdout)
        self.assertEqual(10333, self.config()["teamtalk"]["tcp_port"])

    def test_numbers_are_written_as_numbers(self):
        answers = ["1", "", "10444", "", "2", "", "", "", "", "", "", "60", "", "y"]
        self.run_edit(answers)
        tt = self.config()["teamtalk"]
        self.assertEqual(10444, tt["tcp_port"])
        self.assertIs(True, tt["encrypted"])
        self.assertEqual(60, self.config()["general"]["delete_uploaded_files_after"])

    def test_declining_leaves_the_file_alone(self):
        before = self.config_path.read_bytes()
        self.run_edit(["1", "new.example.org"] + [""] * 11 + ["n"])
        self.assertEqual(before, self.config_path.read_bytes())

    def test_opening_the_portal_syncs_the_firewall(self):
        answers = ["1"] + [""] * 11 + ["2", "y"]
        self.run_edit(answers)
        self.assertEqual("0.0.0.0", self.config()["auth_portal"]["host"])
        self.assertIn("ufw", self.log_text())

    def test_a_running_bot_is_restarted_by_default(self):
        self.run_edit(["1", "new.example.org"] + [""] * 11 + ["y", ""], running=True)
        self.assertIn("docker restart -t 1 radio", self.log_text())

    def test_a_restart_can_be_declined(self):
        result = self.run_edit(["1", "new.example.org"] + [""] * 11 + ["y", "n"], running=True)
        self.assertNotIn("docker restart", self.log_text())
        self.assertIn("next time radio starts", result.stdout)

    def test_renaming_moves_the_folder_and_recreates_the_container(self):
        result = self.run_edit(KEEP_ALL + ["y"], running=True, rename="radio2")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.bots / "radio").exists())
        self.assertTrue((self.bots / "radio2" / "config.json").is_file())
        log = self.log_text()
        self.assertIn("--name radio2", log)
        self.assertIn("TTBOT_INSTANCE=radio2", log)
        self.assertIn("%s/radio2:/home/streamer/StreamerBot/data" % self.bots.as_posix(), log)
        self.assertIn("docker rm -f radio", log)
        self.assertIn("docker start radio2", log)

    def test_a_stopped_bot_is_renamed_and_left_stopped(self):
        self.run_edit(KEEP_ALL + ["y"], rename="radio2")
        self.assertTrue((self.bots / "radio2").is_dir())
        self.assertNotIn("docker start", self.log_text())

    def test_a_failed_create_puts_everything_back(self):
        result = self.run_edit(KEEP_ALL + ["y"], running=True, rename="radio2", create_fails=True)
        self.assertTrue((self.bots / "radio" / "config.json").is_file())
        self.assertFalse((self.bots / "radio2").exists())
        self.assertNotIn("docker rm", self.log_text())
        self.assertIn("docker start radio", self.log_text())
        self.assertIn("keeps its name", result.stdout)

    def test_a_name_already_taken_is_refused(self):
        (self.bots / "other").mkdir()
        result = self.run_edit(KEEP_ALL, rename="other")
        self.assertIn("already exists", result.stdout)
        self.assertIn("Nothing changed", result.stdout)
        self.assertTrue((self.bots / "radio").is_dir())

    def test_an_invalid_name_is_refused(self):
        result = self.run_edit(KEEP_ALL, rename="../escape")
        self.assertIn("Nothing changed", result.stdout)
        self.assertTrue((self.bots / "radio").is_dir())

    def test_the_manage_menu_offers_it_just_before_bulk_update(self):
        manage = self.script[self.script.index("manage_bots() {"):]
        manage = manage[: manage.index("\n}\n")]
        self.assertIn('echo "8. Edit Bot Configuration"', manage)
        self.assertIn('echo "9. Bulk Update Configuration"', manage)
        self.assertRegex(manage, r"\n\s*8\)\s*\n\s*edit_bot_config\n")


if __name__ == "__main__":
    unittest.main()
