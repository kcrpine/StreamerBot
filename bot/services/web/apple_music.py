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
        'input[autocomplete="one-time-code"]',
        'input[name="char0"]',
        'input[inputmode="numeric"]',
    )

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
        """Apple's sign-in lives in an iframe, and the two-step form is the
        norm rather than the exception."""
        from bot.auth.session import AuthState

        job.set_state(AuthState.Launching)
        page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        job.set_state(AuthState.Filling)

        frame = None
        for _ in range(20):
            for candidate in page.frames:
                if "appleid" in (candidate.url or "") or "auth" in (candidate.url or ""):
                    frame = candidate
                    break
            if frame is not None:
                break
            page.wait_for_timeout(500)
        target = frame or page

        user_field = None
        for selector in self.USERNAME_SELECTORS:
            try:
                user_field = target.wait_for_selector(selector, timeout=8000, state="visible")
                if user_field:
                    break
            except Exception:
                continue
        if user_field is None:
            job.fail("The Apple sign-in form did not appear.")
            return
        user_field.fill(username)
        try:
            user_field.press("Enter")
        except Exception:
            pass
        page.wait_for_timeout(2000)

        pass_field = None
        for selector in self.PASSWORD_SELECTORS:
            try:
                pass_field = target.wait_for_selector(selector, timeout=15000, state="visible")
                if pass_field:
                    break
            except Exception:
                continue
        if pass_field is None:
            job.fail("Apple did not ask for a password where expected.")
            return
        pass_field.fill(password)
        try:
            pass_field.press("Enter")
        except Exception:
            pass
        page.wait_for_timeout(4000)

        # Apple two-factor is near universal on these accounts, so this is the
        # expected path rather than an edge case.
        otp_field = None
        for selector in self.OTP_INPUT_SELECTORS:
            try:
                otp_field = target.wait_for_selector(selector, timeout=6000, state="visible")
                if otp_field:
                    break
            except Exception:
                continue
        if otp_field is not None:
            code = job.request_otp("Apple sent a verification code to your devices.")
            if not code:
                return
            otp_field.fill(code)
            try:
                otp_field.press("Enter")
            except Exception:
                pass
            page.wait_for_timeout(5000)

        if self.is_logged_in(page):
            job.succeed()
        else:
            job.fail("Apple Music did not accept those details.")

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
