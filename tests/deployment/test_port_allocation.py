"""Per-bot ports for the account portal and go-librespot.

Bot containers are created with --network host, so every bot shares the host's
port space. Both listeners were written into every bot's config.json as the same
constant, which meant the first bot to start won and the rest failed silently in
two different ways:

  - the portal died with "Address already in use", and because a portal that
    failed to start was indistinguishable from one that was switched off, "li am"
    told people the portal was disabled in their configuration when it was not;
  - go-librespot exited about a second after every start, forever, so Spotify
    never worked on any bot but the first.

These run the real shell functions rather than reading the script, because the
bug was in what the code did, not in whether a line was present.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]

PORT_FUNCTIONS = [
    "bot_claimed_ports",
    "port_claimed_by_another_bot",
    "port_free_for_new_bot",
    "next_free_port",
    "disable_portal_for_port_conflict",
    "reenable_portal_after_repair",
    "assign_unique_bot_ports",
    "repair_all_bot_ports",
]


def require_tools():
    for tool in ("bash", "jq"):
        if not shutil.which(tool):
            raise unittest.SkipTest(f"{tool} is not available on this host")


def extract_function(script, name):
    """Pull one shell function out of streamerbot.sh.

    The script runs its menu at top level, so it cannot simply be sourced. Style
    in this file is consistent enough that a closing brace in column zero ends a
    function.
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


class PortAllocationHarness(TestCase):
    def setUp(self):
        require_tools()
        script_path = ROOT / "streamerbot.sh"
        if not script_path.is_file():
            raise unittest.SkipTest("streamerbot.sh is not present")
        self.script = script_path.read_text(encoding="utf-8", errors="replace")
        self.tmp = Path(tempfile.mkdtemp())
        self.bots = self.tmp / "bots"
        self.bots.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_bot(self, name, portal=4419, api=3678, enabled=True):
        d = self.bots / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(
            json.dumps({
                "auth_portal": {"enabled": enabled, "port": portal, "host": "127.0.0.1"},
                "services": {"sp": {"enabled": True, "api_port": api}},
                "teamtalk": {"nickname": name},
            }),
            encoding="utf-8",
        )
        return d

    def run_shell(self, body):
        preamble = [
            "set -u",
            f'BOTS_ROOT="{self.bots.as_posix()}"',
            "DEFAULT_PORTAL_PORT=4419",
            "DEFAULT_LIBRESPOT_API_PORT=3678",
            # The real one writes to the manager log, which is not under test.
            "log_line() { :; }",
            # Nothing here runs as root.
            "chown() { :; }",
        ]
        for name in PORT_FUNCTIONS:
            preamble.append(extract_function(self.script, name))
        preamble.append(body)
        result = subprocess.run(
            ["bash", "-c", "\n".join(preamble)],
            capture_output=True, text=True, timeout=120,
        )
        return result

    def ports_of(self, name):
        data = json.loads((self.bots / name / "config.json").read_text(encoding="utf-8"))
        return data["auth_portal"]["port"], data["services"]["sp"]["api_port"]

    def portal_enabled(self, name):
        data = json.loads((self.bots / name / "config.json").read_text(encoding="utf-8"))
        return data["auth_portal"]["enabled"]


class AllocationTests(PortAllocationHarness):
    def test_a_lone_bot_keeps_the_defaults(self):
        """Nothing to clash with, so nothing should move."""
        self.make_bot("solo")

        result = self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/solo"')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ports_of("solo"), (4419, 3678))

    def test_a_second_bot_is_moved_off_both_ports(self):
        """The actual bug: two bots, identical ports, one working portal."""
        self.make_bot("first")
        self.make_bot("second")

        result = self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/second"')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ports_of("first"), (4419, 3678))
        portal, api = self.ports_of("second")
        self.assertNotEqual(portal, 4419)
        self.assertNotEqual(api, 3678)

    def test_the_two_ports_never_collide_with_each_other(self):
        """Assigned in one pass, so the portal's new port has to be treated as
        taken before go-librespot picks one."""
        self.make_bot("first")
        self.make_bot("second", portal=3678, api=4419)

        self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/second"')

        portal, api = self.ports_of("second")
        self.assertNotEqual(portal, api)

    def test_running_a_second_time_changes_nothing(self):
        """Idempotence is what makes it safe to run before every start."""
        self.make_bot("first")
        self.make_bot("second")

        self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/second"')
        after_first = self.ports_of("second")
        self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/second"')

        self.assertEqual(self.ports_of("second"), after_first)

    def test_a_bots_own_port_is_never_treated_as_a_clash(self):
        """A running bot is listening on its own port. Counting that as taken
        would move it on every restart, breaking the firewall rule and the
        public_url the user set up for it."""
        self.make_bot("solo", portal=4419)

        # Occupy 4419 for real, the way a running bot would.
        import socket
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 4419))
            sock.listen(1)
        except OSError:
            raise unittest.SkipTest("port 4419 is not bindable on this host")
        try:
            self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/solo"')
        finally:
            sock.close()

        self.assertEqual(self.ports_of("solo"), (4419, 3678))

    def test_five_bots_all_end_up_distinct(self):
        names = [f"bot{n}" for n in range(5)]
        for name in names:
            self.make_bot(name)

        self.run_shell("repair_all_bot_ports")

        portals = [self.ports_of(n)[0] for n in names]
        apis = [self.ports_of(n)[1] for n in names]
        self.assertEqual(len(set(portals)), len(names), portals)
        self.assertEqual(len(set(apis)), len(names), apis)

    def test_a_teamtalk_section_is_never_touched(self):
        """The same promise the restore path makes. A bot that comes back under a
        different nickname is worse than one with no portal."""
        self.make_bot("first")
        self.make_bot("second")

        self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/second"')

        data = json.loads((self.bots / "second" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["teamtalk"]["nickname"], "second")

    def test_a_broken_config_is_left_alone_rather_than_replaced(self):
        d = self.bots / "broken"
        d.mkdir()
        (d / "config.json").write_text("{not json", encoding="utf-8")

        result = self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/broken"')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((d / "config.json").read_text(encoding="utf-8"), "{not json")


class ExhaustionTests(PortAllocationHarness):
    """When no port can be found, the portal is switched off and says why.

    Leaving it configured to a port it cannot have would fail to bind on every
    start, and that failure is indistinguishable from being switched off -- which
    is the confusion this whole change exists to end.
    """

    def test_the_portal_is_disabled_when_nothing_is_free(self):
        # Claim the whole search range through other bots' configs.
        self.make_bot("victim")
        for n in range(4419, 4419 + 205):
            self.make_bot(f"hog{n}", portal=n, api=n)

        self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/victim"')

        self.assertFalse(self.portal_enabled("victim"))

    def test_a_note_is_written_into_the_bots_own_folder(self):
        self.make_bot("victim")
        for n in range(4419, 4419 + 205):
            self.make_bot(f"hog{n}", portal=n, api=n)

        self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/victim"')

        note = self.bots / "victim" / "PORT_CONFLICT.txt"
        self.assertTrue(note.is_file())
        text = note.read_text(encoding="utf-8")
        # It has to name the recovery path, or a blind user has a dead end.
        self.assertIn("Repair Account Portal and Spotify Ports", text)
        # And the firewall case, because the same symptom has a second cause.
        self.assertIn("firewall", text.lower())
        self.assertIn("forward", text.lower())

    def test_the_warning_names_what_still_works(self):
        """YouTube and Spotify sign in with a code, not the portal. Saying so
        stops someone concluding the whole bot is broken."""
        self.make_bot("victim")
        for n in range(4419, 4419 + 205):
            self.make_bot(f"hog{n}", portal=n, api=n)

        result = self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/victim"')

        self.assertIn("YouTube and Spotify still work", result.stdout)

    def test_the_portal_comes_back_on_once_a_port_is_free(self):
        self.make_bot("victim")
        hogs = [f"hog{n}" for n in range(4419, 4419 + 205)]
        for name, n in zip(hogs, range(4419, 4419 + 205)):
            self.make_bot(name, portal=n, api=n)

        self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/victim"')
        self.assertFalse(self.portal_enabled("victim"))

        # Free the range up again.
        for name in hogs:
            shutil.rmtree(self.bots / name)

        self.run_shell('assign_unique_bot_ports "$BOTS_ROOT/victim"')

        self.assertTrue(self.portal_enabled("victim"))
        self.assertFalse((self.bots / "victim" / "PORT_CONFLICT.txt").exists())


class WiringTests(TestCase):
    """That the allocation is actually called from the paths that need it."""

    def setUp(self):
        path = ROOT / "streamerbot.sh"
        if not path.is_file():
            raise unittest.SkipTest("streamerbot.sh is not present")
        self.script = path.read_text(encoding="utf-8", errors="replace")

    def test_a_new_bot_gets_its_own_ports(self):
        self.assertIn('assign_unique_bot_ports "$CURRENT_BOT_DIR"', self.script)

    def test_a_restored_bot_gets_its_own_ports(self):
        # Restored bots all arrive holding the same constants from the defaults.
        self.assertIn('assign_unique_bot_ports "$dir"', self.script)

    def test_starting_all_bots_checks_first(self):
        """The user's existing bots are already clashing, and they will never run
        a repair they do not know they need."""
        self.assertIn("repair_all_bot_ports quiet", self.script)

    def test_there_is_a_menu_item_and_a_flag(self):
        self.assertIn("Repair Account Portal and Spotify Ports", self.script)
        self.assertIn("--repair-ports", self.script)

    def test_the_flag_passes_the_pre_sudo_guard(self):
        """Unknown flags are rejected before elevating, so a new flag missing
        from that list fails with "Unknown option" instead of running."""
        guard = self.script.split("# Validate the flag name before elevating", 1)
        self.assertEqual(len(guard), 2, "the pre-sudo guard has moved")
        self.assertIn("--repair-ports", guard[1].split("esac", 1)[0])


if __name__ == "__main__":
    unittest.main()
