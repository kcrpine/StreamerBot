"""Notice when libmpv's core stops answering, and say so.

Every call into libmpv -- reading a property, setting one, a command -- is
serviced by mpv's core thread. When that thread stops, every caller blocks
forever inside C, where Python cannot time it out or interrupt it. That is what
froze one bot on a shared host for over two hours: a live broadcast ended, mpv's core
stopped, and from then on every play command, ``s`` and even ``rs`` (whose
restart begins by terminating mpv) sat in a call that never returned. Chat
kept arriving and commands kept starting, so it read as "the bot ignores me"
rather than as a crash, and nothing reached the log at any level.

Nothing in-process can recover a core that has stopped, so the remedy is to
detect it, put every thread's stack in the log, and end the process for the
container's restart policy to bring back. A blocked core cannot be detected
by asking it with a timeout, since the asking thread blocks too; so one thread
asks and a second one watches the clock.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
from typing import Any, Callable, Optional

# How often to ask. Reading a property costs mpv microseconds.
MPV_WATCHDOG_INTERVAL_SECONDS = 15.0

# How long an unanswered question means the core has stopped. mpv's core never
# waits on the network itself -- stream I/O is on the demuxer thread and is
# cancellable -- so a healthy core answers in milliseconds even mid-buffering.
# Generous on purpose: a false alarm restarts the bot.
MPV_WATCHDOG_TIMEOUT_SECONDS = 60.0


class MpvWatchdog:
    def __init__(
        self,
        probe: Callable[[], Any],
        on_unresponsive: Callable[[float], None],
        interval: float = MPV_WATCHDOG_INTERVAL_SECONDS,
        timeout: float = MPV_WATCHDOG_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._on_unresponsive = on_unresponsive
        self._interval = interval
        self._timeout = timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._asked_at: Optional[float] = None
        self._fired = False
        self._stop = threading.Event()

    def start(self) -> None:
        for name, target in (
            ("MpvWatchdogProbe", self._probe_loop),
            ("MpvWatchdog", self._watch_loop),
        ):
            threading.Thread(target=target, name=name, daemon=True).start()

    def close(self) -> None:
        self._stop.set()

    def ask(self) -> None:
        """Ask mpv once. Returns only when mpv answers."""
        with self._lock:
            self._asked_at = self._clock()
        try:
            self._probe()
        except Exception as e:  # noqa: BLE001 - an error is still an answer
            logging.debug(f"[MpvWatchdog] probe raised {e!r}; mpv answered")
        with self._lock:
            self._asked_at = None

    def check(self) -> bool:
        """True, once, when a question has gone unanswered past the timeout."""
        with self._lock:
            if self._fired or self._asked_at is None:
                return False
            waited = self._clock() - self._asked_at
            if waited < self._timeout:
                return False
            self._fired = True
        self._on_unresponsive(waited)
        return True

    def _probe_loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.ask()

    def _watch_loop(self) -> None:
        while not self._stop.wait(min(self._interval, self._timeout) / 3):
            if self.check():
                return


def format_all_thread_stacks() -> str:
    """Every Python thread's stack, named, for the log.

    The stuck threads' frames name the mpv call each one is blocked in, which
    is the only record of what the core stopped on.
    """
    names = {t.ident: t.name for t in threading.enumerate()}
    chunks = []
    for ident, frame in sys._current_frames().items():
        chunks.append(
            f'Thread "{names.get(ident, ident)}":\n'
            + "".join(traceback.format_stack(frame))
        )
    return "\n".join(chunks)
