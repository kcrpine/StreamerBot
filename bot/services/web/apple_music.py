"""Apple Music, driven through the MusicKit JS object rather than the DOM.

music.apple.com exposes a real player API on the page as `MusicKit`, so this
adapter calls methods instead of clicking buttons it found by class name. That
makes it far and away the most stable of the four browser services: Apple can
restyle the entire site and `MusicKit.getInstance().play()` still works.

The DOM is still needed for one thing — reading search results — because
MusicKit's catalog search needs a developer token this bot does not have. So
search scrapes, and everything else calls the API.

There is no audio description here: this is music, not video. The capability is
declared false rather than left to fail at the point of use.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from bot.services.web import WebServiceAdapter

logger = logging.getLogger(__name__)

HOME = "https://music.apple.com"

# MusicKit is attached asynchronously after the page loads, so every call waits
# for it rather than assuming it is there.
WAIT_FOR_MUSICKIT = (
    "() => !!(window.MusicKit && window.MusicKit.getInstance"
    " && window.MusicKit.getInstance())"
)


class AppleMusicAdapter(WebServiceAdapter):
    name = "am"
    home_url = f"{HOME}/browse"

    LOGIN_URL = f"{HOME}/login"

    USERNAME_SELECTORS = (
        'input#account_name_text_field',
        'input[name="accountName"]',
        'input[type="email"]',
    )
    PASSWORD_SELECTORS = (
        'input#password_text_field',
        'input[name="password"]',
        'input[type="password"]',
    )
    SUBMIT_SELECTORS = (
        'button#sign-in',
        'button[type="submit"]',
        '#continue-password',
    )
    OTP_INPUT_SELECTORS = (
        'input.form-security-code-input',
        'input[autocomplete="one-time-code"]',
        'input[name="char0"]',
        'input[inputmode="numeric"]',
    )
    ERROR_SELECTORS = (
        '#errMsg',
        '.si-error-message',
        '.form-message-wrapper .form-message',
        '[role="alert"]',
    )
    TRUST_BROWSER_SELECTORS = (
        'button:has-text("Trust")',
    )
    # "Continue" is the first step's button on the Continue with Email page; the
    # others are the password step's, on Apple's own sign-in form.
    CONTINUE_SELECTORS = (
        'button:has-text("Continue")',
        'button#sign-in',
        '#continue-password',
        'button[type="submit"]',
    )

    # How Apple Music's sign-in is actually built, measured on 13 September 2026
    # in the bot's own Chrome, because the first version of this adapter was
    # written against a guess and never reached a form:
    #
    # - music.apple.com/login is the ordinary Music page. It opens a dialog that
    #   spins for 10 to 20 seconds before anything can be typed.
    # - The dialog is music.apple.com/includes/commerce/authenticate, titled
    #   "Continue with Email": one Email box (input#accountName) and a Continue
    #   button. That is the first thing a person sees and types into.
    # - Inside it sits idmsa.apple.com/appleauth/auth/authorize/signin, Apple's
    #   own sign-in form, with its own Apple ID and password boxes. At the first
    #   step that iframe is **zero pixels tall**, the password box's container is
    #   aria-hidden="true", the box is tabindex="-1", and the sign-in button is
    #   disabled. Playwright still calls those fields "visible".
    #
    # The old adapter took the first frame whose URL contained "auth" and gave up
    # after 10 seconds. That picked a frame with no form in it at all — measured,
    # it chose .../commerce/fetch-proxy.html, whose long query string happens to
    # contain "auth" — and on a slow load nothing existed yet either way. Both ended
    # in "The Apple sign-in form did not appear." Hence _person_can_see, below,
    # rather than Playwright's looser idea of visible.
    SIGNIN_FRAME_HOST = "idmsa.apple.com"
    FORM_WAIT_MS = 60000
    STEP_WAIT_MS = 30000
    AFTER_PASSWORD_WAIT_MS = 45000

    @classmethod
    def order_frames_for_signin(cls, frames):
        """Frames in the order to search: Apple's sign-in host first.

        Pure on purpose, so the rule that went wrong can be pinned in a test
        without launching Chrome. Nothing is dropped, only reordered, so a page
        Apple restructures still gets searched.
        """
        preferred = [f for f in frames if cls.SIGNIN_FRAME_HOST in (getattr(f, "url", "") or "")]
        rest = [f for f in frames if f not in preferred]
        return preferred + rest

    # Whether an element is exposed to a person rather than merely rendered.
    # Apple hides its not-yet-reached steps with aria-hidden and tabindex=-1, and
    # a screen reader honours both, so this is also the accessibility tree's view.
    NOT_HIDDEN_JS = (
        "(el) => !el.closest('[aria-hidden=\"true\"]')"
        " && el.getAttribute('tabindex') !== '-1'"
        " && !el.closest('[inert]')"
    )

    @staticmethod
    def _frame_chain_has_size(frame) -> bool:
        """Every iframe between this frame and the page has real size.

        The measured trap: Apple's sign-in iframe is zero pixels tall until the
        email step is done, and Playwright reports fields inside it as visible.
        """
        current = frame
        while getattr(current, "parent_frame", None) is not None:
            try:
                box = current.frame_element().bounding_box()
            except Exception:
                return False
            if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
                return False
            current = current.parent_frame
        return True

    def _person_can_see(self, frame, candidate) -> bool:
        try:
            if not candidate.is_visible():
                return False
            if not candidate.evaluate(self.NOT_HIDDEN_JS):
                return False
        except Exception:
            return False
        return self._frame_chain_has_size(frame)

    def _find_visible(self, page, selectors, timeout_ms: int):
        """Poll every frame for the first match a person could see.

        Frames are re-read on every pass because Apple's dialog attaches them
        late and re-navigates them between steps; a list taken once goes stale.
        Locators are used rather than element handles for the same reason.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for frame in self.order_frames_for_signin(list(page.frames)):
                for selector in selectors:
                    try:
                        loc = frame.locator(selector)
                        count = loc.count()
                        for index in range(count):
                            candidate = loc.nth(index)
                            if self._person_can_see(frame, candidate):
                                return frame, candidate
                    except Exception:
                        # A frame that detached mid-search. The next pass
                        # re-reads the list.
                        continue
            if time.monotonic() >= deadline:
                return None, None
            page.wait_for_timeout(500)

    def _press_continue(self, frame, field) -> None:
        """Press the button that belongs to this step, in the field's own frame.

        Searching the frame the field is in, not the whole page, is what stops
        the first step pressing the disabled sign-in button of Apple's hidden
        form next door. Enter is the fallback, as a person would use it.
        """
        for selector in self.CONTINUE_SELECTORS:
            try:
                loc = frame.locator(selector)
                for index in range(loc.count()):
                    button = loc.nth(index)
                    if self._person_can_see(frame, button) and button.is_enabled():
                        button.click()
                        return
            except Exception:
                continue
        try:
            field.press("Enter")
        except Exception:
            pass

    def _visible_error_text(self, page) -> str:
        for frame in self.order_frames_for_signin(list(page.frames)):
            for selector in self.ERROR_SELECTORS:
                try:
                    loc = frame.locator(selector)
                    for index in range(loc.count()):
                        candidate = loc.nth(index)
                        if self._person_can_see(frame, candidate):
                            text = (candidate.inner_text() or "").strip()
                            if text:
                                return text[:200]
                except Exception:
                    continue
        return ""

    def _authorized_here(self, page) -> bool:
        """Whether MusicKit on the current page reports a signed-in user,
        without navigating away from a sign-in that may still be finishing."""
        try:
            return bool(page.evaluate(
                "() => { const M = window.MusicKit;"
                " const k = M && M.getInstance && M.getInstance();"
                " return !!(k && k.isAuthorized); }"
            ))
        except Exception:
            return False

    def _describe_frames(self, page) -> str:
        return ", ".join((getattr(f, "url", "") or "")[:80] for f in page.frames)

    def _musickit_ready(self, page, timeout: int = 30000) -> bool:
        try:
            page.wait_for_function(WAIT_FOR_MUSICKIT, timeout=timeout)
            return True
        except Exception:
            return False

    # -- session -----------------------------------------------------------

    def is_logged_in(self, page) -> bool:
        try:
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as error:
            logger.debug(f"[apple music] navigation failed: {error}")
            return False
        if not self._musickit_ready(page, timeout=20000):
            return False
        try:
            return bool(page.evaluate(
                "() => { const k = window.MusicKit.getInstance();"
                " return !!(k && k.isAuthorized); }"
            ))
        except Exception:
            return False

    def login(self, page, username: str, password: str, job) -> None:
        """Sign in through Apple's own form, including two-factor.

        Nothing here is logged with the username, password or code in it. Frame
        URLs are logged on failure because they are what diagnoses a changed
        page, and they carry no credential.
        """
        from bot.auth.session import AuthState

        job.set_state(AuthState.Launching)
        page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        job.set_state(AuthState.Filling)

        # 1. The email box on "Continue with Email". Allowed a full minute,
        #    because the dialog was measured spinning for up to 20 seconds.
        user_frame, user_field = self._find_visible(page, self.USERNAME_SELECTORS, self.FORM_WAIT_MS)
        if user_field is None:
            logger.warning(
                "[apple music] sign-in form not found after "
                f"{self.FORM_WAIT_MS // 1000}s; frames: {self._describe_frames(page)}"
            )
            job.fail(
                "Apple's sign-in form did not load within a minute. Apple may be "
                "slow right now, so please try again. If it keeps happening, "
                "Apple may have changed its sign-in page."
            )
            return
        user_field.fill(username)
        self._press_continue(user_frame, user_field)

        # 2. The password, on Apple's own form. Present from the start but not
        #    exposed: its container is aria-hidden and its iframe zero pixels tall
        #    until the email is accepted. _find_visible only returns it once a
        #    person could actually see it, which is the signal to type.
        #
        #    Apple's form may show its own Apple ID box at this point too, empty,
        #    rather than carrying the email over. If so it is filled first.
        pass_frame, pass_field = None, None
        deadline = time.monotonic() + self.STEP_WAIT_MS / 1000
        filled_second_id = False
        while time.monotonic() < deadline:
            error = self._visible_error_text(page)
            if error:
                job.fail(f"Apple did not accept that email address: {error}")
                return
            pass_frame, pass_field = self._find_visible(page, self.PASSWORD_SELECTORS, 500)
            if pass_field is not None:
                break
            if not filled_second_id:
                id_frame, id_field = self._find_visible(
                    page, ('input#account_name_text_field',), 500
                )
                if id_field is not None and id_frame is not user_frame:
                    try:
                        if not (id_field.input_value() or "").strip():
                            id_field.fill(username)
                            self._press_continue(id_frame, id_field)
                    except Exception:
                        pass
                    filled_second_id = True
            page.wait_for_timeout(500)
        if pass_field is None:
            job.fail(
                "Apple did not ask for a password. Check that this is the email "
                "address of your Apple Account; Apple offers to create a new "
                "account for an address it does not recognise."
            )
            return
        page.wait_for_timeout(500)
        pass_field.fill(password)
        self._press_continue(pass_frame, pass_field)

        # 3. What Apple does next, watched rather than guessed: two-factor, an
        #    error, a "trust this browser" prompt, or a finished sign-in.
        deadline = time.monotonic() + self.AFTER_PASSWORD_WAIT_MS / 1000
        asked_for_code = False
        while time.monotonic() < deadline:
            if self._authorized_here(page):
                job.succeed()
                return

            error = self._visible_error_text(page)
            if error:
                job.fail(f"Apple did not accept those details: {error}")
                return

            if not asked_for_code:
                _, code_field = self._find_visible(page, self.OTP_INPUT_SELECTORS, 1000)
                if code_field is not None:
                    asked_for_code = True
                    code = job.request_otp(
                        "Apple sent a six digit verification code to your Apple "
                        "devices. Approve the sign-in on one of them, then type "
                        "the code here."
                    )
                    if not code:
                        # request_otp has already failed the job with a reason.
                        return
                    self._enter_code(page, code_field, code)
                    # A code is followed by Apple's own checks, so give the
                    # remainder of the wait a fresh start.
                    deadline = time.monotonic() + self.AFTER_PASSWORD_WAIT_MS / 1000
                    continue

            # Trusting the browser is what lets the bot's saved sign-in last
            # without another code at the next session refresh.
            _, trust = self._find_visible(page, self.TRUST_BROWSER_SELECTORS, 500)
            if trust is not None:
                try:
                    trust.click()
                except Exception:
                    pass

            page.wait_for_timeout(1000)

        # The dialog may close before MusicKit reports it on this page, so the
        # last word is a fresh load of Apple Music.
        if self.is_logged_in(page):
            job.succeed()
            return
        logger.warning(
            "[apple music] sign-in did not complete; "
            f"asked_for_code={asked_for_code} frames: {self._describe_frames(page)}"
        )
        job.fail(
            "Apple Music did not finish signing in. If you approved a code on "
            "your device, please try again."
            if asked_for_code else
            "Apple Music did not accept those details."
        )

    def _enter_code(self, page, first_field, code: str) -> None:
        """Enter a verification code into whichever form Apple is showing.

        Apple's two-factor page has used six single-character boxes that
        advance on their own. Filling only the first with the whole code leaves
        five empty boxes, so for split boxes the code is typed, the way a person
        does. A single box is filled directly.
        """
        digits = "".join(ch for ch in code if ch.isalnum())
        frame, _ = self._find_visible(page, self.OTP_INPUT_SELECTORS, 1000)
        boxes = 1
        if frame is not None:
            for selector in self.OTP_INPUT_SELECTORS:
                try:
                    boxes = max(boxes, frame.locator(selector).count())
                except Exception:
                    continue
        if boxes > 1:
            first_field.click()
            page.keyboard.type(digits, delay=80)
        else:
            first_field.fill(digits)
            try:
                first_field.press("Enter")
            except Exception:
                pass
        page.wait_for_timeout(2000)

    # -- finding things ----------------------------------------------------

    def search(self, page, query: str) -> List[Dict[str, Any]]:
        """Scraped, because MusicKit's catalog search needs a developer token.

        Apple's own ordering is preserved: its result page leads with what it
        considers the best match, and re-sorting that produces worse answers.
        """
        try:
            page.goto(f"{HOME}/search?term={query}", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
        except Exception as error:
            logger.debug(f"[apple music] search navigation failed: {error}")
            return []
        try:
            return page.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('a[href*="/album/"], a[href*="/playlist/"], a[href*="/artist/"]')
                      .forEach(a => {
                        const href = a.getAttribute('href') || '';
                        if (!href || seen.has(href)) return;
                        const title = (a.getAttribute('aria-label')
                            || a.textContent || '').trim();
                        if (!title) return;
                        seen.add(href);
                        let kind = 'album';
                        if (href.includes('/playlist/')) kind = 'playlist';
                        else if (href.includes('/artist/')) kind = 'artist';
                        else if (href.includes('?i=')) kind = 'track';
                        out.push({
                            id: href, title, kind,
                            url: href.startsWith('http') ? href : 'https://music.apple.com' + href
                        });
                    });
                    return out.slice(0, 25);
                }"""
            ) or []
        except Exception as error:
            logger.debug(f"[apple music] search scrape failed: {error}")
            return []

    # -- playback, through MusicKit ---------------------------------------

    def play(self, page, track) -> None:
        url = getattr(track, "url", "") or ""
        if not url:
            raise ValueError("No Apple Music URL on that track.")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if not self._musickit_ready(page):
            raise RuntimeError("Apple Music's player did not load.")
        try:
            page.evaluate(
                """async () => {
                    const k = window.MusicKit.getInstance();
                    await k.play();
                }"""
            )
        except Exception:
            # setQueue/play sometimes needs the page's own play control to have
            # been used once; fall back to it rather than giving up.
            button = self.first_visible(
                page,
                ('button[aria-label*="Play" i]', ".play-button", '[data-testid="play-button"]'),
                timeout=10000,
            )
            if button is None:
                raise RuntimeError("Apple Music would not start playing.")
            button.click()

        try:
            page.wait_for_function(
                "() => { const k = window.MusicKit.getInstance();"
                " return k && k.isPlaying; }",
                timeout=30000,
            )
        except Exception as error:
            raise RuntimeError(f"Apple Music did not start playing: {error}") from error

    def pause(self, page) -> None:
        try:
            page.evaluate("() => window.MusicKit.getInstance().pause()")
        except Exception as error:
            logger.debug(f"[apple music] pause failed: {error}")

    def resume(self, page) -> None:
        try:
            page.evaluate("() => window.MusicKit.getInstance().play()")
        except Exception as error:
            logger.debug(f"[apple music] resume failed: {error}")

    def stop(self, page) -> None:
        try:
            page.evaluate("() => window.MusicKit.getInstance().stop()")
        except Exception:
            pass
        super().stop(page)

    def set_volume(self, page, volume: float) -> None:
        try:
            page.evaluate("v => { window.MusicKit.getInstance().volume = v; }", volume)
        except Exception:
            super().set_volume(page, volume)

    def seek(self, page, offset: float) -> None:
        try:
            page.evaluate(
                """async (o) => {
                    const k = window.MusicKit.getInstance();
                    await k.seekToTime(Math.max(0, k.currentPlaybackTime + o));
                }""",
                offset,
            )
        except Exception:
            super().seek(page, offset)

    def get_position(self, page) -> Optional[float]:
        try:
            return page.evaluate(
                "() => { const k = window.MusicKit.getInstance();"
                " return k ? k.currentPlaybackTime : null; }"
            )
        except Exception:
            return super().get_position(page)

    def get_duration(self, page) -> Optional[float]:
        try:
            return page.evaluate(
                "() => { const k = window.MusicKit.getInstance();"
                " return k ? k.currentPlaybackDuration : null; }"
            )
        except Exception:
            return super().get_duration(page)

    # -- audio tracks ------------------------------------------------------

    def list_audio_tracks(self, page) -> List[Dict[str, Any]]:
        """Music has no described audio track. Empty rather than a guess."""
        return []

    def set_audio_track(self, page, track_id: str) -> bool:
        return False

    def find_described_track(self, tracks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return None


__all__ = ["AppleMusicAdapter"]
