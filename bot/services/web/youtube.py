"""YouTube sign-in through the bot's own Chrome, and reading the session back out.

Not a playback adapter. YouTube plays through the shared bridge and mpv; the
browser's only jobs are to sign a Google account in once and to keep that
session alive by loading youtube.com, which is what makes Google rotate
__Secure-1PSIDTS and __Secure-3PSIDTS. The search and playback methods the
adapter interface requires exist only to say so.

**Written against Google's documented sign-in flow, not yet measured in the
bot's Chrome.** Apple's adapter was first written the same way and never reached
a form (see [023]). Selectors are therefore lists, every step that can stall
names the page it stalled on in the log, and Google refusing an automated
browser outright ("This browser or app may not be secure") is an expected
outcome with its own message pointing at session import, not an error.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

from bot.services.web import WebServiceAdapter

logger = logging.getLogger(__name__)

HOME = "https://www.youtube.com"

# Signing in through ServiceLogin with service=youtube and a continue URL lands
# back on youtube.com with the YouTube cookies set, which a sign-in on
# accounts.google.com alone does not do.
SIGNIN_URL = (
    "https://accounts.google.com/ServiceLogin?service=youtube&hl=en&continue="
    + quote(f"{HOME}/signin?action_handle_signin=true&app=desktop&next=%2F", safe="")
)

# ytcfg is YouTube's page configuration object. LOGGED_IN is YouTube's own
# answer to "is this session signed in", which is the only check that means
# anything: Google's cookie expiry dates are years away and sessions end
# server-side long before them.
SESSION_STATE_JS = """() => {
    const cfg = window.ytcfg;
    const get = (k) => cfg && (cfg.get ? cfg.get(k) : (cfg.data_ || {})[k]);
    if (!cfg) return null;
    return {logged_in: !!get('LOGGED_IN'), datasync_id: String(get('DATASYNC_ID') || '')};
}"""

# Google's refusal of an automated browser, and the other dead ends that no
# amount of waiting gets past. Matched against the page's visible text.
REFUSED_MARKERS = (
    "browser or app may not be secure",
    "couldn't sign you in",
    "couldn’t sign you in",
)
CAPTCHA_MARKERS = ("type the text you hear or see", "confirm you're not a robot")


class YouTubeAdapter(WebServiceAdapter):
    name = "yt"
    home_url = HOME

    EMAIL_SELECTORS = ('input#identifierId', 'input[type="email"]', 'input[name="identifier"]')
    EMAIL_NEXT_SELECTORS = ('#identifierNext button', '#identifierNext', 'button:has-text("Next")')
    PASSWORD_SELECTORS = ('input[name="Passwd"]', 'input[type="password"]')
    PASSWORD_NEXT_SELECTORS = ('#passwordNext button', '#passwordNext', 'button:has-text("Next")')
    # Authenticator app, text message, and backup code boxes.
    CODE_SELECTORS = (
        'input[name="totpPin"]',
        'input#totpPin',
        'input[name="idvPin"]',
        'input#idvPin',
        'input[name="backupCode"]',
        'input[autocomplete="one-time-code"]',
    )
    CODE_NEXT_SELECTORS = ('#totpNext button', '#idvPreregisteredPhoneNext button', 'button:has-text("Next")')
    ERROR_SELECTORS = ('[aria-live="assertive"]', 'div[jsname="B34EJ"]', '.Ekjuhf')

    FORM_WAIT_MS = 45000
    STEP_WAIT_MS = 30000
    # Long enough to find a phone, unlock it and tap. Matches the typed-code wait.
    APPROVAL_WAIT_SECONDS = 300

    # -- session -----------------------------------------------------------

    def session_state(self, page) -> Optional[Dict[str, Any]]:
        try:
            return page.evaluate(SESSION_STATE_JS)
        except Exception:
            return None

    def _on_youtube(self, page) -> bool:
        host = (urlparse(getattr(page, "url", "") or "").hostname or "").lower()
        return host == "youtube.com" or host.endswith(".youtube.com")

    def _load_home(self, page) -> Optional[Dict[str, Any]]:
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_function("() => !!window.ytcfg", timeout=30000)
        except Exception:
            return None
        # Rotation happens in requests YouTube makes after load. A short settle
        # lets them finish before the cookies are read.
        page.wait_for_timeout(4000)
        return self.session_state(page)

    def is_logged_in(self, page) -> bool:
        try:
            state = self._load_home(page)
        except Exception as error:
            logger.debug(f"[youtube] could not load YouTube: {error}")
            return False
        return bool(state and state.get("logged_in"))

    def export_session(self, page, context) -> Dict[str, Any]:
        """Load youtube.com, let Google rotate the session, and read it out.

        Returns {logged_in, datasync_id, cookies}. Cookies are read only when
        signed in, so a signed-out page can never hand back a set of cookies that
        would overwrite a working session.
        """
        state = self._load_home(page) or {}
        if not state.get("logged_in"):
            return {"logged_in": False, "datasync_id": "", "cookies": []}
        cookies = context.cookies(["https://www.youtube.com", "https://accounts.google.com",
                                   "https://www.google.com"])
        return {"logged_in": True, "datasync_id": state.get("datasync_id", ""), "cookies": cookies}

    @staticmethod
    def playwright_cookies(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Parsed cookies.txt entries in the shape context.add_cookies accepts."""
        out = []
        for c in cookies:
            domain = str(c.get("domain") or "")
            if not domain or not c.get("name"):
                continue
            entry = {
                "name": c["name"],
                "value": str(c.get("value") or ""),
                "domain": domain,
                "path": c.get("path") or "/",
                "secure": bool(c.get("secure")),
                "httpOnly": bool(c.get("httpOnly")),
                "sameSite": "None" if c.get("secure") else "Lax",
            }
            expires = c.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                entry["expires"] = int(expires)
            out.append(entry)
        return out

    def import_session(self, page, context, cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Put an imported session into the bot's own profile, then read it back.

        A browser that holds the session keeps it alive; a file on its own goes
        stale on Google's schedule. Cookies are added one at a time because
        Chrome rejects some shapes (a __Host- cookie with a domain, say) and one
        bad line should not lose the rest.
        """
        rejected = 0
        for entry in self.playwright_cookies(cookies):
            try:
                context.add_cookies([entry])
            except Exception:
                rejected += 1
        if rejected:
            logger.info(f"[youtube] Chrome did not accept {rejected} imported cookie(s)")
        return self.export_session(page, context)

    # -- sign-in -----------------------------------------------------------

    def _visible(self, page, selectors, timeout_ms: int):
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for selector in selectors:
                try:
                    loc = page.locator(selector)
                    for index in range(loc.count()):
                        candidate = loc.nth(index)
                        if candidate.is_visible():
                            return candidate
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(500)

    def _press(self, page, selectors, field) -> None:
        button = self._visible(page, selectors, 1500)
        try:
            if button is not None:
                button.click()
                return
        except Exception:
            pass
        try:
            field.press("Enter")
        except Exception:
            pass

    def _page_text(self, page) -> str:
        try:
            return (page.inner_text("body") or "").lower()
        except Exception:
            return ""

    def _error_text(self, page) -> str:
        for selector in self.ERROR_SELECTORS:
            try:
                loc = page.locator(selector)
                for index in range(loc.count()):
                    candidate = loc.nth(index)
                    if candidate.is_visible():
                        text = (candidate.inner_text() or "").strip()
                        if text:
                            return text[:200]
            except Exception:
                continue
        return ""

    @staticmethod
    def challenge_kind(url: str) -> str:
        """Which second step Google is showing, from the address.

        Pure, so the mapping can be tested without a browser. Google's challenge
        pages live at /v3/signin/challenge/<kind> or /signin/v2/challenge/<kind>.
        """
        match = re.search(r"/challenge/([a-z]+)", url or "")
        kind = match.group(1) if match else ""
        if kind in ("dp", "az", "ootp", "sk"):
            # dp and az are "Check your phone" prompts; sk is a security key,
            # which the bot cannot touch but the user's own device can approve.
            return "approve"
        if kind in ("totp", "ipp", "iap", "bc", "sms"):
            return "code"
        if kind in ("recaptcha", "ipe"):
            return "captcha"
        if kind == "selection":
            return "selection"
        if kind == "pwd":
            return "password"
        return "unknown" if kind else ""

    NUMBER_JS = """() => {
        // Google's number-matching prompt shows a one or two digit number the
        // person must tap on their phone. Found by shape rather than class name.
        const nodes = document.querySelectorAll('main *, #initialView *, body *');
        for (const el of nodes) {
            if (el.children.length) continue;
            const t = (el.textContent || '').trim();
            if (/^\\d{1,3}$/.test(t) && el.offsetParent !== null) {
                const size = parseFloat(getComputedStyle(el).fontSize || '0');
                if (size >= 24) return t;
            }
        }
        return '';
    }"""

    def _approval_prompt(self, page) -> Dict[str, str]:
        heading = ""
        for selector in ("h1", "#headingText"):
            try:
                loc = page.locator(selector)
                if loc.count() and loc.first.is_visible():
                    heading = (loc.first.inner_text() or "").strip()
                    break
            except Exception:
                continue
        instruction = ""
        try:
            loc = page.locator("main p, #initialView p, form p")
            for index in range(min(loc.count(), 6)):
                text = (loc.nth(index).inner_text() or "").strip()
                if text and len(text) > 20:
                    instruction = text[:300]
                    break
        except Exception:
            pass
        try:
            number = page.evaluate(self.NUMBER_JS) or ""
        except Exception:
            number = ""
        return {"heading": heading[:120], "instruction": instruction, "number": number}

    def login(self, page, username: str, password: str, job) -> None:
        """Sign in to Google, then stop on youtube.com without finishing the job.

        The job is left for the caller to finish, because signed in is not yet
        connected: the session still has to be read out and stored, and the
        portal must not say "connected" before the bridge can use it. On success
        this sets detail signed_in=True and returns.

        Nothing is logged with the address, password or code in it. Page
        addresses are logged on failure, because they are what diagnoses a
        changed flow, with the query string removed.
        """
        from bot.auth.session import AuthState

        job.set_state(AuthState.Launching)
        page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
        job.set_state(AuthState.Filling)

        email = self._visible(page, self.EMAIL_SELECTORS, self.FORM_WAIT_MS)
        if email is None:
            if self._refused(page, job):
                return
            logger.warning(f"[youtube] Google's email box did not appear; at {self._where(page)}")
            job.fail("Google's sign-in page did not load. Please try again, or import a session instead.")
            return
        email.fill(username)
        self._press(page, self.EMAIL_NEXT_SELECTORS, email)

        password_box = None
        deadline = time.monotonic() + self.STEP_WAIT_MS / 1000
        while time.monotonic() < deadline:
            if self._refused(page, job):
                return
            error = self._error_text(page)
            if error:
                job.fail(f"Google did not accept that email address: {error}")
                return
            password_box = self._visible(page, self.PASSWORD_SELECTORS, 500)
            if password_box is not None:
                break
        if password_box is None:
            logger.warning(f"[youtube] Google did not ask for a password; at {self._where(page)}")
            job.fail(
                "Google did not ask for a password. Check the email address, or "
                "import a session instead."
            )
            return
        page.wait_for_timeout(700)
        password_box.fill(password)
        self._press(page, self.PASSWORD_NEXT_SELECTORS, password_box)

        self._after_password(page, job)

    def _refused(self, page, job) -> bool:
        text = self._page_text(page)
        if any(marker in text for marker in REFUSED_MARKERS):
            logger.warning("[youtube] Google refused to sign in from the bot's browser")
            job.fail(
                "Google refused to sign in from the bot's browser. This is common "
                "and is not a problem with your account. Import a session from your "
                "own browser instead."
            )
            return True
        if any(marker in text for marker in CAPTCHA_MARKERS):
            job.require_captcha(self._where(page))
            return True
        return False

    def _after_password(self, page, job) -> None:
        """Watch what Google does next: finish, error, ask for a code, or ask
        for approval on a phone. Watched, not assumed, because the order depends
        on the account."""
        from bot.auth.session import AuthState

        deadline = time.monotonic() + self.STEP_WAIT_MS / 1000
        asked_for_code = False
        while time.monotonic() < deadline:
            if self._on_youtube(page):
                state = self.session_state(page)
                if state and state.get("logged_in"):
                    job.set_state(AuthState.Filling, signed_in=True)
                    return

            if self._refused(page, job):
                return
            error = self._error_text(page)
            if error and "/challenge/" not in (page.url or ""):
                job.fail(f"Google did not accept those details: {error}")
                return

            kind = self.challenge_kind(page.url or "")
            if kind == "approve":
                if not self._wait_for_approval(page, job):
                    return
                deadline = time.monotonic() + self.STEP_WAIT_MS / 1000
                continue
            if kind == "code" and not asked_for_code:
                box = self._visible(page, self.CODE_SELECTORS, 3000)
                if box is not None:
                    asked_for_code = True
                    code = job.request_otp(
                        "Google asked for a verification code. Type the code from "
                        "your authenticator app or the text message Google sent."
                    )
                    if not code:
                        return
                    box.fill("".join(ch for ch in code if ch.isalnum()))
                    self._press(page, self.CODE_NEXT_SELECTORS, box)
                    deadline = time.monotonic() + self.STEP_WAIT_MS / 1000
                    continue
            if kind in ("captcha",):
                job.require_captcha(self._where(page))
                return
            if kind in ("selection", "unknown"):
                logger.warning(f"[youtube] Google showed a second step the bot cannot answer; at {self._where(page)}")
                job.fail(
                    "Google asked for a kind of verification the bot cannot complete. "
                    "Import a session from your own browser instead."
                )
                return
            page.wait_for_timeout(1000)

        # The continue redirect can be slow; the last word is youtube.com itself.
        state = None
        try:
            state = self._load_home(page)
        except Exception:
            pass
        if state and state.get("logged_in"):
            job.set_state(AuthState.Filling, signed_in=True)
            return
        logger.warning(f"[youtube] sign-in did not complete; at {self._where(page)}")
        job.fail("Google did not finish signing in. Please try again, or import a session instead.")

    def _wait_for_approval(self, page, job) -> bool:
        """Relay Google's phone prompt and wait for the tap. True once the page moves on.

        The portal page has an "I have approved it" button; pressing it nudges
        this loop to look again at once rather than at the next poll. The loop
        does not depend on it: approval is detected by Google leaving the page.
        """
        from bot.auth.session import AuthState

        prompt = self._approval_prompt(page)
        job.request_approval(**prompt)
        started_at = page.url
        deadline = time.monotonic() + self.APPROVAL_WAIT_SECONDS
        while time.monotonic() < deadline:
            if job.is_finished:
                return False  # cancelled from the portal
            job.wait_for_nudge(3)
            current = page.url or ""
            if current != started_at and self.challenge_kind(current) != "approve":
                job.set_state(AuthState.Filling)
                return True
            if self._refused(page, job):
                return False
            # A new number can appear if the first prompt timed out on the phone.
            refreshed = self._approval_prompt(page)
            if refreshed.get("number") != prompt.get("number"):
                prompt = refreshed
                job.request_approval(**prompt)
        job.fail("The sign-in was not approved in time. Please try again.")
        return False

    @staticmethod
    def _where(page) -> str:
        parsed = urlparse(getattr(page, "url", "") or "")
        return f"{parsed.netloc}{parsed.path}"

    # -- not a player ------------------------------------------------------

    def search(self, page, query: str) -> List[Dict[str, Any]]:
        return []

    def play(self, page, track) -> None:
        raise RuntimeError("YouTube plays through the bridge, not the browser.")

    def pause(self, page) -> None:
        pass

    def resume(self, page) -> None:
        pass

    def stop(self, page) -> None:
        pass

    def list_audio_tracks(self, page) -> List[Dict[str, Any]]:
        return []

    def set_audio_track(self, page, track_id: str) -> bool:
        return False


__all__ = ["YouTubeAdapter", "SIGNIN_URL", "SESSION_STATE_JS"]
