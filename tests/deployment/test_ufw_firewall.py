"""Allowing the account portal's port through ufw.

ufw is replaced by a stub that keeps its rules in a file and prints the same
"Status" and rule lines the real one does, so the real shell functions run
against it. What is NOT covered is the real ufw: its output format is assumed
from 0.36 and its behaviour on a live host has not been observed.
"""

import json
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.deployment.test_port_allocation import ROOT, extract_function, require_tools

UFW_FUNCTIONS = [
    "ufw_state",
    "ufw_bot_portals",
    "ufw_host_is_loopback",
    "ufw_port_allowed",
    "ufw_tagged_rules",
    "ufw_stale_rules",
    "ufw_sync_portal_ports",
]

STUB = r"""#!/bin/bash
# Rules live one per line in $UFW_RULES as: port|comment
state="${UFW_STATE:-active}"
case "$1" in
  status)
    echo "Status: $state"
    [ "$state" = active ] || exit 0
    echo "To                         Action      From"
    echo "--                         ------      ----"
    echo "22/tcp                     ALLOW       Anywhere"
    while IFS='|' read -r p c; do
      [ -n "$p" ] && echo "$p/tcp                  ALLOW       Anywhere                   # $c"
    done < "$UFW_RULES" ;;
  allow)
    echo "$2" | sed 's|/tcp||' | tr -d '\n' >> "$UFW_RULES"; echo "|$4" >> "$UFW_RULES"
    echo "Rule added" ;;
  --force)
    p="${4%/tcp}"; grep -v "^$p|" "$UFW_RULES" > "$UFW_RULES.new"; mv "$UFW_RULES.new" "$UFW_RULES"
    echo "Rule deleted" ;;
esac
"""


class UfwHarness(unittest.TestCase):
    def setUp(self):
        require_tools()
        path = ROOT / "streamerbot.sh"
        if not path.is_file():
            raise unittest.SkipTest("streamerbot.sh is not present")
        self.script = path.read_text(encoding="utf-8", errors="replace")
        self.tmp = Path(tempfile.mkdtemp())
        self.bots = self.tmp / "bots"
        self.bots.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.rules = self.tmp / "rules"
        self.rules.write_text("", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install_stub(self):
        stub = self.bin / "ufw"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    def make_bot(self, name, port, host="0.0.0.0", enabled=True):
        d = self.bots / name
        d.mkdir()
        (d / "config.json").write_text(json.dumps(
            {"auth_portal": {"enabled": enabled, "port": port, "host": host}}))

    def run_fn(self, call, state="active", stdin="", stub=True):
        if stub:
            self.install_stub()
        pre = [
            "set -u",
            f'BOTS_ROOT="{self.bots.as_posix()}"',
            'UFW_RULE_TAG="StreamerBot portal"',
            "log_line() { :; }",
            # Bare /usr/bin and /bin only, so a real ufw cannot leak in.
            f'export UFW_RULES="{self.rules.as_posix()}" UFW_STATE={state}',
            f'export PATH="{self.bin.as_posix()}:$(dirname "$(command -v jq)"):$(dirname "$(command -v bash)")"',
        ]
        if not stub:
            pre = [p for p in pre if "UFW_STATE" not in p] + [
                f'export PATH="{self.bin.as_posix()}:$(dirname "$(command -v jq)")"']
        pre += [extract_function(self.script, n) for n in UFW_FUNCTIONS] + [call]
        return subprocess.run(["bash", "-c", "\n".join(pre)], input=stdin,
                              capture_output=True, text=True, timeout=60)

    def rule_ports(self):
        return sorted(l.split("|")[0] for l in self.rules.read_text().splitlines() if l)


class UfwTests(UfwHarness):
    def test_missing_ufw_changes_nothing_and_says_so(self):
        self.make_bot("a", 4419)
        r = self.run_fn("ufw_sync_portal_ports", stub=False)
        self.assertIn("not installed", r.stdout)
        self.assertEqual(self.rule_ports(), [])

    def test_opens_only_the_portal_port_after_confirmation(self):
        self.make_bot("a", 4419)
        r = self.run_fn("ufw_sync_portal_ports", stdin="y\n")
        self.assertEqual(self.rule_ports(), ["4419"])
        self.assertIn("ufw says: Rule added", r.stdout)

    def test_declining_changes_nothing(self):
        self.make_bot("a", 4419)
        self.run_fn("ufw_sync_portal_ports", stdin="n\n")
        self.assertEqual(self.rule_ports(), [])

    def test_loopback_and_disabled_portals_are_not_opened(self):
        self.make_bot("a", 4419, host="127.0.0.1")
        self.make_bot("b", 4421, enabled=False)
        r = self.run_fn("ufw_sync_portal_ports", stdin="y\n")
        self.assertEqual(self.rule_ports(), [])
        self.assertIn("listens on 127.0.0.1 only", r.stdout)
        self.assertIn("switched off", r.stdout)

    def test_existing_rule_is_reported_not_duplicated(self):
        self.make_bot("a", 4419)
        self.rules.write_text("4419|StreamerBot portal a\n")
        r = self.run_fn("ufw_sync_portal_ports")
        self.assertIn("already allowed", r.stdout)
        self.assertEqual(self.rule_ports(), ["4419"])

    def test_inactive_ufw_is_never_enabled_and_says_so(self):
        self.make_bot("a", 4419)
        r = self.run_fn("ufw_sync_portal_ports", state="inactive", stdin="y\n")
        self.assertIn("does\nnot switch it on", r.stdout)

    def test_changed_port_offers_to_remove_the_old_rule_only_when_agreed(self):
        self.make_bot("a", 4430)
        self.rules.write_text("4419|StreamerBot portal a\n5000|somebody else's\n")
        self.run_fn("ufw_sync_portal_ports", stdin="y\nn\n")
        self.assertEqual(self.rule_ports(), ["4419", "4430", "5000"])
        self.run_fn("ufw_sync_portal_ports", stdin="y\n")
        self.assertEqual(self.rule_ports(), ["4430", "5000"])

    def test_quiet_mode_adds_without_asking_and_never_deletes(self):
        self.make_bot("a", 4430)
        self.rules.write_text("4419|StreamerBot portal a\n")
        self.run_fn("ufw_sync_portal_ports quiet")
        self.assertEqual(self.rule_ports(), ["4419", "4430"])

    def test_quiet_mode_leaves_an_inactive_ufw_and_a_missing_one_silent(self):
        self.make_bot("a", 4419)
        r = self.run_fn("ufw_sync_portal_ports quiet", state="inactive")
        self.assertEqual((r.stdout, self.rule_ports()), ("", []))
        (self.bin / "ufw").unlink()
        r = self.run_fn("ufw_sync_portal_ports quiet", stub=False)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
