"""Tubi, driven through a browser page.

Tubi is free with advertisements and needs no account, so is_logged_in() is
only asked by the portal's status page; nothing waits on it before playing. A
signed-in account adds a watchlist and resume points and nothing else.

Built from Tubi's public page structure, not yet measured against the live
site. As in the other adapters, every element is a list of candidate selectors,
and a failure says what could not be found. Advertisements play inside the same
video element as the programme, so they are heard in the channel too; there is
no way round that on a free service, and skipping them is not attempted.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List
from urllib.parse import quote

from bot.services.web import WebServiceAdapter

logger = logging.getLogger(__name__)

HOME = "https://tubitv.com"


class TubiAdapter(WebServiceAdapter):
    name = "tb"
    home_url = f"{HOME}/home"

    LOGIN_URL = f"{HOME}/login"

    USERNAME_SELECTORS = ('input[name="email"]', 'input[type="email"]')
    PASSWORD_SELECTORS = ('input[name="password"]', 'input[type="password"]')
    SUBMIT_SELECTORS = ('button[type="submit"]', 'button[aria-label*="Sign in" i]')
    CAPTCHA_SELECTORS = ('iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]')
    SIGNED_IN_SELECTORS = ('a[href*="/account"]', '[data-test-id*="avatar"]', 'button[aria-label*="account" i]')

    def is_logged_in(self, page) -> bool:
        try:
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as error:
            logger.debug(f"[tubi] navigation failed: {error}")
            return False
        return self.first_visible(page, self.SIGNED_IN_SELECTORS, timeout=8000) is not None

    def login(self, page, username: str, password: str, job) -> None:
        from bot.auth.session import AuthState

        job.set_state(AuthState.Launching)
        page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        job.set_state(AuthState.Filling)

        user_field = self.first_visible(page, self.USERNAME_SELECTORS, timeout=15000)
        if user_field is None:
            logger.info(f"[tubi] no email field on {page.url}")
            job.fail("The Tubi sign-in page did not look as expected.")
            return
        user_field.fill(username)

        pass_field = self.first_visible(page, self.PASSWORD_SELECTORS, timeout=3000)
        if pass_field is None:
            submit = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=8000)
            if submit is not None:
                submit.click()
            pass_field = self.first_visible(page, self.PASSWORD_SELECTORS, timeout=20000)
        if pass_field is None:
            job.fail("Tubi did not ask for a password where expected.")
            return
        pass_field.fill(password)

        submit = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=10000)
        if submit is None:
            job.fail("Could not find the Tubi sign-in button.")
            return
        submit.click()
        try:
            page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass

        if self.first_visible(page, self.CAPTCHA_SELECTORS, timeout=3000) is not None:
            job.require_captcha(page.url)
            return
        if "/login" in page.url:
            job.fail("Tubi rejected that email address or password.")
            return
        job.succeed()

    # -- finding things ----------------------------------------------------

    def search(self, page, query: str) -> List[Dict[str, Any]]:
        try:
            page.goto(f"{HOME}/search/{quote(query)}", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
        except Exception as error:
            logger.debug(f"[tubi] search navigation failed: {error}")
            return []
        try:
            return page.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll(
                        'a[href*="/movies/"], a[href*="/series/"], a[href*="/tv-shows/"]'
                    ).forEach(a => {
                        const href = (a.getAttribute('href') || '').split('?')[0];
                        if (!href || seen.has(href)) return;
                        const title = (a.getAttribute('aria-label')
                            || a.querySelector('img')?.getAttribute('alt')
                            || a.textContent || '').trim();
                        if (!title) return;
                        seen.add(href);
                        out.push({
                            id: href,
                            title,
                            kind: href.includes('/series/') ? 'series' : 'title',
                            url: 'tubi://' + href.replace(/^https?:\\/\\/[^/]+/, '').replace(/^\\//, '')
                        });
                    });
                    return out.slice(0, 25);
                }"""
            ) or []
        except Exception as error:
            logger.debug(f"[tubi] search scrape failed: {error}")
            return []

    # -- playback ----------------------------------------------------------

    def play(self, page, track) -> None:
        url = getattr(track, "url", "") or ""
        # A pasted link arrives as itself; a search result as tubi://path.
        if url.startswith("https://"):
            target = url
        else:
            path = url.replace("tubi://", "").strip("/")
            if not path:
                raise ValueError(f"Not a Tubi title: {url!r}")
            target = f"{HOME}/{path}"
        page.goto(target, wait_until="domcontentloaded", timeout=60000)

        play_button = self.first_visible(
            page,
            (
                'a[href*="/player/"]',
                'button[aria-label*="Play" i]',
                'a[aria-label*="Play" i]',
                '[data-test-id*="play"]',
            ),
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
                # Longer than the paid services: an advertisement loads first.
                timeout=120000,
            )
        except Exception as error:
            logger.info(f"[tubi] no playing video on {page.url}")
            raise RuntimeError(f"Tubi did not start playing: {error}") from error

    def pause(self, page) -> None:
        page.evaluate("() => { const v = document.querySelector('video'); if (v) v.pause(); }")

    def resume(self, page) -> None:
        page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play(); }")

    # -- audio tracks ------------------------------------------------------

    def list_audio_tracks(self, page) -> List[Dict[str, Any]]:
        return []

    def set_audio_track(self, page, track_id: str) -> bool:
        return False


__all__ = ["TubiAdapter"]
