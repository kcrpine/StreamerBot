"""The auth portal: routing, token gating, and the accessibility contract.

The markup assertions are not decoration. Each one pins a decision from the
accessibility review that is invisible in a screenshot and easy to regress: an
aria-hidden device code cannot be copied by a screen reader user, a type=number
OTP field announces as a spinbutton and eats leading zeros, and a GET link that
disconnects an account gets fired by link prefetchers.
"""

import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from unittest import TestCase

from bot.auth.session import AuthJobManager
from bot.auth.store import SecretStore
from bot.modules.auth_portal import AuthPortal
from bot.modules.portal_pages import PageBuilder, spell_out


class FakeTranslator:
    def translate(self, text):
        return text


def make_portal(youtube_bridge=None):
    config = SimpleNamespace(
        enabled=True, host="127.0.0.1", port=0, public_url="", token_ttl=72000
    )
    portal = AuthPortal(
        translator=FakeTranslator(),
        store=SecretStore(tempfile.mkdtemp()),
        jobs=AuthJobManager(),
        config=config,
        locale="en",
        youtube_bridge=youtube_bridge,
    )
    return portal


class SpellOutTests(TestCase):
    def test_symbols_become_words_and_characters_are_comma_separated(self):
        """Comma-space is what forces per-character reading across NVDA/JAWS."""
        self.assertEqual(
            spell_out("BCDF-GHJK", FakeTranslator()),
            "B, C, D, F, dash, G, H, J, K.",
        )


class PageMarkupTests(TestCase):
    def setUp(self):
        self.pages = PageBuilder(FakeTranslator(), "en")

    def test_the_device_code_is_reachable_and_copyable(self):
        """An aria-hidden code is absent from the virtual buffer entirely."""
        html = self.pages.device_code_page("tok", "BCDF-GHJK", "https://www.google.com/device")

        self.assertIn('id="device-code"', html)
        self.assertIn("readonly", html)
        # disabled would remove it from the tab order and stop selection.
        self.assertNotIn("disabled", html)
        self.assertNotIn('class="device-code" aria-hidden', html)
        self.assertIn('aria-describedby="device-code-spelled"', html)
        self.assertIn("B, C, D, F, dash, G, H, J, K.", html)
        # Prevents visual reordering of an LTR code inside an RTL page.
        self.assertIn('dir="ltr"', html)

    def test_the_device_code_is_not_in_the_title(self):
        """Synths mangle BCDF-GHJK as a word, and a title is hard to replay."""
        html = self.pages.device_code_page("tok", "BCDF-GHJK", "https://www.google.com/device")
        title = html.split("<title>")[1].split("</title>")[0]

        self.assertNotIn("BCDF", title)
        self.assertIn("Connect YouTube", title)

    def test_titles_are_front_loaded_with_the_app_name_last(self):
        html = self.pages.success_page("tok", "nf")
        title = html.split("<title>")[1].split("</title>")[0]

        self.assertTrue(title.startswith("Netflix connected"), title)
        self.assertTrue(title.endswith("- StreamerBot"), title)

    def test_an_error_page_title_is_prefixed(self):
        html = self.pages.credentials_page(
            "tok", "nf", errors=[("password", "Enter your Netflix password")]
        )
        title = html.split("<title>")[1].split("</title>")[0]

        self.assertTrue(title.startswith("Error:"), title)

    def test_service_rows_are_h2_not_h3(self):
        """h1 -> h3 skips a level and breaks the screen reader's outline."""
        html = self.pages.status_page("tok", {"nf": "disconnected"})

        self.assertIn("<h2>Netflix</h2>", html)
        self.assertNotIn("<h3>", html)

    def test_the_service_name_is_visible_in_the_control(self):
        """A hidden span risks "ConnectNetflix" and breaks translation."""
        html = self.pages.status_page("tok", {"nf": "disconnected"})

        self.assertIn(">Connect Netflix</a>", html)
        self.assertNotIn('Connect<span class="visually-hidden"', html)

    def test_connect_and_disconnect_navigate_so_they_are_links(self):
        html = self.pages.status_page("tok", {"nf": "connected"})

        self.assertIn('<a class="button" href="/disconnect/nf/confirm', html)

    def test_disconnect_is_a_post_button_not_a_get_link(self):
        """A GET that deletes data is fired by prefetchers and AV proxies."""
        html = self.pages.disconnect_confirm_page("tok", "nf")

        self.assertIn('<form method="post" action="/disconnect/nf', html)
        self.assertIn('<button type="submit"', html)

    def test_status_text_stands_alone_without_colour_or_icon(self):
        html = self.pages.status_page("tok", {"nf": "disconnected"})

        self.assertIn("Not connected", html)
        self.assertIn('aria-hidden="true"', html)

    def test_the_otp_field_is_text_with_numeric_inputmode(self):
        """type=number announces as a spinbutton and drops leading zeros."""
        html = self.pages.otp_page("tok", "nf")

        self.assertIn('id="otp"', html)
        self.assertIn('type="text"', html)
        self.assertIn('inputmode="numeric"', html)
        self.assertIn('autocomplete="one-time-code"', html)
        self.assertNotIn('type="number"', html)
        # A pasted code with a trailing space would be silently truncated.
        self.assertNotIn("maxlength", html)

    def test_the_password_field_has_no_maxlength_and_is_never_repopulated(self):
        html = self.pages.credentials_page("tok", "nf", username="a@example.com")

        self.assertIn('autocomplete="current-password"', html)
        self.assertNotIn("maxlength", html)
        # SC 3.3.7: username comes back, password never does.
        self.assertIn('value="a@example.com"', html)
        self.assertNotIn('type="password" value=', html)

    def test_the_otp_page_shows_the_username_rather_than_asking_again(self):
        html = self.pages.otp_page("tok", "nf", username="a@example.com")

        self.assertIn("a@example.com", html)

    def test_forms_are_novalidate_but_keep_required(self):
        html = self.pages.credentials_page("tok", "nf")

        self.assertIn("novalidate", html)
        self.assertIn("required", html)

    def test_the_error_summary_focuses_itself_without_role_alert(self):
        """role=alert on a page load double-announces in JAWS."""
        html = self.pages.credentials_page(
            "tok", "nf", errors=[("password", "Enter your Netflix password")]
        )

        self.assertIn('id="error-summary"', html)
        self.assertIn('tabindex="-1"', html)
        self.assertIn('href="#password"', html)
        self.assertNotIn('role="alert"', html)

    def test_summary_and_field_error_text_are_identical(self):
        message = "Enter your Netflix password"
        html = self.pages.credentials_page("tok", "nf", errors=[("password", message)])

        self.assertIn(f'<a href="#password">{message}</a>', html)
        self.assertIn(f'<span class="visually-hidden">Error: </span>{message}', html)

    def test_a_field_in_error_is_marked_invalid_and_described_by_it(self):
        html = self.pages.credentials_page("tok", "nf", errors=[("password", "x")])

        self.assertIn('aria-invalid="true"', html)
        self.assertIn('aria-describedby="password-error"', html)

    def test_a_clean_field_is_not_marked_invalid_at_all(self):
        html = self.pages.credentials_page("tok", "nf")

        self.assertNotIn("aria-invalid", html)

    def test_the_progress_live_region_exists_empty_in_the_initial_html(self):
        """A live region injected with its text is not reliably announced."""
        html = self.pages.progress_page("tok", "nf")

        self.assertIn('<p id="poll-status" role="status"></p>', html)
        # role=status already implies both.
        self.assertNotIn("aria-live", html)

    def test_no_meta_refresh_anywhere(self):
        for html in (
            self.pages.progress_page("tok", "nf"),
            self.pages.device_code_page("tok", "AB-CD", "https://x.test"),
        ):
            self.assertNotIn("http-equiv", html)

    def test_the_token_is_not_leaked_to_third_parties_via_referer(self):
        html = self.pages.device_code_page("tok", "AB-CD", "https://www.google.com/device")

        self.assertIn('<meta name="referrer" content="no-referrer">', html)

    def test_error_pages_carry_a_recovery_path(self):
        """A bare 404 is a dead end for someone who cannot inspect the URL."""
        for html in (self.pages.not_found_page(), self.pages.expired_page()):
            self.assertIn("li command", html)

    def test_visually_hidden_uses_clip_path_not_display_none(self):
        html = self.pages.status_page("tok", {"nf": "disconnected"})

        self.assertIn("clip-path:inset(50%)", html)

    def test_rtl_locales_set_the_document_direction(self):
        arabic = PageBuilder(FakeTranslator(), "ar")

        self.assertIn('dir="rtl"', arabic.status_page("tok", {"nf": "disconnected"}))
        self.assertIn('lang="ar"', arabic.status_page("tok", {"nf": "disconnected"}))

    def test_user_input_is_escaped(self):
        html = self.pages.credentials_page("tok", "nf", username='"><script>x</script>')

        self.assertNotIn("<script>x</script>", html)


class PortalRoutingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.portal = make_portal()
        cls.portal.config.port = 0
        cls.portal.start()
        cls.base = f"http://127.0.0.1:{cls.portal._server.server_address[1]}"
        cls.token = cls.portal.tokens.mint("tester")

    @classmethod
    def tearDownClass(cls):
        cls.portal.close()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"{self.base}{path}", timeout=10) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_no_token_is_404_not_403(self):
        """403 confirms there is something here worth attacking."""
        status, body = self.get("/")

        self.assertEqual(status, 404)
        self.assertIn("Page not found", body)

    def test_a_wrong_token_is_410_with_recovery_advice(self):
        status, body = self.get("/?t=not-a-real-token")

        self.assertEqual(status, 410)
        self.assertIn("li command", body)

    def test_a_valid_token_reaches_the_status_page(self):
        status, body = self.get(f"/?t={self.token}")

        self.assertEqual(status, 200)
        self.assertIn("Your streaming accounts", body)

    def test_an_unknown_service_is_refused(self):
        status, _ = self.get(f"/connect/nonsense?t={self.token}")

        self.assertEqual(status, 404)

    def test_security_headers_are_set(self):
        with urllib.request.urlopen(f"{self.base}/?t={self.token}", timeout=10) as r:
            headers = {k.lower(): v for k, v in r.headers.items()}

        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    def test_the_credentials_form_renders_for_a_real_service(self):
        status, body = self.get(f"/connect/nf?t={self.token}")

        self.assertEqual(status, 200)
        self.assertIn('autocomplete="current-password"', body)

    def test_a_submit_with_no_password_re_renders_with_errors(self):
        data = urllib.parse.urlencode(
            {"t": self.token, "username": "a@example.com", "password": ""}
        ).encode()
        request = urllib.request.Request(f"{self.base}/connect/nf", data=data)
        with urllib.request.urlopen(request, timeout=10) as r:
            body = r.read().decode("utf-8")

        self.assertIn("error-summary", body)
        self.assertIn("a@example.com", body)


if __name__ == "__main__":
    unittest.main()
