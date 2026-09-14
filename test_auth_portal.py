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


def make_portal(youtube_bridge=None, youtube_session=None):
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
        youtube_session=youtube_session,
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
        self.assertIn("Connect Spotify", title)

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


class YouTubePortalPageTests(TestCase):
    """Phase 9 pages, pinned to the accessibility review made before they were built."""

    def setUp(self):
        self.pages = PageBuilder(FakeTranslator(), "en")

    @staticmethod
    def title(html):
        return html.split("<title>")[1].split("</title>")[0]

    def test_the_youtube_sign_in_names_the_google_account_not_a_youtube_password(self):
        html = self.pages.credentials_page("tok", "yt")

        self.assertIn(">Google account email address</label>", html)
        self.assertIn(">Google account password</label>", html)
        self.assertNotIn("YouTube password", html)

    def test_the_separate_account_advice_is_a_heading_before_the_form_and_takes_no_focus(self):
        html = self.pages.credentials_page("tok", "yt")

        self.assertIn("<h2>Use a separate Google account</h2>", html)
        self.assertLess(html.index("Use a separate Google account"), html.index("<form"))
        notice = html[html.index("<h2>Use a separate"):html.index("<form")]
        for forbidden in ('role="', "aria-live", "tabindex"):
            self.assertNotIn(forbidden, notice)

    def test_the_email_hint_repeats_the_advice_for_people_who_jump_to_the_field(self):
        html = self.pages.credentials_page("tok", "yt")

        self.assertIn("not your personal one", html[html.index('id="username-hint"'):])

    def test_the_error_summary_still_comes_straight_after_the_heading(self):
        html = self.pages.credentials_page("tok", "yt", errors=[("password", "x")])

        self.assertLess(html.index('id="error-summary"'), html.index("Use a separate Google account"))

    def test_other_services_are_unchanged(self):
        html = self.pages.credentials_page("tok", "nf")

        self.assertNotIn("Google", html)

    def test_the_match_number_is_a_readonly_input_read_as_a_number(self):
        """The phone's screen reader says eighty-eight; spelling it 8, 8 would not match."""
        html = self.pages.approval_page("tok", number="88")

        self.assertIn('id="match-number"', html)
        self.assertIn('value="88"', html)
        self.assertIn("readonly", html)
        self.assertNotIn("disabled", html)
        self.assertNotIn("8, 8", html)
        self.assertNotIn("88", self.title(html))

    def test_no_number_means_no_number_field(self):
        self.assertNotIn("match-number", self.pages.approval_page("tok"))

    def test_googles_words_are_marked_as_english(self):
        html = self.pages.approval_page("tok", heading="Check your Pixel 8")

        self.assertIn('<p lang="en">Check your Pixel 8</p>', html)

    def test_approval_is_a_post_button_with_import_and_cancel_as_links(self):
        html = self.pages.approval_page("tok")

        self.assertIn('<form method="post" action="/approve/yt', html)
        self.assertIn(">I have approved it on my phone</button>", html)
        self.assertIn('<a href="/import/yt?t=tok">Import a YouTube session</a>', html)
        self.assertIn('<a href="/connect/yt/cancel?t=tok">', html)
        self.assertNotIn("http-equiv", html)

    def test_coming_back_unapproved_changes_the_title(self):
        first = self.title(self.pages.approval_page("tok"))
        again = self.title(self.pages.approval_page("tok", not_yet=True))

        self.assertNotEqual(first, again)
        self.assertTrue(again.startswith("Not approved yet"))

    def test_the_import_page_offers_a_file_before_the_paste_box(self):
        html = self.pages.import_page("tok")

        self.assertIn('enctype="multipart/form-data"', html)
        self.assertIn('type="file"', html)
        self.assertLess(html.index('id="cookies-file"'), html.index('id="cookies"'))
        self.assertIn("<ol", html)

    def test_the_paste_box_does_not_send_a_credential_to_spellcheck_or_grammarly(self):
        html = self.pages.import_page("tok")
        textarea = html[html.index("<textarea"):html.index("</textarea>")]

        for attribute in ('spellcheck="false"', 'autocomplete="off"', 'data-gramm="false"',
                          'data-enable-grammarly="false"', 'dir="ltr"', 'wrap="off"',
                          'aria-describedby="cookies-hint"'):
            self.assertIn(attribute, textarea)
        for forbidden in ("maxlength", "placeholder", "required"):
            self.assertNotIn(forbidden, textarea)

    def test_the_paste_status_region_exists_empty(self):
        self.assertIn('<p id="cookies-status" class="hint" role="status"></p>', self.pages.import_page("tok"))

    def test_an_import_error_never_echoes_the_pasted_cookies(self):
        html = self.pages.import_page("tok", [("cookies", "This is not a cookies.txt file.")])

        self.assertIn("></textarea>", html, "the paste box comes back empty")
        self.assertTrue(self.title(html).startswith("Error:"))
        self.assertIn('href="#cookies-file"', html)
        self.assertIn('aria-invalid="true"', html)

    def test_every_import_problem_has_a_sentence(self):
        for value in ("empty", "too_large", "not_cookies", "no_google_session", "session_ended"):
            self.assertNotEqual(self.pages.import_problem_message(value),
                                "The session could not be imported.", value)

    def test_a_disconnected_youtube_row_offers_connect_and_import_with_unique_names(self):
        html = self.pages.status_page("tok", {"yt": "disconnected"})

        self.assertIn(">Connect YouTube</a>", html)
        self.assertIn(">Import a YouTube session</a>", html)

    def test_without_a_browser_only_import_is_offered(self):
        html = self.pages.status_page("tok", {"yt": "disconnected"}, youtube_browser=False)

        self.assertNotIn(">Connect YouTube</a>", html)
        self.assertIn(">Import a YouTube session</a>", html)

    def test_the_youtube_failure_page_puts_import_before_try_again(self):
        html = self.pages.failure_page("tok", "yt", "Google refused.")

        self.assertLess(html.index("Import a YouTube session"), html.index("Try again"))


class FakeKeeper:
    def __init__(self, browser=True):
        self.browser_available = browser
        self.imported = []
        self.result = (True, None)

    def status(self):
        return "disconnected"

    def import_text(self, text):
        self.imported.append(text)
        return self.result

    def sign_out(self):
        pass


class YouTubePortalRoutingTests(TestCase):
    def setUp(self):
        self.keeper = FakeKeeper()
        self.portal = make_portal(youtube_session=self.keeper)
        self.portal.start()
        self.addCleanup(self.portal.close)
        self.base = f"http://127.0.0.1:{self.portal._server.server_address[1]}"
        self.token = self.portal.tokens.mint("tester")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def request(self, path, data=None, headers=None):
        opener = urllib.request.build_opener(self.NoRedirect)
        request = urllib.request.Request(f"{self.base}{path}", data=data, headers=headers or {})
        try:
            with opener.open(request, timeout=10) as r:
                return r.status, r.headers, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8")

    def test_the_old_device_code_address_goes_to_the_new_sign_in(self):
        status, headers, _ = self.request(f"/youtube?t={self.token}")

        self.assertEqual(status, 303)
        self.assertTrue(headers["location"].startswith("/connect/yt"))

    def test_without_a_browser_connect_goes_straight_to_import(self):
        self.keeper.browser_available = False

        status, headers, _ = self.request(f"/connect/yt?t={self.token}")

        self.assertEqual(status, 303)
        self.assertTrue(headers["location"].startswith("/import/yt"))

    def test_a_pasted_import_is_handed_to_the_keeper(self):
        data = urllib.parse.urlencode({"t": self.token, "cookies": "pasted text"}).encode()

        status, headers, _ = self.request("/import/yt", data=data)

        self.assertEqual(status, 303)
        self.assertTrue(headers["location"].startswith("/success/yt"))
        self.assertEqual(self.keeper.imported, ["pasted text"])

    def test_an_uploaded_file_wins_over_the_paste_box(self):
        boundary = "----streamerbot"
        crlf = chr(13) + chr(10)
        parts = [
            f"--{boundary}", 'Content-Disposition: form-data; name="t"', "", self.token,
            f"--{boundary}", 'Content-Disposition: form-data; name="cookies_file"; filename="cookies.txt"',
            "Content-Type: text/plain", "", "file text",
            f"--{boundary}", 'Content-Disposition: form-data; name="cookies"', "", "pasted text",
            f"--{boundary}--", "",
        ]
        body = crlf.join(parts).encode()

        status, _, _ = self.request(
            f"/import/yt?t={self.token}", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

        self.assertEqual(status, 303)
        self.assertEqual(self.keeper.imported, ["file text"])

    def test_a_rejected_import_re_renders_with_the_reason_and_without_the_text(self):
        from bot.modules.youtube_session_keeper import ImportProblem

        self.keeper.result = (False, ImportProblem.NotCookies)
        data = urllib.parse.urlencode({"t": self.token, "cookies": "SECRET-COOKIE-VALUE"}).encode()

        status, _, body = self.request("/import/yt", data=data)

        self.assertEqual(status, 200)
        self.assertIn("This is not a cookies.txt file.", body)
        self.assertNotIn("SECRET-COOKIE-VALUE", body)

    def test_an_oversized_import_is_an_error_not_a_truncation(self):
        data = urllib.parse.urlencode({"t": self.token, "cookies": "x" * (1200 * 1024)}).encode()

        status, _, body = self.request(f"/import/yt?t={self.token}", data=data)

        self.assertEqual(status, 200)
        self.assertIn("too long to be a cookies file", body)
        self.assertEqual(self.keeper.imported, [])

    def test_approve_without_a_waiting_job_goes_to_progress(self):
        status, headers, _ = self.request(f"/approve/yt?t={self.token}")

        self.assertEqual(status, 303)
        self.assertTrue(headers["location"].startswith("/progress/yt"))


if __name__ == "__main__":
    unittest.main()
