import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
SCRIPT = ROOT / "youtube_egress.sh"


def run_egress(body: str, config_dir: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Source youtube_egress.sh against a scratch config directory and run `body`.

    Nothing here touches Docker or the real youtube_proxy.env: SCRIPT_DIR points
    at a temporary directory, and only pure functions are called.
    """
    if not SCRIPT.is_file():
        raise unittest.SkipTest("youtube_egress.sh is not present; host-only test skipped")
    env = {**os.environ, "SCRIPT_DIR": config_dir, "BOTS_ROOT": config_dir}
    return subprocess.run(
        ["bash", "-c", f'. "{SCRIPT}"; {body}'],
        input=stdin, capture_output=True, text=True, env=env, timeout=30,
    )


class EgressConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_default_is_direct_when_nothing_is_configured(self):
        self.assertEqual(run_egress("egress_mode", self.dir).stdout.strip(), "direct")

    def test_settings_file_is_private_and_round_trips(self):
        # The proxy URL can carry credentials, so the file must not be world-readable.
        run_egress('egress_write_env url "http://user:pw@proxy.example:3128"', self.dir)
        path = Path(self.dir) / "youtube_proxy.env"
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
        self.assertEqual(run_egress("egress_mode", self.dir).stdout.strip(), "url")
        self.assertIn("YOUTUBE_PROXY_URL=http://user:pw@proxy.example:3128", path.read_text())

    def test_an_address_already_used_is_remembered_and_the_log_is_bounded(self):
        # Rotation must never accept an address it already tried.
        out = run_egress(
            'for i in $(seq 1 30); do egress_remember_ip "10.0.0.$i"; done; '
            'egress_ip_seen 10.0.0.30 && echo recent-seen; '
            'egress_ip_seen 10.0.0.1 || echo old-forgotten; '
            'wc -l < "$EGRESS_IP_LOG"',
            self.dir,
        ).stdout.split()
        self.assertEqual(out, ["recent-seen", "old-forgotten", "20"])

    def test_rotating_is_refused_when_there_is_nothing_to_rotate(self):
        result = run_egress("egress_rotate", self.dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VPN or WARP", result.stdout)


class EgressMenuTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_menu_offers_warp_vpn_own_proxy_and_account_help(self):
        out = run_egress("youtube_egress_menu", self._tmp.name, stdin="0\n").stdout
        for expected in ("Cloudflare WARP", "VPN container", "My own proxy", "I need a VPN account"):
            self.assertIn(expected, out)

    def test_account_help_names_the_steps_and_the_free_alternative(self):
        out = run_egress("egress_account_help", self._tmp.name).stdout
        for expected in ("Mullvad", "ProtonVPN", "NordVPN", "WireGuard", "OpenVPN", "WARP", "free"):
            self.assertIn(expected, out)
        # Numbered plain lines, not boxes or colour, so a screen reader reads them in order.
        self.assertIn("1. Pick a provider", out)
        self.assertNotIn("\x1b[", out)


if __name__ == "__main__":
    unittest.main()
