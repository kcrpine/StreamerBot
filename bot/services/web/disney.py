"""Disney Plus, driven through a browser page.

Structurally the closest site to Netflix: a profile gate, a search page, and a
player whose audio menu carries the described track. The differences are all in
the markup, which is exactly what the adapter layer exists to absorb.

Two behaviours worth knowing before changing anything:

- Disney's player is slower to negotiate a licence than Netflix's, so the play
  path waits on the media element being ready rather than on navigation.
- Its audio menu labels described tracks in the account's language, and the
  common English label is "Audio Description" while some regions use the
  abbreviation "AD" on its own. Matching bare "ad" as a substring would hit
  "Standard" and "Broadcast", so that one is matched as a whole word.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from bot.services.web import WebServiceAdapter

logger = logging.getLogger(__name__)

HOME = "https://www.disneyplus.com"


class DisneyAdapter(WebServiceAdapter):
    name = "dp"
    home_url = f"{HOME}/home"

    LOGIN_URL = f"{HOME}/login"
    PROFILES_URL = f"{HOME}/select-profile"

    USERNAME_SELECTORS = (
        'input[data-testid="email-input"]',
        'input[name="email"]',
        'input[type="email"]',
    )
    PASSWORD_SELECTORS = (
        'input[data-testid="password-input"]',
        'input[name="password"]',
        'input[type="password"]',
    )
    SUBMIT_SELECTORS = (
        'button[data-testid="login-continue-button"]',
        'button[data-testid="btn-submit"]',
        'button[type="submit"]',
    )
    OTP_INPUT_SELECTORS = (
        'input[data-testid="otp-input"]',
        'input[autocomplete="one-time-code"]',
        'input[name="otp"]',
        'input[inputmode="numeric"]',
    )
    CAPTCHA_SELECTORS = (
        'iframe[src*="recaptcha"]',
        'iframe[src*="hcaptcha"]',
        '[data-testid*="captcha"]',
    )
    LOGGED_IN_SELECTORS = (
        '[data-testid="profile-avatar"]',
        '[data-testid="home-collection"]',
        'a[href*="/home"]',
    )

    def is_logged_in(self, page) -> bool:
        try:
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as error:
            logger.debug(f"[disney] navigation failed: {error}")
            return False
        if "/login" in page.url or "/welcome" in page.url:
            return False
        return self.first_visible(page, self.LOGGED_IN_SELECTORS, timeout=8000) is not None

    def login(self, page, username: str, password: str, job) -> None:
        from bot.auth.session import AuthState

        job.set_state(AuthState.Launching)
        page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        job.set_state(AuthState.Filling)

        user_field = self.first_visible(page, self.USERNAME_SELECTORS, timeout=15000)
        if user_field is None:
            job.fail("The Disney Plus sign-in page did not look as expected.")
            return
        user_field.fill(username)

        # Disney splits email and password across two steps more often than not.
        submit = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=8000)
        if submit is not None:
            submit.click()
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass

        pass_field = self.first_visible(page, self.PASSWORD_SELECTORS, timeout=20000)
        if pass_field is None:
            job.fail("Disney Plus did not ask for a password where expected.")
            return
        pass_field.fill(password)

        submit = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=10000)
        if submit is None:
            job.fail("Could not find the Disney Plus sign-in button.")
            return
        submit.click()
        try:
            page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass

        if self.first_visible(page, self.CAPTCHA_SELECTORS, timeout=3000) is not None:
            job.require_captcha(page.url)
            return

        otp_field = self.first_visible(page, self.OTP_INPUT_SELECTORS, timeout=5000)
        if otp_field is not None:
            code = job.request_otp("Disney Plus sent a one-time passcode.")
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

        if "/login" in page.url:
            job.fail("Disney Plus rejected that email address or password.")
            return
        job.succeed()

    # -- profiles ----------------------------------------------------------

    def list_profiles(self, page) -> List[Dict[str, Any]]:
        try:
            page.goto(self.PROFILES_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
        except Exception:
            return []
        try:
            return page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll(
                        '[data-testid="profile-card"], [data-gv2elementkey="profile"], .profile-card'
                    ).forEach((n, i) => {
                        const label = (n.getAttribute('aria-label')
                            || n.querySelector('.profile-name')?.textContent
                            || n.textContent || '').trim();
                        if (label) out.push({ id: String(i), name: label });
                    });
                    return out;
                }"""
            ) or []
        except Exception as error:
            logger.debug(f"[disney] profile listing failed: {error}")
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
                        '[data-testid="profile-card"], [data-gv2elementkey="profile"], .profile-card'
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
            logger.debug(f"[disney] profile selection failed: {error}")
            return False

    # -- finding things ----------------------------------------------------

    def search(self, page, query: str) -> List[Dict[str, Any]]:
        try:
            page.goto(f"{HOME}/search?q={query}", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
        except Exception as error:
            logger.debug(f"[disney] search navigation failed: {error}")
            return []
        try:
            return page.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('a[href*="/video/"], a[href*="/series/"], a[href*="/movies/"]')
                      .forEach(a => {
                        const href = a.getAttribute('href') || '';
                        const m = href.match(/\\/(?:video|series|movies)\\/[^/]*\\/?([0-9a-zA-Z-]+)?/);
                        const id = href;
                        if (!id || seen.has(id)) return;
                        const title = (a.getAttribute('aria-label')
                            || a.querySelector('img')?.getAttribute('alt')
                            || a.textContent || '').trim();
                        if (!title) return;
                        seen.add(id);
                        out.push({
                            id: href,
                            title,
                            kind: href.includes('/series/') ? 'series' : 'title',
                            url: 'disney://' + href.replace(/^\\//, '')
                        });
                    });
                    return out.slice(0, 25);
                }"""
            ) or []
        except Exception as error:
            logger.debug(f"[disney] search scrape failed: {error}")
            return []

    # -- playback ----------------------------------------------------------

    def play(self, page, track) -> None:
        url = getattr(track, "url", "") or ""
        path = url.replace("disney://", "").strip("/")
        if not path:
            raise ValueError(f"Not a Disney Plus title: {url!r}")
        page.goto(f"{HOME}/{path}", wait_until="domcontentloaded", timeout=60000)

        # Disney is slower than Netflix to negotiate a licence, and a play
        # button often stands between the page and the video.
        play_button = self.first_visible(
            page,
            ('[data-testid="play-button"]', 'button[aria-label*="Play" i]', ".play-button"),
            timeout=15000,
        )
        if play_button is not None:
            try:
                play_button.click()
            except Exception:
                pass

        try:
            page.wait_for_selector("video", timeout=60000)
            page.wait_for_function(
                "() => { const v = document.querySelector('video');"
                " return v && v.readyState >= 2; }",
                timeout=90000,
            )
        except Exception as error:
            raise RuntimeError(f"Disney Plus did not start playing: {error}") from error

    def pause(self, page) -> None:
        page.evaluate("() => { const v = document.querySelector('video'); if (v) v.pause(); }")

    def resume(self, page) -> None:
        page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play(); }")

    # -- audio tracks ------------------------------------------------------

    def list_audio_tracks(self, page) -> List[Dict[str, Any]]:
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
            logger.debug(f"[disney] audioTracks unavailable: {error}")

        try:
            return page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll(
                        '[data-testid*="audio"] li, [role="menuitemradio"], .audio-track-item'
                    ).forEach((n, i) => {
                        const label = (n.getAttribute('aria-label') || n.textContent || '').trim();
                        if (label) out.push({ id: String(i), label });
                    });
                    return out;
                }"""
            ) or []
        except Exception as error:
            logger.debug(f"[disney] audio menu scrape failed: {error}")
            return []

    def set_audio_track(self, page, track_id: str) -> bool:
        try:
            return bool(page.evaluate(
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
                        '[data-testid*="audio"] li, [role="menuitemradio"], .audio-track-item'
                    );
                    const i = parseInt(id, 10);
                    if (!isNaN(i) && i >= 0 && i < nodes.length) { nodes[i].click(); return true; }
                    return false;
                }""",
                str(track_id),
            ))
        except Exception as error:
            logger.debug(f"[disney] setting the audio track failed: {error}")
            return False

    def find_described_track(self, tracks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Disney abbreviates to "AD" in some regions.

        Matched as a whole word, never as a substring: a bare "ad" would hit
        "Standard" and "Broadcast" and silently select the wrong track, which is
        worse than not finding one at all.
        """
        found = super().find_described_track(tracks)
        if found is not None:
            return found
        for track in tracks:
            label = (track.get("label") or "")
            if re.search(r"\bAD\b", label):
                return track
        return None


__all__ = ["DisneyAdapter"]
