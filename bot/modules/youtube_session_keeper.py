"""Keeping this bot's YouTube session alive, and saying so when it has ended.

A Google session cannot be renewed on the user's behalf once Google has ended
it; if it could, that would be a hole in Google. What can be done is to stop it
ending unnecessarily. Google rotates part of the session and treats one that
stops rotating as stale, and a live browser rotates it just by loading
youtube.com. So every few hours this loads youtube.com in the bot's own Chrome
profile, asks YouTube whether it is still signed in, and stores the fresh
cookies for the bridge.

It is a thread in the bot process, not a cron job: it runs in the container that
already holds this bot's Chrome profile, so it is per bot for free and never
reaches into another bot's directory.

The schedule counts from the last check recorded on disk, not from start-up.
Bots restart on every update, and a timer that started over each time might
never fire on a bot that updates daily.

On arm64 there is no Chrome. An imported session is then used as it is, and goes
stale on Google's schedule; the keeper has nothing to refresh it with and says
so rather than pretending.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

from bot.auth.cookies import CookieFileError, has_google_session, parse_netscape
from bot.services.youtube_session import SOURCE_BROWSER, SOURCE_IMPORT, YouTubeSessionStore

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger(__name__)

SERVICE = "yt"

# A failed resolution is the most direct evidence a session just went stale, so
# it triggers a refresh, but no more than this often: a dead session fails every
# request, and each refresh loads a page in Chrome.
ON_DEMAND_MIN_INTERVAL_SECONDS = 600

# How long a held play request waits for a refresh before being told it failed.
HOLD_TIMEOUT_SECONDS = 150

# Cookies pasted or uploaded on the import page. A real youtube.com export is a
# few kilobytes; anything far larger is the wrong file.
MAX_IMPORT_BYTES = 512 * 1024


class RefreshResult(Enum):
    Alive = "alive"            # signed in; cookies stored if they changed
    Ended = "ended"            # YouTube says signed out: someone must sign in again
    NoSession = "no_session"   # nothing to refresh
    Unavailable = "unavailable"  # no browser here, or it failed; state unknown


class ImportProblem(Enum):
    Empty = "empty"
    TooLarge = "too_large"
    NotCookies = "not_cookies"
    NoGoogleSession = "no_google_session"
    SessionEnded = "session_ended"


def is_login_required(error: BaseException) -> bool:
    """Whether a bridge error is YouTube refusing playback for want of a sign-in."""
    text = str(error)
    return "LOGIN_REQUIRED" in text or "Sign in to confirm" in text


class YouTubeSessionKeeper:
    def __init__(
        self,
        bot: Bot,
        store: Optional[YouTubeSessionStore] = None,
        engine_getter: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.bot = bot
        self.config = bot.config.services.yt
        self.store = store or YouTubeSessionStore(
            os.path.join(bot.config_manager.config_dir, "youtube_auth")
        )
        self._engine_getter = engine_getter or (lambda: bot.player.engines.get("browser"))
        self._clock = clock
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._last_result: Optional[RefreshResult] = None
        self._last_on_demand = 0.0
        self._thread: Optional[threading.Thread] = None
        self._closing = threading.Event()

    # -- what is here ------------------------------------------------------

    @property
    def engine(self):
        engine = self._engine_getter()
        if engine is None or not getattr(self.config, "browser_sign_in", True):
            return None
        return engine

    @property
    def browser_available(self) -> bool:
        return self.engine is not None

    @property
    def refresh_interval_seconds(self) -> float:
        hours = float(getattr(self.config, "session_refresh_hours", 6) or 0)
        return hours * 3600

    def status(self) -> str:
        return self.store.status()

    @property
    def is_refreshing(self) -> bool:
        return not self._idle.is_set()

    def wait_until_idle(self, timeout: float = HOLD_TIMEOUT_SECONDS) -> Optional[RefreshResult]:
        """Block until no refresh is running. Returns the last result, or None on timeout."""
        if not self._idle.wait(timeout):
            return None
        return self._last_result

    # -- the schedule ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="YouTubeSessionKeeper", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closing.set()

    def is_due(self) -> bool:
        """Whether the scheduled refresh should run now. Pure apart from the clock and disk."""
        interval = self.refresh_interval_seconds
        if interval <= 0 or not self.store.has_session() or self.store.needs_sign_in():
            return False
        checked_at = float(self.store.meta().get("checked_at") or 0)
        return self._clock() - checked_at >= interval

    def _run(self) -> None:
        # Let the bot reach TeamTalk and the browser finish starting first.
        if self._closing.wait(120):
            return
        while not self._closing.is_set():
            try:
                if self.is_due() and self.browser_available:
                    self.refresh("scheduled")
            except Exception as error:  # noqa: BLE001 - the keeper must not die
                logger.error(f"[youtube session] scheduled refresh failed: {error}", exc_info=True)
            if self._closing.wait(60):
                return

    # -- refreshing --------------------------------------------------------

    def refresh(self, reason: str) -> RefreshResult:
        """Load youtube.com in the bot's profile and store what it says.

        Single flight: a second caller while one refresh runs waits for it and
        gets the same answer, rather than loading the page twice.
        """
        if not self._lock.acquire(blocking=False):
            result = self.wait_until_idle()
            return result or RefreshResult.Unavailable
        self._idle.clear()
        try:
            result = self._refresh_locked(reason)
            self._last_result = result
            return result
        finally:
            self._idle.set()
            self._lock.release()

    def _refresh_locked(self, reason: str) -> RefreshResult:
        if not self.store.has_session():
            return RefreshResult.NoSession
        engine = self.engine
        if engine is None:
            return RefreshResult.Unavailable
        try:
            exported = engine.export_session(SERVICE)
        except Exception as error:
            logger.warning(f"[youtube session] could not load YouTube to refresh ({reason}): {error}")
            return RefreshResult.Unavailable

        if not exported.get("logged_in"):
            self.store.mark_needs_sign_in()
            logger.warning(
                f"[youtube session] {self._bot_name()}: YouTube says this bot is no longer "
                f"signed in ({reason}). Someone must sign in again with li yt; the "
                "keeper will not retry until they do."
            )
            return RefreshResult.Ended

        try:
            changed = self.store.save(
                exported.get("cookies") or [], exported.get("datasync_id", ""),
                self.store.meta().get("source") or SOURCE_BROWSER,
            )
        except ValueError:
            # Signed in according to the page, but the cookies could not sign a
            # request in. Keep the file that works rather than replace it.
            logger.warning("[youtube session] YouTube reported signed in but returned no usable cookies")
            return RefreshResult.Unavailable
        logger.info(
            f"[youtube session] refreshed ({reason}); "
            + ("cookies rotated and stored" if changed else "cookies unchanged")
        )
        return RefreshResult.Alive

    def can_refresh_now(self) -> bool:
        """Whether a refused request may trigger a refresh: a browser, and not too soon after the last."""
        if not self.browser_available:
            return False
        return self._clock() - self._last_on_demand >= ON_DEMAND_MIN_INTERVAL_SECONDS

    def on_login_required(self) -> RefreshResult:
        """A resolution was refused for want of a sign-in. Find out whether the session is dead."""
        if not self.store.has_session():
            return RefreshResult.NoSession
        if self.store.needs_sign_in():
            return RefreshResult.Ended
        if self.is_refreshing:
            return self.wait_until_idle() or RefreshResult.Unavailable
        now = self._clock()
        if now - self._last_on_demand < ON_DEMAND_MIN_INTERVAL_SECONDS:
            return self._last_result or RefreshResult.Unavailable
        self._last_on_demand = now
        return self.refresh("playback was refused")

    # -- connecting --------------------------------------------------------

    def finish_browser_sign_in(self, job) -> None:
        """The adapter signed Google in; store the session, then finish the job.

        The job is finished here, not in the adapter, so the portal never says
        "connected" before the bridge has a session it can use.
        """
        engine = self.engine
        try:
            exported = engine.export_session(SERVICE) if engine else {}
        except Exception as error:
            logger.error(f"[youtube session] could not read the new session: {error}", exc_info=True)
            exported = {}
        if not exported.get("logged_in"):
            job.fail(self.bot.translator.translate(
                "Google signed in, but YouTube did not recognise the session. Please try again."
            ))
            return
        try:
            self.store.save(exported["cookies"], exported.get("datasync_id", ""), SOURCE_BROWSER)
        except ValueError:
            job.fail(self.bot.translator.translate(
                "Google signed in, but YouTube did not recognise the session. Please try again."
            ))
            return
        logger.info(f"[youtube session] {self._bot_name()} signed in to YouTube through the browser")
        job.succeed()

    def import_text(self, text: str) -> Tuple[bool, Optional[ImportProblem]]:
        """Store a cookies.txt file from the user's own browser.

        With Chrome here, the cookies go into the bot's profile and YouTube is
        asked whether they sign in, so a dead session is refused at the door and
        the keep-alive can take it from there. Without Chrome they are stored as
        they are, after checking they could sign a request in at all.
        """
        if not text or not text.strip():
            return False, ImportProblem.Empty
        if len(text.encode("utf-8", "replace")) > MAX_IMPORT_BYTES:
            return False, ImportProblem.TooLarge
        try:
            cookies = parse_netscape(text)
        except CookieFileError:
            return False, ImportProblem.NotCookies
        if not has_google_session(cookies):
            return False, ImportProblem.NoGoogleSession

        engine = self.engine
        if engine is None:
            self.store.save(cookies, "", SOURCE_IMPORT)
            logger.info(f"[youtube session] {self._bot_name()} imported a session (no browser to check it with)")
            return True, None

        with self._lock:
            self._idle.clear()
            try:
                exported = engine.import_session(SERVICE, cookies)
            except Exception as error:
                logger.warning(f"[youtube session] could not check the imported session: {error}")
                exported = None
            finally:
                self._idle.set()

        if exported is None:
            # The browser failed, not the session. Store it; the keep-alive checks later.
            self.store.save(cookies, "", SOURCE_IMPORT)
            return True, None
        if not exported.get("logged_in"):
            return False, ImportProblem.SessionEnded
        try:
            self.store.save(exported["cookies"], exported.get("datasync_id", ""), SOURCE_IMPORT)
        except ValueError:
            self.store.save(cookies, exported.get("datasync_id", ""), SOURCE_IMPORT)
        logger.info(f"[youtube session] {self._bot_name()} imported a session and YouTube confirmed it")
        return True, None

    def sign_out(self) -> None:
        self.store.clear()
        engine = self.engine
        if engine is not None:
            try:
                engine.sign_out(SERVICE)
            except Exception as error:
                logger.warning(f"[youtube session] clearing the browser profile failed: {error}")

    # -- held requests -----------------------------------------------------

    def hold(self, retry: Callable[[], Optional[str]], on_result: Callable[[str], None],
             failed_message: str, ended_message: Optional[str] = None) -> None:
        """Run retry once the refresh in progress finishes, and report what happened.

        A request that arrives during a refresh is played when it finishes rather
        than dropped: asking a screen reader user to find and re-paste a link they
        already sent is avoidable work. Exactly one message follows the "please
        wait" one, whichever way it goes.
        """
        def run() -> None:
            if not self._idle.wait(HOLD_TIMEOUT_SECONDS):
                on_result(failed_message)
                return
            # An import also holds requests and records no refresh result, so
            # "no result" means "go ahead", not "failed".
            if self._last_result is RefreshResult.Ended and self.store.needs_sign_in():
                on_result(ended_message or failed_message)
                return
            try:
                message = retry()
            except Exception as error:  # noqa: BLE001 - reported, not raised
                logger.error(f"[youtube session] held request failed: {error}", exc_info=True)
                message = failed_message
            if message:
                on_result(message)

        threading.Thread(target=run, name="YouTubeHeldRequest", daemon=True).start()

    def _bot_name(self) -> str:
        return os.getenv("TTBOT_INSTANCE", "") or "this bot"


__all__ = [
    "HOLD_TIMEOUT_SECONDS",
    "ImportProblem",
    "MAX_IMPORT_BYTES",
    "ON_DEMAND_MIN_INTERVAL_SECONDS",
    "RefreshResult",
    "YouTubeSessionKeeper",
    "is_login_required",
]
