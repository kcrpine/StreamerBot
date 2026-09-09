"""Amazon Music, driven through the DOM.

**The most fragile of the four, and it is worth being honest about why.** Apple
exposes MusicKit, Netflix and Disney expose real media elements with audio
tracks. Amazon Music exposes neither: it is a heavily obfuscated single-page
application with generated class names, so everything here is a guess about
markup that changes without notice.

The consequences are designed for rather than hidden:

- Playback control goes through the `<audio>` element where one is reachable,
  because a media element is a stable browser API even when the markup around it
  is not.
- Keyboard shortcuts are the fallback for play and pause, since Amazon's own
  bindings survive restyling better than its class names do.
- Every failure returns empty or False so the service can say "Amazon Music could
  not do that" rather than surfacing a Playwright timeout.

Expect this file to need updating more often than the other three. That is a
property of the site, not of the design.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from bot.services.web import WebServiceAdapter

logger = logging.getLogger(__name__)

HOME = "https://music.amazon.com"


class AmazonMusicAdapter(WebServiceAdapter):
    name = "az"
    home_url = f"{HOME}/home"

    LOGIN_URL = "https://www.amazon.com/ap/signin"

    USERNAME_SELECTORS = (
        'input#ap_email',
        'input[name="email"]',
        'input[type="email"]',
    )
    PASSWORD_SELECTORS = (
        'input#ap_password',
        'input[name="password"]',
        'input[type="password"]',
    )
    SUBMIT_SELECTORS = (
        'input#signInSubmit',
        'input#continue',
        'button[type="submit"]',
    )
    OTP_INPUT_SELECTORS = (
        'input#auth-mfa-otpcode',
        'input[name="otpCode"]',
        'input[autocomplete="one-time-code"]',
    )
    CAPTCHA_SELECTORS = (
        'img[src*="captcha"]',
        '#auth-captcha-image',
        'form[action*="captcha"]',
    )

    def is_logged_in(self, page) -> bool:
        try:
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
        except Exception as error:
            logger.debug(f"[amazon music] navigation failed: {error}")
            return False
        if "/ap/signin" in page.url or "amazon.com/ap" in page.url:
            return False
        try:
            return bool(page.evaluate(
                """() => !!document.querySelector(
                    'music-horizontal-item, music-shoveler, [class*="navigationList"]'
                )"""
            ))
        except Exception:
            return False

    def login(self, page, username: str, password: str, job) -> None:
        from bot.auth.session import AuthState

        job.set_state(AuthState.Launching)
        page.goto(self.home_url, wait_until="domcontentloaded", timeout=60000)
        # Amazon Music bounces to the Amazon account sign-in rather than hosting
        # its own, so follow wherever it lands.
        if "/ap/signin" not in page.url:
            try:
                page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass

        job.set_state(AuthState.Filling)
        user_field = self.first_visible(page, self.USERNAME_SELECTORS, timeout=15000)
        if user_field is None:
            job.fail("The Amazon sign-in page did not look as expected.")
            return
        user_field.fill(username)

        submit = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=8000)
        if submit is not None:
            submit.click()
            page.wait_for_timeout(2500)

        pass_field = self.first_visible(page, self.PASSWORD_SELECTORS, timeout=20000)
        if pass_field is None:
            job.fail("Amazon did not ask for a password where expected.")
            return
        pass_field.fill(password)
        submit = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=10000)
        if submit is not None:
            submit.click()
        try:
            page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass

        # Amazon shows image CAPTCHAs readily, and more so to a datacentre IP.
        if self.first_visible(page, self.CAPTCHA_SELECTORS, timeout=3000) is not None:
            job.require_captcha(page.url)
            return

        otp_field = self.first_visible(page, self.OTP_INPUT_SELECTORS, timeout=5000)
        if otp_field is not None:
            code = job.request_otp("Amazon sent a one-time password.")
            if not code:
                return
            otp_field.fill(code)
            confirm = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=10000)
            if confirm is not None:
                confirm.click()
            try:
                page.wait_for_load_state("networkidle", timeout=45000)
            except Exception:
                pass

        if self.is_logged_in(page):
            job.succeed()
        else:
            job.fail("Amazon Music did not accept those details.")

    # -- finding things ----------------------------------------------------

    def search(self, page, query: str) -> List[Dict[str, Any]]:
        try:
            page.goto(f"{HOME}/search/{query}", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)  # this app renders late
        except Exception as error:
            logger.debug(f"[amazon music] search navigation failed: {error}")
            return []
        try:
            return page.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('a[href*="/albums/"], a[href*="/playlists/"], a[href*="/artists/"], a[href*="/tracks/"]')
                      .forEach(a => {
                        const href = a.getAttribute('href') || '';
                        if (!href || seen.has(href)) return;
                        const title = (a.getAttribute('aria-label')
                            || a.textContent || '').trim();
                        if (!title) return;
                        seen.add(href);
                        let kind = 'album';
                        if (href.includes('/playlists/')) kind = 'playlist';
                        else if (href.includes('/artists/')) kind = 'artist';
                        else if (href.includes('/tracks/')) kind = 'track';
                        out.push({
                            id: href, title, kind,
                            url: href.startsWith('http') ? href : 'https://music.amazon.com' + href
                        });
                    });
                    return out.slice(0, 25);
                }"""
            ) or []
        except Exception as error:
            logger.debug(f"[amazon music] search scrape failed: {error}")
            return []

    # -- playback ----------------------------------------------------------

    def play(self, page, track) -> None:
        url = getattr(track, "url", "") or ""
        if not url:
            raise ValueError("No Amazon Music URL on that track.")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        button = self.first_visible(
            page,
            (
                'music-button[aria-label*="Play" i]',
                'button[aria-label*="Play" i]',
                '[data-key="play-button"]',
                'music-image-row',
            ),
            timeout=20000,
        )
        if button is not None:
            try:
                button.click()
            except Exception:
                pass

        try:
            page.wait_for_function(
                "() => { const a = document.querySelector('audio, video');"
                " return a && !a.paused && a.readyState >= 2; }",
                timeout=45000,
            )
        except Exception as error:
            raise RuntimeError(f"Amazon Music did not start playing: {error}") from error

    def pause(self, page) -> None:
        if not self._media_control(page, "pause"):
            # Amazon's own space binding outlives its class names.
            try:
                page.keyboard.press("Space")
            except Exception as error:
                logger.debug(f"[amazon music] pause failed: {error}")

    def resume(self, page) -> None:
        if not self._media_control(page, "play"):
            try:
                page.keyboard.press("Space")
            except Exception as error:
                logger.debug(f"[amazon music] resume failed: {error}")

    @staticmethod
    def _media_control(page, action: str) -> bool:
        try:
            return bool(page.evaluate(
                """(a) => {
                    const m = document.querySelector('audio, video');
                    if (!m) return false;
                    a === 'pause' ? m.pause() : m.play();
                    return true;
                }""",
                action,
            ))
        except Exception:
            return False

    # -- audio tracks ------------------------------------------------------

    def list_audio_tracks(self, page) -> List[Dict[str, Any]]:
        return []

    def set_audio_track(self, page, track_id: str) -> bool:
        return False

    def find_described_track(self, tracks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return None


__all__ = ["AmazonMusicAdapter"]
