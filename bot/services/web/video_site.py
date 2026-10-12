"""The shared shape of a subscription video site driven through a browser page.

Paramount Plus and Hulu are laid out the way Disney Plus is: email and
password on one or two steps, a "who's watching" profile gate, a search page
whose results link to titles, a Watch button between a title page and the
player, and an audio menu that names the described track. What differs is
addresses and selectors, so each site is a subclass that only declares those.

Everything here was written from the sites' public page structure, not yet
measured against them with a signed-in account. Every element is a list of
candidate selectors, so a redesign degrades to a sentence saying what could not
be found, and every failure logs the page URL it was on. Read that before
changing code.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

from bot.services.web import WebServiceAdapter

logger = logging.getLogger(__name__)


class VideoSiteAdapter(WebServiceAdapter):
    #: The site's name in messages, e.g. "Paramount Plus".
    SITE = ""
    #: Scheme prefix for search results, e.g. "paramount://".
    SCHEME = ""
    HOME = ""
    LOGIN_PATH = "/login"
    PROFILES_PATH = "/profiles"
    #: The search address, with {query} for the URL-encoded words.
    SEARCH_PATH = "/search?q={query}"
    #: Path pieces that mark a search result link, and which of them are films.
    TITLE_PATHS: Tuple[str, ...] = ("/movies/", "/series/")
    MOVIE_PATHS: Tuple[str, ...] = ("/movies/",)
    #: Substrings of a URL that mean the visit landed signed out.
    SIGNED_OUT_MARKS: Tuple[str, ...] = ("/login", "/signin", "/welcome")
    TRAILING_SLASH = False

    AUDIO_MENU = '[data-testid*="audio"] li, [role="menuitemradio"], [class*="audio-track"] li'
    PROFILE_CARDS = '[data-testid*="profile"] button, [class*="profile-item"], [class*="profiles"] li a'

    USERNAME_SELECTORS = ('input[name="email"]', 'input[type="email"]', "#email")
    PASSWORD_SELECTORS = ('input[name="password"]', 'input[type="password"]', "#password")
    SUBMIT_SELECTORS = (
        'button[type="submit"]',
        'button[aria-label*="Continue" i]',
        'button[aria-label*="Log in" i]',
        'button[aria-label*="Sign in" i]',
    )
    OTP_INPUT_SELECTORS = (
        'input[autocomplete="one-time-code"]',
        'input[name="code"]',
        'input[inputmode="numeric"]',
    )
    CAPTCHA_SELECTORS = ('iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]', '[class*="captcha"]')
    LOGGED_IN_SELECTORS = ('[data-testid*="profile"]', 'a[href*="/account"]')
    PLAY_SELECTORS = (
        'a[aria-label*="Watch" i]',
        'button[aria-label*="Watch" i]',
        'button[aria-label*="Play" i]',
        '[data-testid*="play"]',
    )

    def _signed_out(self, url: str) -> bool:
        return any(mark in url for mark in self.SIGNED_OUT_MARKS)

    # -- session -----------------------------------------------------------

    def is_logged_in(self, page) -> bool:
        try:
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as error:
            logger.debug(f"[{self.name}] navigation failed: {error}")
            return False
        if self._signed_out(page.url):
            return False
        return self.first_visible(page, self.LOGGED_IN_SELECTORS, timeout=8000) is not None

    def login(self, page, username: str, password: str, job) -> None:
        from bot.auth.session import AuthState

        job.set_state(AuthState.Launching)
        page.goto(self.HOME + self.LOGIN_PATH, wait_until="domcontentloaded", timeout=60000)
        job.set_state(AuthState.Filling)

        user_field = self.first_visible(page, self.USERNAME_SELECTORS, timeout=15000)
        if user_field is None:
            logger.info(f"[{self.name}] no email field on {page.url}")
            job.fail(f"The {self.SITE} sign-in page did not look as expected.")
            return
        user_field.fill(username)

        pass_field = self.first_visible(page, self.PASSWORD_SELECTORS, timeout=3000)
        if pass_field is None:
            # Email first, then a second step for the password.
            submit = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=8000)
            if submit is not None:
                submit.click()
            pass_field = self.first_visible(page, self.PASSWORD_SELECTORS, timeout=20000)
        if pass_field is None:
            logger.info(f"[{self.name}] no password field on {page.url}")
            job.fail(f"{self.SITE} did not ask for a password where expected.")
            return
        pass_field.fill(password)

        submit = self.first_visible(page, self.SUBMIT_SELECTORS, timeout=10000)
        if submit is None:
            job.fail(f"Could not find the {self.SITE} sign-in button.")
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
            code = job.request_otp(f"{self.SITE} sent a one-time passcode.")
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

        if self._signed_out(page.url):
            job.fail(f"{self.SITE} rejected that email address or password.")
            return
        job.succeed()

    # -- profiles ----------------------------------------------------------

    def list_profiles(self, page) -> List[Dict[str, Any]]:
        try:
            page.goto(self.HOME + self.PROFILES_PATH, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            return page.evaluate(
                """(selector) => {
                    const out = [];
                    document.querySelectorAll(selector).forEach((n, i) => {
                        const label = (n.getAttribute('aria-label') || n.textContent || '').trim();
                        if (label) out.push({ id: String(i), name: label });
                    });
                    return out;
                }""",
                self.PROFILE_CARDS,
            ) or []
        except Exception as error:
            logger.debug(f"[{self.name}] profile listing failed: {error}")
            return []

    def select_profile(self, page, profile_id: str) -> bool:
        try:
            index = int(profile_id)
        except (TypeError, ValueError):
            return False
        try:
            clicked = page.evaluate(
                """([selector, i]) => {
                    const nodes = document.querySelectorAll(selector);
                    if (i < 0 || i >= nodes.length) return false;
                    nodes[i].click();
                    return true;
                }""",
                [self.PROFILE_CARDS, index],
            )
            if clicked:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            return bool(clicked)
        except Exception as error:
            logger.debug(f"[{self.name}] profile selection failed: {error}")
            return False

    # -- finding things ----------------------------------------------------

    def search(self, page, query: str) -> List[Dict[str, Any]]:
        try:
            page.goto(
                self.HOME + self.SEARCH_PATH.format(query=quote(query)),
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(2500)
        except Exception as error:
            logger.debug(f"[{self.name}] search navigation failed: {error}")
            return []
        try:
            return page.evaluate(
                """([titlePaths, moviePaths, scheme]) => {
                    const out = [];
                    const seen = new Set();
                    const selector = titlePaths.map(p => `a[href*="${p}"]`).join(', ');
                    document.querySelectorAll(selector).forEach(a => {
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
                            kind: moviePaths.some(p => href.includes(p)) ? 'title' : 'series',
                            url: scheme + href.replace(/^https?:\\/\\/[^/]+/, '').replace(/^\\//, '')
                        });
                    });
                    return out.slice(0, 25);
                }""",
                [list(self.TITLE_PATHS), list(self.MOVIE_PATHS), self.SCHEME],
            ) or []
        except Exception as error:
            logger.debug(f"[{self.name}] search scrape failed: {error}")
            return []

    # -- playback ----------------------------------------------------------

    def title_url(self, url: str) -> str:
        """The page to open: a pasted link as itself, a search result under HOME."""
        if url.startswith("https://"):
            return url
        path = url.replace(self.SCHEME, "").strip("/")
        if not path:
            raise ValueError(f"Not a {self.SITE} title: {url!r}")
        return f"{self.HOME}/{path}" + ("/" if self.TRAILING_SLASH else "")

    def play(self, page, track) -> None:
        page.goto(self.title_url(getattr(track, "url", "") or ""), wait_until="domcontentloaded", timeout=60000)

        play_button = self.first_visible(page, self.PLAY_SELECTORS, timeout=15000)
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
            logger.info(f"[{self.name}] no playing video on {page.url}")
            raise RuntimeError(f"{self.SITE} did not start playing: {error}") from error

    def pause(self, page) -> None:
        page.evaluate("() => { const v = document.querySelector('video'); if (v) v.pause(); }")

    def resume(self, page) -> None:
        page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play(); }")

    # -- audio tracks ------------------------------------------------------

    def list_audio_tracks(self, page) -> List[Dict[str, Any]]:
        try:
            return page.evaluate(
                """(menu) => {
                    const v = document.querySelector('video');
                    if (v && v.audioTracks && v.audioTracks.length) {
                        return Array.from(v.audioTracks).map((t, i) => ({
                            id: t.id || String(i), label: t.label || t.language || ''
                        }));
                    }
                    const out = [];
                    document.querySelectorAll(menu).forEach((n, i) => {
                        const label = (n.getAttribute('aria-label') || n.textContent || '').trim();
                        if (label) out.push({ id: String(i), label });
                    });
                    return out;
                }""",
                self.AUDIO_MENU,
            ) or []
        except Exception as error:
            logger.debug(f"[{self.name}] audio tracks unavailable: {error}")
            return []

    def set_audio_track(self, page, track_id: str) -> bool:
        try:
            return bool(page.evaluate(
                """([id, menu]) => {
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
                    const nodes = document.querySelectorAll(menu);
                    const i = parseInt(id, 10);
                    if (!isNaN(i) && i >= 0 && i < nodes.length) { nodes[i].click(); return true; }
                    return false;
                }""",
                [str(track_id), self.AUDIO_MENU],
            ))
        except Exception as error:
            logger.debug(f"[{self.name}] setting the audio track failed: {error}")
            return False


__all__ = ["VideoSiteAdapter"]
