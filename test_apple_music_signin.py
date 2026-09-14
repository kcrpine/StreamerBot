"""Apple Music sign-in: finding the form a person actually sees.

Measured against Apple's live page on 13 September 2026, in the bot's own Chrome:
music.apple.com/login opens a dialog that spins for 10 to 20 seconds, then shows
"Continue with Email" — one Email box and a Continue button — in the frame
music.apple.com/includes/commerce/authenticate. Inside that frame sits Apple's
own sign-in form on idmsa.apple.com, which at this step is **zero pixels tall**,
with the password box's container aria-hidden and the sign-in button disabled.
Playwright still reports those hidden fields as visible.

The old adapter took the first frame whose URL contained "auth", gave up after
10 seconds, and on the live page picked a frame with no form in it at all. Every
attempt ended in "The Apple sign-in form did not appear."

These tests pin the rules without launching Chrome. The steps after Continue —
password, two-factor, "trust this browser" — could not be observed without a real
Apple account, so they are pinned by behaviour here and need a real sign-in to
confirm.
"""

import unittest
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from bot.auth.session import OTP_TIMEOUT_SECONDS
from bot.services.web.apple_music import AppleMusicAdapter


def frame(url, parent=None, box=None):
    f = MagicMock()
    f.url = url
    f.parent_frame = parent
    if parent is not None:
        f.frame_element.return_value.bounding_box.return_value = box
    return f


class FrameOrderTests(TestCase):
    def test_apples_sign_in_host_is_searched_first(self):
        top = frame("https://music.apple.com/us/login")
        proxy = frame("https://music.apple.com/includes/commerce/fetch-proxy.html?devToken=x")
        outer = frame("https://music.apple.com/includes/commerce/authenticate")
        idmsa = frame("https://idmsa.apple.com/appleauth/auth/authorize/signin")

        ordered = AppleMusicAdapter.order_frames_for_signin([top, proxy, outer, idmsa])

        self.assertIs(ordered[0], idmsa)

    def test_nothing_is_dropped(self):
        """Apple restructures this page. A frame that is not the expected host
        must still be searched, or the next redesign is a dead end again."""
        frames = [frame(f"https://example.com/{n}") for n in range(4)]

        self.assertEqual(len(AppleMusicAdapter.order_frames_for_signin(frames)), 4)

    def test_the_old_rule_picked_a_frame_with_no_form(self):
        """Pinned so nobody reintroduces it: "auth" appears in URLs that are not
        the sign-in form."""
        proxy = "https://music.apple.com/includes/commerce/fetch-proxy.html?devToken=eyJhbGciOiJ-authx"
        old_rule = "appleid" in proxy or "auth" in proxy
        self.assertTrue(old_rule)
        self.assertNotIn(AppleMusicAdapter.SIGNIN_FRAME_HOST, proxy)


class SeenByAPersonTests(TestCase):
    """Playwright's "visible" is not the same as exposed to a person."""

    def setUp(self):
        self.adapter = AppleMusicAdapter()

    def test_a_zero_height_iframe_hides_everything_inside_it(self):
        """The measured trap: Apple's sign-in iframe is 0px tall at step one."""
        top = frame("https://music.apple.com/us/login")
        outer = frame("https://music.apple.com/includes/commerce/authenticate",
                      parent=top, box={"x": 0, "y": 0, "width": 1280, "height": 720})
        inner = frame("https://idmsa.apple.com/appleauth/auth/authorize/signin",
                      parent=outer, box={"x": 340, "y": 320, "width": 600, "height": 0})

        self.assertFalse(AppleMusicAdapter._frame_chain_has_size(inner))
        self.assertTrue(AppleMusicAdapter._frame_chain_has_size(outer))

    def test_the_top_page_needs_no_iframe_to_have_size(self):
        self.assertTrue(AppleMusicAdapter._frame_chain_has_size(frame("https://music.apple.com")))

    def test_an_aria_hidden_field_is_not_seen(self):
        """Apple marks the not-yet-reached password step aria-hidden. A screen
        reader honours that, so the bot does too."""
        top = frame("https://music.apple.com")
        field = MagicMock()
        field.is_visible.return_value = True
        field.evaluate.return_value = False  # NOT_HIDDEN_JS says it is hidden

        self.assertFalse(self.adapter._person_can_see(top, field))

    def test_a_field_that_is_exposed_is_seen(self):
        top = frame("https://music.apple.com")
        field = MagicMock()
        field.is_visible.return_value = True
        field.evaluate.return_value = True

        self.assertTrue(self.adapter._person_can_see(top, field))

    def test_the_hidden_check_covers_all_three_ways_apple_hides_a_step(self):
        js = AppleMusicAdapter.NOT_HIDDEN_JS
        self.assertIn('aria-hidden', js)
        self.assertIn("tabindex", js)
        self.assertIn("inert", js)


class ContinueTests(TestCase):
    def setUp(self):
        self.adapter = AppleMusicAdapter()

    def test_continue_is_pressed_in_the_fields_own_frame(self):
        """Searching the whole page would find the disabled sign-in button on
        Apple's hidden form next door."""
        top = frame("https://music.apple.com")
        button = MagicMock()
        button.is_visible.return_value = True
        button.evaluate.return_value = True
        button.is_enabled.return_value = True
        loc = MagicMock()
        loc.count.return_value = 1
        loc.nth.return_value = button
        top.locator.return_value = loc
        field = MagicMock()

        self.adapter._press_continue(top, field)

        button.click.assert_called_once()
        field.press.assert_not_called()

    def test_enter_is_the_fallback_when_there_is_no_button(self):
        top = frame("https://music.apple.com")
        loc = MagicMock()
        loc.count.return_value = 0
        top.locator.return_value = loc
        field = MagicMock()

        self.adapter._press_continue(top, field)

        field.press.assert_called_once_with("Enter")

    def test_a_disabled_button_is_not_pressed(self):
        top = frame("https://music.apple.com")
        button = MagicMock()
        button.is_visible.return_value = True
        button.evaluate.return_value = True
        button.is_enabled.return_value = False
        loc = MagicMock()
        loc.count.return_value = 1
        loc.nth.return_value = button
        top.locator.return_value = loc
        field = MagicMock()

        self.adapter._press_continue(top, field)

        button.click.assert_not_called()
        field.press.assert_called_once_with("Enter")


def fake_page():
    page = MagicMock()
    page.frames = []
    return page


class LoginFlowTests(TestCase):
    def setUp(self):
        self.adapter = AppleMusicAdapter()
        self.adapter.STEP_WAIT_MS = 50
        self.adapter.AFTER_PASSWORD_WAIT_MS = 50
        self.job = MagicMock()

    def test_a_form_that_never_loads_says_so_and_suggests_trying_again(self):
        with patch.object(self.adapter, "_find_visible", return_value=(None, None)):
            self.adapter.login(fake_page(), "user@example.com", "pw", self.job)

        reason = self.job.fail.call_args.args[0]
        self.assertIn("did not load within a minute", reason)
        self.assertIn("try again", reason)

    def test_the_form_is_given_a_full_minute(self):
        """Measured spinning for up to 20 seconds. Ten was the old limit."""
        self.assertGreaterEqual(AppleMusicAdapter.FORM_WAIT_MS, 60000)

    def test_an_unrecognised_email_is_explained(self):
        """Apple offers to create an account for an address it does not know,
        so a missing password step most likely means the wrong address."""
        outer = frame("https://music.apple.com/includes/commerce/authenticate")
        email_box = MagicMock()

        def finder(page, selectors, timeout):
            if selectors is self.adapter.USERNAME_SELECTORS:
                return outer, email_box
            return None, None

        with patch.object(self.adapter, "_find_visible", side_effect=finder), \
             patch.object(self.adapter, "_press_continue"), \
             patch.object(self.adapter, "_visible_error_text", return_value=""):
            self.adapter.login(fake_page(), "user@example.com", "pw", self.job)

        reason = self.job.fail.call_args.args[0]
        self.assertIn("email address of your Apple Account", reason)
        email_box.fill.assert_called_once_with("user@example.com")

    def test_apples_own_error_message_is_passed_on(self):
        outer = frame("https://music.apple.com/includes/commerce/authenticate")

        def finder(page, selectors, timeout):
            if selectors is self.adapter.USERNAME_SELECTORS:
                return outer, MagicMock()
            return None, None

        with patch.object(self.adapter, "_find_visible", side_effect=finder), \
             patch.object(self.adapter, "_press_continue"), \
             patch.object(self.adapter, "_visible_error_text",
                          return_value="This Apple Account has been locked."):
            self.adapter.login(fake_page(), "user@example.com", "pw", self.job)

        self.assertIn("has been locked", self.job.fail.call_args.args[0])

    def test_no_password_is_ever_put_in_a_failure_reason(self):
        """Failure reasons reach the portal page and the log."""
        with patch.object(self.adapter, "_find_visible", return_value=(None, None)):
            self.adapter.login(fake_page(), "user@example.com", "hunter2-secret", self.job)

        self.assertNotIn("hunter2-secret", self.job.fail.call_args.args[0])

    def test_a_two_factor_prompt_asks_the_portal_for_the_code(self):
        outer = frame("https://music.apple.com/includes/commerce/authenticate")
        idmsa = frame("https://idmsa.apple.com/appleauth/auth/authorize/signin")
        password_box, code_box = MagicMock(), MagicMock()
        self.job.request_otp.return_value = None  # the portal timed out

        def finder(page, selectors, timeout):
            if selectors is self.adapter.USERNAME_SELECTORS:
                return outer, MagicMock()
            if selectors is self.adapter.PASSWORD_SELECTORS:
                return idmsa, password_box
            if selectors is self.adapter.OTP_INPUT_SELECTORS:
                return idmsa, code_box
            return None, None

        self.adapter.AFTER_PASSWORD_WAIT_MS = 5000
        with patch.object(self.adapter, "_find_visible", side_effect=finder), \
             patch.object(self.adapter, "_press_continue"), \
             patch.object(self.adapter, "_visible_error_text", return_value=""), \
             patch.object(self.adapter, "_authorized_here", return_value=False):
            self.adapter.login(fake_page(), "user@example.com", "pw", self.job)

        password_box.fill.assert_called_once_with("pw")
        self.job.request_otp.assert_called_once()
        self.assertIn("Apple devices", self.job.request_otp.call_args.args[0])


class CodeEntryTests(TestCase):
    def setUp(self):
        self.adapter = AppleMusicAdapter()

    def page_with_boxes(self, count):
        f = frame("https://idmsa.apple.com/appleauth/auth/authorize/signin")
        loc = MagicMock()
        loc.count.return_value = count
        f.locator.return_value = loc
        return MagicMock(), f

    def test_split_boxes_are_typed_into_not_filled(self):
        """Filling the first of six single-character boxes with the whole code
        leaves five empty."""
        page, f = self.page_with_boxes(6)
        first = MagicMock()
        with patch.object(self.adapter, "_find_visible", return_value=(f, first)):
            self.adapter._enter_code(page, first, "123 456")

        first.click.assert_called_once()
        page.keyboard.type.assert_called_once()
        self.assertEqual(page.keyboard.type.call_args.args[0], "123456")
        first.fill.assert_not_called()

    def test_a_single_box_is_filled(self):
        page, f = self.page_with_boxes(1)
        box = MagicMock()
        with patch.object(self.adapter, "_find_visible", return_value=(f, box)):
            self.adapter._enter_code(page, box, "123456")

        box.fill.assert_called_once_with("123456")


class TimeBudgetTests(TestCase):
    def test_the_whole_sign_in_outlasts_its_slowest_honest_steps(self):
        """At 300 seconds the outer limit gave up on someone still typing their
        code, because the code alone may wait OTP_TIMEOUT_SECONDS."""
        import inspect
        from bot.player.engines import browser_engine

        source = inspect.getsource(browser_engine.BrowserEngine.login)
        import re
        timeout = int(re.search(r"timeout=(\d+)", source).group(1))
        slowest = (
            AppleMusicAdapter.FORM_WAIT_MS / 1000
            + AppleMusicAdapter.STEP_WAIT_MS / 1000
            + OTP_TIMEOUT_SECONDS
            + AppleMusicAdapter.AFTER_PASSWORD_WAIT_MS / 1000
        )
        self.assertGreater(timeout, slowest)


if __name__ == "__main__":
    unittest.main()
