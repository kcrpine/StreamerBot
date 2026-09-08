"""Netflix, driven through a browser page.

Every selector here is a guess about somebody else's markup, and Netflix
redesigns without notice. Two consequences shape the whole file:

- **Elements are addressed by a list of candidate selectors**, tried in order,
  so one renamed class degrades to "could not find the audio menu" rather than a
  traceback with a Playwright timeout in it.
- **Nothing here raises for a missing element by default.** A failure returns
  None or an empty list, and the caller turns that into a sentence a user can
  act on.

All methods run on the browser engine's worker thread. See
bot/services/web/__init__.py for why that matters.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

from bot.services.web import WebServiceAdapter

logger = logging.getLogger(__name__)

HOME = "https://www.netflix.com"


class NetflixAdapter(WebServiceAdapter):
    name = "nf"
    home_url = f"{HOME}/browse"

    LOGIN_URL = f"{HOME}/login"
    PROFILES_URL = f"{HOME}/ProfilesGate"

    USERNAME_SELECTORS = (
        'input[name="userLoginId"]',
        'input[data-uia="field-userLoginId"]',
        'input[type="email"]',
    )
    PASSWORD_SELECTORS = (
        'input[name="password"]',
        'input[data-uia="field-password"]',
        'input[type="password"]',
    )
    SUBMIT_SELECTORS = (
        'button[data-uia="login-submit-button"]',
        'button[type="submit"]',
    )
    OTP_INPUT_SELECTORS = (
        'input[data-uia="field-code"]',
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
    )
    CAPTCHA_SELECTORS = (
        'iframe[src*="recaptcha"]',
        'iframe[title*="captcha" i]',
        '[data-uia*="captcha"]',
    )
    LOGGED_IN_SELECTORS = (
        '[data-uia="account-menu-item"]',
        '[data-uia="profiles-gate-label"]',
        'div.profiles-gate-container',
        'a[href*="/browse"]',
    )

    # -- session -----------------------------------------------------------

    def is_logged_in(self, page) -> bool:
        try:
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as error:
            logger.debug(f"[netflix] navigation failed: {error}")
            return False
        if "/login" in page.url:
            return False
        return self.first_visible(page, self.LOGGED_IN_SELECTORS, timeout=8000) is not None

    def login(self, page, username: str, password: str, job) -> None:
        from bot.auth.session import AuthState

        job.set_state(AuthState.Launching)
        page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

        job.set_state(AuthState.Filling)
        user_field = self.first_visible(page, self.USERNAME_SELECTORS, timeout=15000)
        pass_field = self.first_visible(page, self.PASSWORD_SELECTORS, timeout=15000)
        if user_field is None or pass_field is None:
            job.fail("The Netflix sign-in page did not look as expected.")
            return

        user_field.fill(username)
        pass_field.fill(password)

        submit = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=10000)
        if submit is None:
            job.fail("Could not find the Netflix sign-in button.")
            return
        submit.click()

        try:
            page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass

        # A CAPTCHA is a dead end for automation. Say so rather than retrying.
        if self.first_visible(page, self.CAPTCHA_SELECTORS, timeout=3000) is not None:
            job.require_captcha(page.url)
            return

        # A code step is normal, not a failure. Block here while the portal
        # collects it from the user, who is somewhere else entirely.
        otp_field = self.first_visible(page, self.OTP_INPUT_SELECTORS, timeout=5000)
        if otp_field is not None:
            code = job.request_otp("Netflix sent a verification code.")
            if not code:
                return  # request_otp already failed the job on timeout
            otp_field.fill(code)
            confirm = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=10000)
            if confirm is not None:
                confirm.click()
            try:
                page.wait_for_load_state("networkidle", timeout=45000)
            except Exception:
                pass

        if "/login" in page.url:
            job.fail("Netflix rejected that email address or password.")
            return

        job.succeed()

    # -- profiles ----------------------------------------------------------

    def list_profiles(self, page) -> List[Dict[str, Any]]:
        """Profiles matter here: each has its own watchlist and audio settings."""
        try:
            page.goto(self.PROFILES_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            return []
        try:
            return page.evaluate(
                """() => {
                    const out = [];
                    const nodes = document.querySelectorAll(
                        '[data-uia="profile-link"], .profile-link, li.profile a'
                    );
                    nodes.forEach((n, i) => {
                        const label = (n.getAttribute('aria-label')
                            || n.querySelector('.profile-name')?.textContent
                            || n.textContent || '').trim();
                        if (label) out.push({ id: String(i), name: label });
                    });
                    return out;
                }"""
            ) or []
        except Exception as error:
            logger.debug(f"[netflix] profile listing failed: {error}")
            return []

    def select_profile(self, page, profile_id: str) -> bool:
        try:
            index = int(profile_id)
        except (TypeError, ValueError):
            return False
        try:
            clicked = page.evaluate(
                """(i) => {
                    const nodes = document.querySelectorAll(
                        '[data-uia="profile-link"], .profile-link, li.profile a'
                    );
                    if (i < 0 || i >= nodes.length) return false;
                    nodes[i].click();
                    return true;
                }""",
                index,
            )
            if clicked:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            return bool(clicked)
        except Exception as error:
            logger.debug(f"[netflix] profile selection failed: {error}")
            return False

    # -- finding things ----------------------------------------------------

    def search(self, page, query: str) -> List[Dict[str, Any]]:
        """Netflix's own ordering is preserved: it is the site's answer to the
        query and re-sorting it produces worse results."""
        url = f"{HOME}/search?q={query}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)  # results render after the initial paint
        except Exception as error:
            logger.debug(f"[netflix] search navigation failed: {error}")
            return []
        try:
            return page.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('a[href*="/watch/"]').forEach(a => {
                        const m = a.getAttribute('href').match(/\\/watch\\/(\\d+)/);
                        if (!m || seen.has(m[1])) return;
                        const title = (a.getAttribute('aria-label')
                            || a.querySelector('img')?.getAttribute('alt')
                            || a.textContent || '').trim();
                        if (!title) return;
                        seen.add(m[1]);
                        out.push({ id: m[1], title, kind: 'title',
                                   url: 'netflix://watch/' + m[1] });
                    });
                    return out.slice(0, 25);
                }"""
            ) or []
        except Exception as error:
            logger.debug(f"[netflix] search scrape failed: {error}")
            return []

    # -- playback ----------------------------------------------------------

    @staticmethod
    def _watch_id(track) -> Optional[str]:
        url = getattr(track, "url", "") or ""
        match = re.search(r"(?:netflix://watch/|/watch/)(\d+)", url)
        return match.group(1) if match else None

    def play(self, page, track) -> None:
        watch_id = self._watch_id(track)
        if not watch_id:
            raise ValueError(f"Not a Netflix title: {getattr(track, 'url', '')!r}")
        page.goto(f"{HOME}/watch/{watch_id}", wait_until="domcontentloaded", timeout=60000)
        # Wait for the media element rather than a fixed sleep: licence
        # negotiation takes an unpredictable amount of time.
        try:
            page.wait_for_selector("video", timeout=60000)
            page.wait_for_function(
                "() => { const v = document.querySelector('video');"
                " return v && v.readyState >= 2; }",
                timeout=60000,
            )
        except Exception as error:
            raise RuntimeError(f"Netflix did not start playing: {error}") from error

    def pause(self, page) -> None:
        page.evaluate("() => { const v = document.querySelector('video'); if (v) v.pause(); }")

    def resume(self, page) -> None:
        page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play(); }")

    # -- audio tracks ------------------------------------------------------

    def list_audio_tracks(self, page) -> List[Dict[str, Any]]:
        """Read the player's own audio menu.

        Only meaningful once playback has started: Netflix does not populate the
        menu before the licence is negotiated.
        """
        try:
            tracks = page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    if (v && v.audioTracks && v.audioTracks.length) {
                        return Array.from(v.audioTracks).map((t, i) => ({
                            id: t.id || String(i),
                            label: t.label || t.language || ''
                        }));
                    }
                    return [];
                }"""
            ) or []
            if tracks:
                return tracks
        except Exception as error:
            logger.debug(f"[netflix] audioTracks unavailable: {error}")

        # Fall back to the rendered menu when the media element does not expose
        # tracks, which is the usual case behind Netflix's own player UI.
        try:
            return page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll(
                        '[data-uia*="audio"] li, .track-list-audio li, [role="menuitemradio"]'
                    ).forEach((n, i) => {
                        const label = (n.getAttribute('aria-label') || n.textContent || '').trim();
                        if (label) out.push({ id: String(i), label });
                    });
                    return out;
                }"""
            ) or []
        except Exception as error:
            logger.debug(f"[netflix] audio menu scrape failed: {error}")
            return []

    def set_audio_track(self, page, track_id: str) -> bool:
        try:
            changed = page.evaluate(
                """(id) => {
                    const v = document.querySelector('video');
                    if (v && v.audioTracks && v.audioTracks.length) {
                        let found = false;
                        Array.from(v.audioTracks).forEach((t, i) => {
                            const match = (t.id || String(i)) === id;
                            t.enabled = match;
                            if (match) found = true;
                        });
                        if (found) return true;
                    }
                    const nodes = document.querySelectorAll(
                        '[data-uia*="audio"] li, .track-list-audio li, [role="menuitemradio"]'
                    );
                    const i = parseInt(id, 10);
                    if (!isNaN(i) && i >= 0 && i < nodes.length) {
                        nodes[i].click();
                        return true;
                    }
                    return false;
                }""",
                str(track_id),
            )
            return bool(changed)
        except Exception as error:
            logger.debug(f"[netflix] setting the audio track failed: {error}")
            return False


__all__ = ["NetflixAdapter"]
