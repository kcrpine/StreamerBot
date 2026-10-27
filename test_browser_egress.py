"""The Disney Plus and Paramount Plus proxy: its menu, its settings file, and the refused-address message."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).parent
SCRIPT = ROOT / "browser_egress.sh"


def run_menu(body: str, config_dir: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Source browser_egress.sh against a scratch directory and run `body`.

    SCRIPT_DIR points at a temporary directory and the recreate helpers are
    stubbed, so nothing touches Docker or a real browser_proxy.env.
    """
    if not SCRIPT.is_file() or shutil.which("bash") is None:
        raise unittest.SkipTest("browser_egress.sh or bash is not available")
    env = {**os.environ, "SCRIPT_DIR": config_dir}
    stubs = 'recreate_bot_containers() { echo RECREATED; }; cli_for_each_bot() { echo "STARTED $1"; };'
    return subprocess.run(
        ["bash", "-c", f'{stubs} . "{SCRIPT}"; {body}'],
        input=stdin, capture_output=True, text=True, env=env, timeout=30,
    )


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_the_file_is_private_and_holds_both_settings(self):
        # The proxy address carries a password, so the file must not be world-readable.
        run_menu('browser_proxy_write_env "http://u:pw@proxy.example:8080" "dp,pp"', self.dir)
        path = Path(self.dir) / "browser_proxy.env"
        if os.name != "nt":
            self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
        text = path.read_text()
        self.assertIn("BROWSER_PROXY_URL=http://u:pw@proxy.example:8080", text)
        self.assertIn("BROWSER_PROXY_SERVICES=dp,pp", text)

    def test_the_status_never_shows_the_password(self):
        out = run_menu(
            'BROWSER_PROXY_URL="http://u:secretpw@proxy.example:8080"; browser_proxy_status', self.dir
        ).stdout
        self.assertIn("http://proxy.example:8080", out)
        self.assertNotIn("secretpw", out)

    def test_a_socks5_address_is_refused_with_the_reason(self):
        result = run_menu("browser_proxy_setup", self.dir, stdin="socks5://u:p@h:1080\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SOCKS5", result.stdout)
        self.assertFalse((Path(self.dir) / "browser_proxy.env").exists())

    def test_turning_it_off_removes_the_file_and_recreates_the_bots(self):
        run_menu('browser_proxy_write_env "http://u:pw@h:1" "dp"', self.dir)
        out = run_menu("browser_proxy_off", self.dir).stdout
        self.assertFalse((Path(self.dir) / "browser_proxy.env").exists())
        self.assertIn("RECREATED", out)
        self.assertIn("STARTED start", out)


class MenuTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_the_menu_offers_advice_setup_and_off(self):
        out = run_menu("browser_egress_menu", self._tmp.name, stdin="0\n").stdout
        for expected in ("Which proxy should I buy?", "Set the proxy", "Turn the proxy off"):
            self.assertIn(expected, out)

    def test_the_advice_names_providers_prices_and_the_bandwidth_trap(self):
        out = run_menu("browser_proxy_help", self._tmp.name).stdout
        for expected in ("IPRoyal", "Proxy-Seller", "Webshare", "dollars", "per gigabyte",
                         "same country", "SOCKS5"):
            self.assertIn(expected, out)
        # Plain numbered lines, no colour codes, so a screen reader reads them in order.
        self.assertIn("1. IPRoyal", out)
        self.assertNotIn("\x1b[", out)

    def test_streamerbot_sh_sources_the_menu_and_offers_it(self):
        text = (ROOT / "streamerbot.sh").read_text(encoding="utf-8")
        self.assertIn('. "$SCRIPT_DIR/browser_egress.sh"', text)
        self.assertIn("Disney Plus and Paramount Plus Proxy", text)
        self.assertIn("browser_egress_menu", text)


class RefusedAddressTests(unittest.TestCase):
    """A site refusing the server must not be reported as a wrong password or a changed page."""

    def adapter(self):
        try:
            from bot.services.web.disney import DisneyAdapter
        except Exception as error:  # the image has the dependencies; a bare host may not
            raise unittest.SkipTest(f"bot package not importable here: {error}")
        return DisneyAdapter()

    def page(self, title, body):
        page = Mock()
        page.title.return_value = title
        page.inner_text.return_value = body
        return page

    def test_disneys_error_page_is_recognised(self):
        reason = self.adapter().refused_address_reason(
            self.page("Login to Disney+", "DisneyPlus | Sorry, an unexpected error has occurred. | Please try again later."),
            "Disney Plus",
        )
        self.assertIn("refused", reason)
        self.assertIn("never sent", reason)
        self.assertIn("option 10", reason)

    def test_paramounts_406_is_recognised(self):
        reason = self.adapter().refused_address_reason(self.page("406 Not Acceptable", "openresty"), "Paramount Plus")
        self.assertTrue(reason.startswith("Paramount Plus refused"))

    def test_an_ordinary_page_is_not_mistaken_for_a_refusal(self):
        self.assertIsNone(self.adapter().refused_address_reason(
            self.page("Login to Disney+", "Enter your email to continue | Email | Continue"), "Disney Plus"))

    def test_a_page_that_cannot_be_read_is_not_a_refusal(self):
        page = Mock()
        page.title.side_effect = RuntimeError("target closed")
        self.assertIsNone(self.adapter().refused_address_reason(page, "Disney Plus"))

    def test_the_disney_sign_in_reports_the_refusal(self):
        adapter = self.adapter()
        page = self.page("Login to Disney+", "Sorry, an unexpected error has occurred.")
        adapter.first_visible = Mock(return_value=None)
        job = Mock()
        adapter.login(page, "a@b.c", "pw", job)
        reason = job.fail.call_args[0][0]
        self.assertIn("refused to show its sign-in page", reason)


if __name__ == "__main__":
    unittest.main()
