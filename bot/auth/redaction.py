"""Keep secrets out of the logs.

Users are asked to paste logs when something breaks, and the bot now handles
real account passwords, OAuth tokens and one-time codes. A filter on the root
logger is the only place that catches every path at once, including tracebacks
from libraries that never heard of this project.

Two layers, because either alone is insufficient:

- **Registered values.** Exact strings we know are secret. Catches a password
  appearing in a library's exception text, which no pattern would match.
- **Patterns.** Catches secrets we were never told about: a bearer token in a
  request dump, a refresh_token in a JSON body.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Iterable, List, Pattern, Tuple

REDACTED = "[redacted]"

# Only values at least this long are registered. Redacting a two-character
# password would blank out unrelated text across every log line.
MIN_REGISTERED_LENGTH = 4

# (pattern, replacement) where group 1 is the label to keep.
PATTERNS: List[Tuple[Pattern[str], str]] = [
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1=" + REDACTED),
    (re.compile(r"(?i)\b(refresh_token|access_token|id_token|api_key|apikey|client_secret)"
                r"\s*[\"']?\s*[=:]\s*[\"']?[\w.\-]+"), r"\1=" + REDACTED),
    (re.compile(r"(?i)\bBearer\s+[\w.\-]+"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\b(cookie|set-cookie)\s*:\s*\S+"), r"\1: " + REDACTED),
]


class SecretRedactingFilter(logging.Filter):
    """Rewrites records so registered secrets and secret-shaped text never print.

    Installed on the root logger, and also on every handler: a filter on the
    logger alone does not run for records that propagate up from child loggers,
    which is exactly where library tracebacks come from.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._values: List[str] = []

    # -- registration ------------------------------------------------------

    def register(self, *values: str) -> None:
        with self._lock:
            for value in values:
                if value and len(value) >= MIN_REGISTERED_LENGTH and value not in self._values:
                    self._values.append(value)
            # Longest first, so a password that contains a shorter secret is
            # replaced whole rather than leaving a fragment behind.
            self._values.sort(key=len, reverse=True)

    def register_all(self, values: Iterable[str]) -> None:
        self.register(*values)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    # -- scrubbing ---------------------------------------------------------

    def scrub(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            values = list(self._values)
        for value in values:
            if value in text:
                text = text.replace(value, REDACTED)
        for pattern, replacement in PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Render now: the args are scrubbed along with the message, and a
            # record that has already been formatted cannot leak later.
            message = record.getMessage()
        except Exception:
            return True

        scrubbed = self.scrub(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()

        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)

        return True


_filter = SecretRedactingFilter()


def get_filter() -> SecretRedactingFilter:
    return _filter


def install(logger: logging.Logger | None = None) -> SecretRedactingFilter:
    """Attach the filter to the root logger and each of its handlers."""
    root = logger or logging.getLogger()
    if _filter not in root.filters:
        root.addFilter(_filter)
    for handler in root.handlers:
        if _filter not in handler.filters:
            handler.addFilter(_filter)
    return _filter


__all__ = ["SecretRedactingFilter", "get_filter", "install", "REDACTED"]
