from __future__ import annotations

import logging
import time
from typing import Any, TYPE_CHECKING, Callable

from bot.commands.task_processor import Task

if TYPE_CHECKING:
    from bot.commands import CommandProcessor


class Command:
    def __init__(self, command_processor: CommandProcessor):
        self._bot = command_processor.bot
        self.cache = command_processor.cache
        self.cache_manager = command_processor.cache_manager
        self.command_processor = command_processor
        self.config = command_processor.config
        self.config_manager = command_processor.config_manager
        self.module_manager = command_processor.module_manager
        self.player = command_processor.player
        self.service_manager = command_processor.service_manager
        self._task_processor = command_processor.task_processor
        self.ttclient = command_processor.ttclient
        self.translator = command_processor.translator

    @property
    def help(self) -> str:
        return self.translator.translate("help text not found")

    def run_async(self, func: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        task = Task(id(self), func, args, kwargs)
        self._task_processor.task_queue.put(task)
        function_name = getattr(func, "__qualname__", repr(func))
        logging.info(
            "[PlaybackTiming] task_queued "
            f"function={function_name!r} "
            f"pending_tasks={self._task_processor.task_queue.qsize()}"
        )

    def send_message_async(self, *args: Any, **kwargs: Any) -> None:
        import threading
        threading.Thread(
            target=self.ttclient.send_message,
            args=args,
            kwargs=kwargs,
            daemon=True,
            name="TT_MessageSender",
        ).start()

    # -- YouTube session (Phase 9) ----------------------------------------
    #
    # Commands run on the bot's main loop, so nothing here waits for a refresh:
    # the requester gets one message now and one when it is over, and the
    # request itself is played when the refresh finishes rather than dropped.

    def youtube_keeper(self) -> Any:
        return getattr(self.command_processor, "youtube_session", None)

    def youtube_hold_or_run(
        self, request: str, user: Any, run: Callable[[], Any]
    ) -> Any:
        """Run a YouTube request, unless a refresh is running or it is refused for want of a sign-in.

        run() returns the reply for a successful request and raises ServiceError
        otherwise. Returns the reply to send now; any follow-up is sent by the
        keeper's thread. Errors that are not about the session are re-raised for
        the caller's existing handling.
        """
        from bot import errors
        from bot.modules.youtube_session_keeper import RefreshResult, is_login_required

        keeper = self.youtube_keeper()
        if keeper is None:
            return run()

        def send(text: str) -> None:
            self.ttclient.send_message(text, user)

        skipped = self.translator.translate(
            "The YouTube sign-in could not be renewed, so {request} was not played."
        ).format(request=request)

        def retry() -> Any:
            try:
                return run()
            except errors.ServiceError:
                return skipped

        if keeper.is_refreshing:
            keeper.hold(retry, send, skipped, self.youtube_signed_out_message())
            return self.youtube_renewing_message(request)

        try:
            return run()
        except errors.ServiceError as error:
            if not is_login_required(error):
                raise
            if not keeper.store.has_session():
                return self.translator.translate(
                    "YouTube refused to play this without a signed-in account, which is "
                    "usual on a server. To connect one, send this command: li yt"
                )
            if keeper.store.needs_sign_in():
                return self.youtube_signed_out_message()
            if not keeper.can_refresh_now():
                raise

            def renew_then_retry() -> None:
                result = keeper.on_login_required()
                if result is RefreshResult.Ended:
                    send(self.youtube_signed_out_message())
                elif result is RefreshResult.Alive:
                    reply = retry()
                    if reply:
                        send(reply)
                else:
                    send(skipped)

            import threading

            threading.Thread(target=renew_then_retry, name="YouTubeRenew", daemon=True).start()
            return self.youtube_renewing_message(request)

    def youtube_renewing_message(self, request: str) -> str:
        return self.translator.translate(
            "Renewing the YouTube sign-in. {request} will start playing when it "
            "finishes, usually within a minute."
        ).format(request=request)

    def youtube_signed_out_message(self) -> str:
        # The command goes last, after a colon and with no full stop, so a
        # screen reader's review cursor finds it at the end and nothing trailing
        # is taken as part of it.
        return self.translator.translate(
            "Google signed StreamerBot out of YouTube. To sign in again, send this command: li yt"
        )

    def search_tracks(self, query: str, limit: int | None = None) -> Any:
        service = self.service_manager.service
        started_at = time.perf_counter()
        self._last_search_started_at = started_at
        self._last_search_query = query
        logging.info(
            "[PlaybackTiming] search_started "
            f"service={service.name} query={query!r} limit={limit!r}"
        )
        try:
            tracks = service.search(query, limit=limit)
        except Exception:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            logging.info(
                "[PlaybackTiming] search_failed "
                f"elapsed_ms={elapsed_ms:.2f} service={service.name} "
                f"query={query!r}"
            )
            raise
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logging.info(
            "[PlaybackTiming] search_completed "
            f"elapsed_ms={elapsed_ms:.2f} service={service.name} "
            f"results={len(tracks)} query={query!r}"
        )
        return tracks
