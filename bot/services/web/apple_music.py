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
import re
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

    # What Apple's search page is made of, measured on 14 September 2026 for
    # "adventure of a lifetime":
    #
    # - Top Results come first, as <a data-testid="click-action"> whose
    #   aria-label carries the kind: "Adventure of a Lifetime · Song · Coldplay",
    #   "Coldplay · Artist", "A Head Full of Dreams · Album · Coldplay". This is
    #   Apple's own answer to the query, in Apple's own order.
    # - Shelves of albums and singles follow, as <a data-testid="product-lockup-link">
    #   labelled "Title, Artist", each repeated by a -title and a -subtitle link.
    # - Song rows are click-action links to /album/...?i=<song id>.
    #
    # The old scraper took every album, playlist and artist link anywhere on the
    # page. Signed in, that includes the sidebar's own library, which is how a
    # search for a Coldplay song played the user's "Favorite Songs" playlist.
    # Only result links inside <main> are taken now, never navigation or /library/.
    SEARCH_SCRAPE_JS = """() => {
        const main = document.querySelector('main') || document.body;
        const out = [];
        const seen = new Set();
        const KINDS = {song: 'track', album: 'album', artist: 'artist', playlist: 'playlist'};

        const absolute = (href) => href.startsWith('http') ? href : 'https://music.apple.com' + href;
        const excluded = (a, href) =>
            !href || href.includes('/library/') || href.includes('/music-video/')
            || a.closest('nav, aside, [role="navigation"]');
        const kindFromHref = (href) => {
            if (href.includes('?i=')) return 'track';
            if (href.includes('/playlist/')) return 'playlist';
            if (href.includes('/artist/')) return 'artist';
            if (href.includes('/album/')) return 'album';
            return null;
        };
        const add = (item) => {
            if (seen.has(item.url)) return;
            seen.add(item.url);
            out.push(item);
        };

        // 1. Top Results, which name their own kind.
        main.querySelectorAll('a[data-testid="click-action"][aria-label]').forEach(a => {
            const href = a.getAttribute('href') || '';
            if (excluded(a, href)) return;
            const parts = a.getAttribute('aria-label').split('\\u00b7').map(s => s.trim()).filter(Boolean);
            if (parts.length < 2) return;
            const kind = KINDS[parts[1].toLowerCase()];
            if (!kind) return;  // music videos, stations and anything else not playable here
            add({
                id: absolute(href), url: absolute(href), kind: 'top', top_kind: kind,
                title: parts[0], artist: parts[2] || '',
            });
        });

        // 2. Albums, singles and playlists on the result shelves.
        main.querySelectorAll('a[data-testid="product-lockup-link"]').forEach(a => {
            const href = a.getAttribute('href') || '';
            if (excluded(a, href)) return;
            const kind = kindFromHref(href);
            if (!kind) return;
            const label = (a.getAttribute('aria-label') || '').trim();
            const comma = label.lastIndexOf(', ');
            add({
                id: absolute(href), url: absolute(href), kind,
                title: comma > 0 ? label.slice(0, comma) : label,
                artist: comma > 0 ? label.slice(comma + 2) : '',
            });
        });

        // 3. Song rows, with the artist read from the same row where there is one.
        main.querySelectorAll('a[data-testid="click-action"][href*="?i="]').forEach(a => {
            const href = a.getAttribute('href') || '';
            if (excluded(a, href)) return;
            const title = (a.getAttribute('aria-label') || a.textContent || '').trim();
            if (!title || title.includes('\\u00b7')) return;
            let artist = '';
            const row = a.closest('li, [role="row"], [data-testid*="track"]');
            if (row) {
                const artistLink = row.querySelector('a[href*="/artist/"]');
                if (artistLink) artist = (artistLink.textContent || '').trim();
            }
            add({id: absolute(href), url: absolute(href), kind: 'track', title, artist});
        });

        return out.slice(0, 25);
    }"""

    @staticmethod
    def spoken_title(item: Dict[str, Any]) -> str:
        """The title as read out in a result list.

        A top result says what it is, because its group label only says "Top
        result": "Song: Adventure of a Lifetime, by Coldplay". Other results
        already have their kind as the group label, so they only add the artist.
        """
        title = (item.get("title") or "").strip()
        artist = (item.get("artist") or "").strip()
        by = f", by {artist}" if artist and artist != title else ""
        if item.get("kind") == "top":
            kind_word = {"track": "Song", "album": "Album", "artist": "Artist",
                         "playlist": "Playlist"}.get(item.get("top_kind", ""), "")
            if item.get("top_kind") == "artist":
                return f"{kind_word}: {title}" if kind_word else title
            return f"{kind_word}: {title}{by}" if kind_word else f"{title}{by}"
        return f"{title}{by}"

    SETTLE_POLL_MS = 1000
    SETTLE_MAX_MS = 12000

    def _settled_results(self, page) -> List[Dict[str, Any]]:
        """Scrape once the result list has stopped changing.

        Apple renders a first set of results and then replaces it as the search
        completes. Reading on the first result that appears captured that
        intermediate set: measured, a search whose settled top result is the
        Coldplay song returned five results that did not include Coldplay at all.
        So read repeatedly and return the first list that matches the one before.
        """
        previous = None
        waited = 0
        while True:
            try:
                current = page.evaluate(self.SEARCH_SCRAPE_JS) or []
            except Exception as error:
                logger.debug(f"[apple music] search scrape failed: {error}")
                current = []
            fingerprint = [item.get("url") for item in current]
            if current and fingerprint == previous:
                return current
            if waited >= self.SETTLE_MAX_MS:
                return current
            previous = fingerprint
            page.wait_for_timeout(self.SETTLE_POLL_MS)
            waited += self.SETTLE_POLL_MS

    STOREFRONT_RE = re.compile(r"^https://music\.apple\.com/([a-z]{2})(?:/|$|\?)")

    @classmethod
    def storefront_from_url(cls, url: str) -> Optional[str]:
        match = cls.STOREFRONT_RE.match(url or "")
        return match.group(1) if match else None

    def _storefront(self, page) -> str:
        """The account's two-letter storefront, such as "us" or "gb".

        Needed in every search URL: see search() for what happens without it.
        Read from MusicKit first, because that is the signed-in account's own
        country; then from the address Apple redirected to; then a default.
        """
        if self._musickit_ready(page, timeout=2000):
            try:
                code = page.evaluate(
                    "() => { const k = MusicKit.getInstance();"
                    " return (k.storefrontCountryCode || k.storefrontId || '').toLowerCase(); }"
                )
                # Lowercased here as well as in the page, so a storefront that
                # arrives as "GB" is used rather than silently replaced by "us".
                code = code.strip().lower() if isinstance(code, str) else ""
                if re.fullmatch(r"[a-z]{2}", code):
                    return code
            except Exception:
                pass
        code = self.storefront_from_url(getattr(page, "url", ""))
        if code:
            return code
        try:
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=45000)
            code = self.storefront_from_url(page.url)
        except Exception as error:
            logger.debug(f"[apple music] could not load Apple Music to find the storefront: {error}")
        return code or "us"

    @classmethod
    def search_url(cls, storefront: str, query: str) -> str:
        """The search address, storefront included.

        Measured on 14 September 2026: music.apple.com/search?term=adventure%20of%20a%20lifetime
        redirects to /us/search?term=adventure%2Bof%2Ba%2Blifetime, turning every
        space into a literal plus sign. Apple then searches for
        "adventure+of+a+lifetime", and the top results were Chubb+Bits — an artist
        with a plus in its name — instead of Coldplay. Asking for /us/search
        directly skips the redirect, keeps the spaces, and puts the Coldplay song
        first. quote() encodes a space as %20, never +, and a literal + in the
        query as %2B, so both survive.
        """
        from urllib.parse import quote

        return f"{HOME}/{storefront}/search?term={quote(query, safe='')}"

    def search(self, page, query: str) -> List[Dict[str, Any]]:
        """Scraped, because MusicKit's catalog search needs a developer token.

        Apple's own ordering is preserved: its result page leads with what it
        considers the best match, and re-sorting that produces worse answers.
        """
        try:
            page.goto(self.search_url(self._storefront(page), query),
                      wait_until="domcontentloaded", timeout=45000)
            # The results render after the page loads. Wait for them rather than
            # for a fixed delay, which returned an empty page on a slow host.
            try:
                page.wait_for_selector(
                    'main a[data-testid="click-action"], main a[data-testid="product-lockup-link"]',
                    timeout=20000,
                )
            except Exception:
                page.wait_for_timeout(2500)
        except Exception as error:
            logger.debug(f"[apple music] search navigation failed: {error}")
            return []

        results = self._settled_results(page)

        # A top result that is an artist cannot be queued and played directly,
        # and `p <query>` plays the first result. So artists never lead: they
        # stay in the list under their own kind, where choosing one is deliberate.
        for item in results:
            if item.get("kind") == "top" and item.get("top_kind") == "artist":
                item["kind"] = "artist"
            item["title"] = self.spoken_title(item)
        return results

    # -- playback, through MusicKit ---------------------------------------

    PLAYBACK_STATE_JS = (
        "() => { const k = window.MusicKit && MusicKit.getInstance();"
        " if (!k) return null;"
        " return {isPlaying: !!k.isPlaying, state: k.playbackState,"
        " queue: k.queue ? k.queue.length : 0, authorized: !!k.isAuthorized}; }"
    )

    def play(self, page, track) -> None:
        """Queue the item, then play it.

        The old version opened the item's page and called MusicKit's play() with
        nothing queued. Measured against Apple Music on 14 September 2026, that
        leaves the queue at 0 and playback never starts, for a song and an album
        alike — the 30-second "did not start playing" timeout every time.
        setQueue({url}) then play() started both: the song with a queue of 1 and
        the album with its 11 tracks.
        """
        url = getattr(track, "url", "") or ""
        if not url:
            raise ValueError("No Apple Music URL on that track.")

        # setQueue takes a URL, so there is no need to open the item's own page.
        # Only load Apple Music if MusicKit is not already on the current page.
        on_apple_music = (getattr(page, "url", "") or "").startswith(HOME)
        if not (on_apple_music and self._musickit_ready(page, timeout=2000)):
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=60000)
            if not self._musickit_ready(page):
                raise RuntimeError(
                    "Apple Music's player did not load. Please try again in a moment."
                )

        try:
            queued = page.evaluate(
                """async (u) => {
                    const k = MusicKit.getInstance();
                    await k.setQueue({ url: u, startPlaying: false });
                    if (!k.queue || !k.queue.length) return 0;
                    await k.play();
                    return k.queue.length;
                }""",
                url,
            )
        except Exception as error:
            logger.warning(f"[apple music] setQueue/play failed for {url}: {error}")
            if "/artist/" in url:
                raise RuntimeError(
                    "Apple Music cannot play an artist directly. Search again and "
                    "choose one of their songs or albums."
                ) from error
            raise RuntimeError(
                "Apple Music would not queue that. It may not be available in "
                "your country, or it may have been removed."
            ) from error

        # An empty queue will never start, so say so now rather than after the
        # 30-second playback wait. Measured: an artist page queues nothing, and
        # the message used to arrive 39 seconds after the request.
        if not queued:
            logger.warning(f"[apple music] nothing was queued for {url}")
            if "/artist/" in url:
                raise RuntimeError(
                    "Apple Music cannot play an artist directly. Search again and "
                    "choose one of their songs or albums."
                )
            raise RuntimeError(
                "Apple Music found nothing to play there. Search again and "
                "choose a song, album or playlist."
            )

        try:
            page.wait_for_function(
                "() => { const k = window.MusicKit && MusicKit.getInstance();"
                " return !!(k && k.isPlaying); }",
                timeout=30000,
            )
        except Exception as error:
            state = None
            try:
                state = page.evaluate(self.PLAYBACK_STATE_JS)
            except Exception:
                pass
            # The state is what tells the next person reading the log whether this
            # was an empty queue, a subscription problem, or playback stalling.
            logger.warning(f"[apple music] did not start playing {url}: state={state}")
            if state and not state.get("queue"):
                raise RuntimeError(
                    "Apple Music found nothing to play there. Search again and "
                    "choose a song, album or playlist."
                ) from error
            if state and not state.get("authorized"):
                raise RuntimeError(
                    "Apple Music is not signed in on this bot, so it cannot play "
                    "full songs. Send li am to connect it again."
                ) from error
            raise RuntimeError(
                "Apple Music did not start playing. Check that the account has an "
                "active Apple Music subscription, then try again."
            ) from error

        try:
            if not page.evaluate("() => !!MusicKit.getInstance().isAuthorized"):
                logger.warning(
                    "[apple music] playing without a signed-in session, which plays "
                    "30-second previews only"
                )
        except Exception:
            pass

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
