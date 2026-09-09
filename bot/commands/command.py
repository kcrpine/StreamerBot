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
