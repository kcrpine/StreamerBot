"""Portal access tokens.

The portal has no login page of its own, and adding one would mean asking a
blind user to invent and type yet another password. Instead the only way to mint
a token is a TeamTalk command from a user who already passed
CommandProcessor.check_access, so authentication has happened in TeamTalk and the
token is the proof.

A missing or wrong token gets 404, not 403: a 403 confirms the portal is there
and worth attacking, while a 404 says nothing at all.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Dict, Optional

# 20 hours. A token in the URL is a time limit on user activity, which SC 2.2.1
# Timing Adjustable covers at AA. The portal has no JS timer and cannot warn
# before expiry, so rather than build a countdown that fights every other design
# decision, we sit inside WCAG's 20 Hour Exception, which requires no UI.
DEFAULT_TTL_SECONDS = 72000


class TokenStore:
    def __init__(self, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.RLock()
        self._tokens: Dict[str, dict] = {}

    def mint(self, username: str = "") -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._expire()
            self._tokens[token] = {"username": username, "expires_at": time.time() + self._ttl}
        return token

    def check(self, token: Optional[str]) -> bool:
        """Constant-time comparison against every live token.

        `token in self._tokens` would be a dict lookup, whose timing leaks how
        much of a guessed token was right.
        """
        if not token:
            return False
        with self._lock:
            self._expire()
            candidates = list(self._tokens)
        found = False
        for known in candidates:
            if secrets.compare_digest(token, known):
                found = True
        return found

    def username_for(self, token: str) -> str:
        with self._lock:
            entry = self._tokens.get(token)
        return entry["username"] if entry else ""

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._tokens)
            self._tokens.clear()
        return count

    @property
    def live_count(self) -> int:
        with self._lock:
            self._expire()
            return len(self._tokens)

    def _expire(self) -> None:
        now = time.time()
        for token, entry in list(self._tokens.items()):
            if entry["expires_at"] <= now:
                del self._tokens[token]


__all__ = ["TokenStore", "DEFAULT_TTL_SECONDS"]
