from __future__ import annotations
import logging
import time
from threading import Thread
from queue import Queue
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from bot.commands import CommandProcessor


class Task:
    def __init__(
        self, command_id: int, function: Callable[..., None], args: Any, kwargs: Any
    ) -> None:
        self.command_id = command_id
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.queued_at = time.perf_counter()


class TaskProcessor(Thread):
    def __init__(self, command_processor: CommandProcessor) -> None:
        super().__init__(daemon=True)
        self.command_processor = command_processor
        self.task_queue: Queue[Task] = Queue()

    def run(self) -> None:
        while True:
            task = self.task_queue.get()
            queue_wait_ms = (time.perf_counter() - task.queued_at) * 1000
            function_name = getattr(task.function, "__qualname__", repr(task.function))
            logging.info(
                "[PlaybackTiming] task_started "
                f"function={function_name!r} queue_wait_ms={queue_wait_ms:.2f} "
                f"pending_tasks={self.task_queue.qsize()}"
            )
            if task.command_id == self.command_processor.current_command_id:
                try:
                    started_at = time.perf_counter()
                    task.function(*task.args, **task.kwargs)
                    elapsed_ms = (time.perf_counter() - started_at) * 1000
                    logging.info(
                        "[PlaybackTiming] task_completed "
                        f"function={function_name!r} elapsed_ms={elapsed_ms:.2f}"
                    )
                except Exception as e:
                    logging.error(f"TaskProcessor: Error executing task: {e}", exc_info=True)
