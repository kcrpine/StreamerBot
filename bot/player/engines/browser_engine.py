"""Playback through a real browser, for services with no usable API.

Netflix, Disney+, Apple Music and Amazon Music are DRM-protected and have no
public playback API. The only way to play them is a browser that holds a
Widevine licence, so this drives **real Google Chrome** through Playwright.

Three constraints shape everything here, and none of them are negotiable:

**Headful, under Xvfb.** Headless Chrome produces no audio at all, and Netflix
detects it. So Chrome runs normally against the virtual display the entrypoint
starts, and its audio goes into the same PulseAudio null sink every other engine
feeds.

**Real Chrome, not Chromium.** `channel="chrome"` uses the Chrome installed in
the image, which is the only build carrying the Widevine CDM. It also skips
`playwright install`, saving roughly 400 MB. On arm64 there is no Google Chrome
at all, so this engine refuses to start and its services disable themselves with
a spoken reason rather than failing at playback.

**One thread owns Playwright.** The sync API is not thread-safe, and this bot
calls in from the mpv event thread, from per-command threads, and from
TaskProcessor. Every operation is therefore a job on a queue, executed by one
worker thread that exclusively owns the Playwright instance. Callers block on a
result. This is the single most important property of this file: touching the
page from any other thread produces failures that look like site changes.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from bot import errors
from bot.player.engines import PlaybackEngine

if TYPE_CHECKING:
    from bot.player.track import Track

logger = logging.getLogger(__name__)

BROWSER_MARKER = "/etc/streamerbot-browser-available"

# Generous: a cold Chrome start on a small VPS plus a Netflix page load.
DEFAULT_JOB_TIMEOUT = 120
STARTUP_TIMEOUT = 90

# The two tabs each service gets. PLAYER is where the audio is, and only
# playback may navigate it; AUX is for everything that loads a page for its own
# reasons — searching, checking the sign-in, the login form, profiles, sessions.
# They share the service's persistent context, so they share its cookies.
PLAYER = "player"
AUX = "aux"


class _Job:
    """One unit of work for the browser thread, plus somewhere to put the result."""

    __slots__ = ("fn", "result", "error", "done", "name")

    def __init__(self, fn: Callable[..., Any], name: str = "") -> None:
        self.fn = fn
        self.name = name or getattr(fn, "__name__", "job")
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.done = threading.Event()


class BrowserEngine(PlaybackEngine):
    name = "browser"
    supports_seek = True
    supports_speed = False
    supports_audio_description = True

    def __init__(self, data_dir: str, display: str = ":99") -> None:
        super().__init__()
        self._dir = data_dir
        self._display = display
        self._queue: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._closing = False
        self._ready = threading.Event()
        self._start_error: Optional[str] = None

        # Owned by the worker thread only. Nothing else may touch these.
        self._playwright = None
        self._contexts: Dict[str, Any] = {}
        self._pages: Dict[Tuple[str, str], Any] = {}

        self._adapters: Dict[str, Any] = {}
        self._active_service: Optional[str] = None
        self._playing = False

    # -- availability ------------------------------------------------------

    @staticmethod
    def is_supported() -> bool:
        """False on arm64, where Google publishes no Chrome and so no Widevine."""
        try:
            with open(BROWSER_MARKER, encoding="utf-8") as f:
                return f.read().strip() == "1"
        except OSError:
            return False

    def register_adapter(self, service: str, adapter: Any) -> None:
        self._adapters[service] = adapter

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        if not self.is_supported():
            raise errors.EngineUnavailableError(
                "Google Chrome is not available on this architecture, so Netflix, "
                "Disney Plus, Apple Music and Amazon Music cannot play here."
            )
        try:
            import playwright  # noqa: F401
        except ImportError as error:
            raise errors.EngineUnavailableError(
                "Playwright is not installed in this image."
            ) from error

        os.makedirs(self._dir, mode=0o700, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run, name="BrowserEngine", daemon=True
        )
        self._thread.start()

        if not self._ready.wait(timeout=STARTUP_TIMEOUT):
            raise errors.EngineUnavailableError("The browser did not start in time.")
        if self._start_error:
            raise errors.EngineUnavailableError(self._start_error)

    def _run(self) -> None:
        """The only thread that ever touches Playwright."""
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._ready.set()
            logger.info("Browser engine ready")
        except Exception as error:
            self._start_error = str(error)
            self._ready.set()
            logger.error(f"[browser] Could not start Playwright: {error}")
            return

        while not self._closing:
            job = self._queue.get()
            if job is None:
                break
            try:
                job.result = job.fn()
            except BaseException as error:  # noqa: BLE001 - reported to the caller
                job.error = error
                logger.debug(f"[browser] job {job.name} failed: {error}")
            finally:
                job.done.set()

        self._teardown()

    def _teardown(self) -> None:
        for service, context in list(self._contexts.items()):
            try:
                context.close()
            except Exception as error:
                logger.debug(f"[browser] closing {service} context failed: {error}")
        self._contexts.clear()
        self._pages.clear()
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as error:
                logger.debug(f"[browser] stopping playwright failed: {error}")
            self._playwright = None

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=30)

    # -- the job channel ---------------------------------------------------

    def submit(self, fn: Callable[[], Any], timeout: float = DEFAULT_JOB_TIMEOUT,
               name: str = "") -> Any:
        """Run fn on the browser thread and return its result.

        Blocks. Raises whatever fn raised, so callers see real errors rather
        than a sentinel they might forget to check.
        """
        if self._closing or self._thread is None or not self._thread.is_alive():
            raise errors.ServiceError("The browser is not running.")
        job = _Job(fn, name)
        self._queue.put(job)
        if not job.done.wait(timeout=timeout):
            raise errors.ServiceError(
                f"The browser did not finish {job.name} in {int(timeout)} seconds."
            )
        if job.error is not None:
            raise job.error
        return job.result

    # -- contexts ----------------------------------------------------------

    def _context_for(self, service: str):
        """Persistent context per service. Worker thread only.

        Separate directories mean a Netflix sign-in cannot see a Disney+ one,
        and deleting one service's profile does not sign the others out.
        """
        context = self._contexts.get(service)
        if context is not None:
            return context

        profile_dir = os.path.join(self._dir, service)
        os.makedirs(profile_dir, mode=0o700, exist_ok=True)
        context = self._playwright.chromium.launch_persistent_context(
            profile_dir,
            channel="chrome",
            headless=False,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-features=Translate",
                "--no-first-run",
                "--no-default-browser-check",
                # Playwright otherwise leaves navigator.webdriver true, which is
                # the first thing a streaming site checks. Measured: true
                # without this flag, false with it.
                "--disable-blink-features=AutomationControlled",
                # Chrome refuses to run as root without this, and the container
                # user is unprivileged anyway.
                "--no-sandbox",
            ],
            # Merged onto the real environment, not passed on its own. Playwright
            # *replaces* the environment when given env=, so {"DISPLAY": ...}
            # alone launched Chrome with no HOME, no XDG_RUNTIME_DIR and no
            # PULSE_* — and a Chrome that cannot find the PulseAudio socket is a
            # Chrome that plays into nothing, which looks exactly like a site
            # that failed to start the video.
            env={**os.environ, "DISPLAY": self._display},
            viewport={"width": 1280, "height": 720},
            ignore_default_args=["--mute-audio"],
        )
        self._contexts[service] = context
        return context

    def _page_for(self, service: str, role: str = PLAYER):
        """A tab for one service and one role. Worker thread only.

        Two roles, because a tab that is producing audio is a playback device
        rather than a browser tab: navigating it ends the audio, with no error,
        no event and nothing in Player.state to say the music stopped. Apple
        Music's search and its is_logged_in both goto() a page, so before this
        split, searching for the next song stopped the current one.

        Both roles share the service's persistent context, so cookies — and
        therefore the sign-in — are the same on both.
        """
        key = (service, role)
        page = self._pages.get(key)
        if page is not None and not page.is_closed():
            return page
        context = self._context_for(service)
        # A page already handed to another role is spoken for. The persistent
        # context opens with one blank tab, which the first role to ask adopts.
        taken = {
            id(other)
            for (name, _), other in self._pages.items()
            if name == service and other is not None and not other.is_closed()
        }
        page = next(
            (p for p in context.pages if id(p) not in taken and not p.is_closed()),
            None,
        )
        if page is None:
            page = context.new_page()
        self._pages[key] = page
        return page

    def page(self, service: str, role: str = PLAYER):
        """A page for an adapter to drive. Call only from the browser thread."""
        return self._page_for(service, role)

    # -- playback ----------------------------------------------------------

    def _adapter(self, service: str):
        adapter = self._adapters.get(service)
        if adapter is None:
            raise errors.ServiceError(f"No browser adapter for {service}.")
        return adapter

    def play(self, track: Track) -> None:
        service = track.service
        adapter = self._adapter(service)

        def job():
            page = self._page_for(service)
            adapter.play(page, track)

        self.submit(job, name=f"play:{service}")
        self._active_service = service
        self._playing = True

    def pause(self) -> None:
        if self._active_service is None:
            return
        service, adapter = self._active_service, self._adapter(self._active_service)
        self.submit(lambda: adapter.pause(self._page_for(service)), timeout=30, name="pause")

    def resume(self) -> None:
        if self._active_service is None:
            return
        service, adapter = self._active_service, self._adapter(self._active_service)
        self.submit(lambda: adapter.resume(self._page_for(service)), timeout=30, name="resume")

    def stop(self) -> None:
        """Leave the player.

        Pausing is not enough on a handover: an advertisement or an autoplaying
        "next episode" will start on its own and be heard over whatever engine
        took over. The adapter navigates away instead.
        """
        self._playing = False
        if self._active_service is None:
            return
        service, adapter = self._active_service, self._adapter(self._active_service)
        try:
            self.submit(lambda: adapter.stop(self._page_for(service)), timeout=30, name="stop")
        except Exception as error:
            logger.warning(f"[browser] stop failed for {service}: {error}")
        self._active_service = None

    # -- transport ---------------------------------------------------------

    def set_volume(self, volume: int) -> None:
        if self._active_service is None:
            return
        service, adapter = self._active_service, self._adapter(self._active_service)
        self.submit(
            lambda: adapter.set_volume(self._page_for(service), volume / 100.0),
            timeout=30, name="set_volume",
        )

    def seek(self, offset: float) -> None:
        if self._active_service is None:
            raise errors.UnsupportedOperationError("seek")
        service, adapter = self._active_service, self._adapter(self._active_service)
        self.submit(
            lambda: adapter.seek(self._page_for(service), offset), timeout=30, name="seek"
        )

    def get_position(self) -> Optional[float]:
        if self._active_service is None:
            return None
        service, adapter = self._active_service, self._adapter(self._active_service)
        try:
            return self.submit(
                lambda: adapter.get_position(self._page_for(service)), timeout=15,
                name="get_position",
            )
        except Exception:
            return None

    def get_duration(self) -> Optional[float]:
        if self._active_service is None:
            return None
        service, adapter = self._active_service, self._adapter(self._active_service)
        try:
            return self.submit(
                lambda: adapter.get_duration(self._page_for(service)), timeout=15,
                name="get_duration",
            )
        except Exception:
            return None

    # -- audio description -------------------------------------------------

    def list_audio_tracks(self, service: str) -> List[Dict[str, Any]]:
        adapter = self._adapter(service)
        return self.submit(
            lambda: adapter.list_audio_tracks(self._page_for(service)),
            timeout=60, name="list_audio_tracks",
        )

    def set_audio_track(self, service: str, track_id: str) -> bool:
        adapter = self._adapter(service)
        return self.submit(
            lambda: adapter.set_audio_track(self._page_for(service), track_id),
            timeout=60, name="set_audio_track",
        )

    def enable_audio_description(self, service: str) -> bool:
        """Switch to a described track if the title offers one.

        Applied after the player has loaded, because the audio menu is not
        populated before then.
        """
        adapter = self._adapter(service)

        def job():
            page = self._page_for(service)
            tracks = adapter.list_audio_tracks(page)
            described = adapter.find_described_track(tracks)
            if described is None:
                return False
            return adapter.set_audio_track(page, described["id"])

        return self.submit(job, timeout=90, name="enable_audio_description")

    # -- sign-in and profiles ---------------------------------------------

    def is_logged_in(self, service: str) -> bool:
        adapter = self._adapter(service)
        try:
            return self.submit(
                lambda: adapter.is_logged_in(self._page_for(service, AUX)),
                timeout=60, name="is_logged_in",
            )
        except Exception:
            return False

    def login(self, service: str, username: str, password: str, job) -> None:
        adapter = self._adapter(service)
        self.submit(
            lambda: adapter.login(self._page_for(service, AUX), username, password, job),
            # Must exceed the slowest honest sign-in: Apple's form was measured
            # taking up to 20 seconds to load and is allowed a minute, the
            # password step 30 seconds, and the verification code alone may wait
            # OTP_TIMEOUT_SECONDS (300). At 300 this gave up on a user who was
            # still typing their code, while the worker carried on underneath.
            timeout=600, name=f"login:{service}",
        )

    def list_profiles(self, service: str) -> List[Dict[str, Any]]:
        adapter = self._adapter(service)
        return self.submit(
            lambda: adapter.list_profiles(self._page_for(service, AUX)),
            timeout=60, name="list_profiles",
        )

    def select_profile(self, service: str, profile_id: str) -> bool:
        adapter = self._adapter(service)
        return self.submit(
            lambda: adapter.select_profile(self._page_for(service, AUX), profile_id),
            timeout=60, name="select_profile",
        )

    def search(self, service: str, query: str) -> List[Dict[str, Any]]:
        adapter = self._adapter(service)
        return self.submit(
            lambda: adapter.search(self._page_for(service, AUX), query),
            timeout=90, name="search",
        )

    def now_playing(self, service: str) -> Optional[Dict[str, Any]]:
        """What the service's own player reports is playing, for adapters that can say."""
        adapter = self._adapter(service)
        if not hasattr(adapter, "now_playing"):
            return None
        return self.submit(
            lambda: adapter.now_playing(self._page_for(service)),
            timeout=30, name="now_playing",
        )

    def cookies(self, service: str) -> List[Dict[str, Any]]:
        """The service profile's cookies, for a downloader acting as the same account.

        Contains the sign-in. The caller must keep it inside the bot's data
        directory and delete it when done, and nothing may log it.
        """
        return self.submit(
            lambda: self._context_for(service).cookies(),
            timeout=30, name="cookies",
        )

    def export_session(self, service: str) -> Dict[str, Any]:
        """Load the service in its profile and read the signed-in session back out.

        For YouTube this is also the keep-alive: loading youtube.com is what makes
        Google rotate the session cookies.
        """
        adapter = self._adapter(service)
        return self.submit(
            lambda: adapter.export_session(self._page_for(service, AUX), self._context_for(service)),
            timeout=120, name=f"export_session:{service}",
        )

    def import_session(self, service: str, cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Load cookies from another browser into this profile, then export as above."""
        adapter = self._adapter(service)
        return self.submit(
            lambda: adapter.import_session(
                self._page_for(service, AUX), self._context_for(service), cookies
            ),
            timeout=120, name=f"import_session:{service}",
        )

    def sign_out(self, service: str) -> None:
        """Drop the whole browser profile: cookies live in it."""
        def job():
            context = self._contexts.pop(service, None)
            for key in [k for k in self._pages if k[0] == service]:
                self._pages.pop(key, None)
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
        try:
            self.submit(job, timeout=60, name="sign_out")
        except Exception as error:
            logger.warning(f"[browser] closing {service} context failed: {error}")

        import shutil

        shutil.rmtree(os.path.join(self._dir, service), ignore_errors=True)


__all__ = ["BrowserEngine"]
