"""Deciding whether to play a title with audio description.

For a blind user this is the difference between a film being watchable and being
ninety minutes of unexplained silence, so the bot asks rather than guessing, and
remembers the answer when told to.

The prompt is time-limited on purpose. An unanswered question must never leave
playback hanging: whoever asked may have walked away, and a channel full of
people waiting on a prompt nobody will answer is worse than a wrong default.
When the timer fires the configured default is used and the bot says which.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Long enough to hear the question and answer it, short enough that a forgotten
# prompt does not hold the channel.
PROMPT_TIMEOUT_SECONDS = 30


class AudioDescriptionPreference:
    """Per-user preference, resolved as user choice, then config default."""

    ALWAYS = "always"
    NEVER = "never"
    ASK = "ask"

    def __init__(self, default: str = ASK) -> None:
        self._default = default if default in (self.ALWAYS, self.NEVER, self.ASK) else self.ASK
        self._by_user: Dict[int, str] = {}
        self._lock = threading.RLock()

    def get(self, user_id: Optional[int]) -> str:
        with self._lock:
            if user_id is not None and user_id in self._by_user:
                return self._by_user[user_id]
            return self._default

    def set(self, user_id: int, value: str) -> None:
        with self._lock:
            self._by_user[user_id] = value

    def clear(self, user_id: int) -> None:
        with self._lock:
            self._by_user.pop(user_id, None)

    @property
    def default(self) -> str:
        return self._default


class PendingPrompt:
    """One outstanding "with audio description?" question."""

    def __init__(self, user_id: int, service: str, on_answer: Callable[[bool, bool], None],
                 timeout: float = PROMPT_TIMEOUT_SECONDS) -> None:
        self.user_id = user_id
        self.service = service
        self.created_at = time.time()
        self._on_answer = on_answer
        self._answered = threading.Event()
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def answer(self, enable: bool, remember: bool = False) -> bool:
        """False when the prompt has already been answered or has expired."""
        if self._answered.is_set():
            return False
        self._answered.set()
        self._timer.cancel()
        try:
            self._on_answer(enable, remember)
        except Exception as error:
            logger.error(f"[audio description] answer handler failed: {error}")
        return True

    def _expire(self) -> None:
        if self._answered.is_set():
            return
        self._answered.set()
        logger.info("[audio description] prompt timed out; using the default")
        try:
            # remember=False: a timeout is not a decision and must not be stored.
            self._on_answer(False, False)
        except Exception as error:
            logger.error(f"[audio description] timeout handler failed: {error}")

    @property
    def is_answered(self) -> bool:
        return self._answered.is_set()

    def cancel(self) -> None:
        self._answered.set()
        self._timer.cancel()


def parse_answer(text: str) -> Optional[tuple]:
    """Map a reply to (enable, remember), or None when it is not an answer.

    Accepts the numbered options and the obvious words, because a user who hears
    "1. Yes" will sometimes type "yes" and should not be told it is an unknown
    command.
    """
    value = (text or "").strip().lower()
    mapping = {
        "1": (True, False), "yes": (True, False), "y": (True, False),
        "2": (False, False), "no": (False, False), "n": (False, False),
        "3": (True, True),
        "4": (False, True),
    }
    return mapping.get(value)


def prompt_text(translator, title: str = "") -> str:
    """The question, as one message.

    One message rather than five: each send is its own screen reader
    announcement, and a five-part question read as five interruptions is far
    harder to follow than one sentence with four options in it.
    """
    heading = (
        translator.translate("%(title)s has audio description. Play it with the description?")
        % {"title": title}
        if title
        else translator.translate("This has audio description. Play it with the description?")
    )
    return (
        f"{heading} "
        + translator.translate("1. Yes. 2. No. 3. Yes, and remember. 4. No, and remember.")
    )


__all__ = [
    "AudioDescriptionPreference",
    "PendingPrompt",
    "parse_answer",
    "prompt_text",
    "PROMPT_TIMEOUT_SECONDS",
]
