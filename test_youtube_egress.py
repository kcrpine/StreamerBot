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


class VpnFailureTests(unittest.TestCase):
    """The setup must say which kind of failure it saw, using a stubbed `docker`."""

    SILENT = "TLS Error: TLS key negotiation failed to occur within 60 seconds"
    REJECTED = "AUTH: Received control message: AUTH_FAILED"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _run(self, log: str, call: str) -> subprocess.CompletedProcess:
        return run_egress(f"docker() {{ printf '%s\\n' '{log}'; }}; {call}", self._tmp.name)

    def test_silent_server_is_not_called_a_password_problem(self):
        out = self._run(self.SILENT, "egress_vpn_explain_failure").stdout
        self.assertIn("did not answer", out)
        self.assertIn("not a password problem", out)
        self.assertNotIn("rejected", out)

    def test_rejected_login_is_named_as_such(self):
        out = self._run(self.REJECTED, "egress_vpn_explain_failure").stdout
        self.assertIn("rejected the username or password", out)

    def test_only_a_silent_server_is_retried_over_tcp(self):
        self.assertEqual(self._run(self.SILENT, "egress_vpn_server_silent").returncode, 0)
        self.assertNotEqual(self._run(self.REJECTED, "egress_vpn_server_silent").returncode, 0)
        both = f"{self.SILENT}; {self.REJECTED}"
        self.assertNotEqual(self._run(both, "egress_vpn_server_silent").returncode, 0)

    def test_expressvpn_is_not_retried_over_tcp(self):
        self.assertNotEqual(run_egress("egress_vpn_can_try_tcp expressvpn", self._tmp.name).returncode, 0)
        self.assertEqual(run_egress("egress_vpn_can_try_tcp protonvpn", self._tmp.name).returncode, 0)

    def test_giving_up_resets_the_saved_setting_to_direct(self):
        run_egress('egress_write_env vpn "http://172.17.0.1:8888"', self._tmp.name)
        out = self._run("", "egress_vpn_give_up; egress_mode")
        self.assertEqual(out.stdout.strip().splitlines()[-1], "direct")

    def test_proton_help_uses_wireguard_and_free_servers(self):
        out = run_egress("egress_account_help", self._tmp.name).stdout
        self.assertIn("WireGuard configuration", out)
        self.assertIn("Free server", out)


if __name__ == "__main__":
    unittest.main()
