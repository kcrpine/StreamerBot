"""This bot's signed-in YouTube session, as the files the bridge reads.

YouTube stopped serving playback to the TV device-code sign-in, and refuses
anonymous playback from datacenter addresses, so a VPS bot plays as a real
browser session instead: cookies plus the account's DataSync ID, which the
proof-of-origin token has to be bound to (see youtube_bridge/server.mjs).

Layout, under the bot's own data/ directory and nowhere else:

    youtube_auth/            0700
        cookies.txt          0600  Netscape format; youtube.com and google.com
        session.json         0600  datasync_id, source, fingerprint, state

The bridge is one process shared by every bot on the host and finds these at
/bots/<bot_id>/youtube_auth/. It rebuilds a bot's session when cookies.txt
changes, so this module writes that file only when the cookies actually did:
an unchanged rewrite would throw away the bridge's warm session every refresh.

session.json is written before cookies.txt, so a bridge that notices the new
cookies always finds the DataSync ID that belongs to them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from bot.auth.cookies import has_google_session, parse_netscape, to_netscape

logger = logging.getLogger(__name__)

# youtube.com is what the bridge sends; google.com is what keeps the session
# alive when an imported file is loaded back into the bot's own Chrome.
COOKIE_DOMAINS = ("youtube.com", "google.com")

SOURCE_BROWSER = "browser"
SOURCE_IMPORT = "import"


def fingerprint(cookies: List[Dict[str, Any]]) -> str:
    """Identity of a cookie set, ignoring order and expiry.

    Expiry is left out on purpose: session cookies get a fresh horizon every
    time they are written, and counting that as a change would rewrite the file,
    and rebuild the bridge's session, on every single refresh.
    """
    relevant = sorted(
        (str(c.get("domain", "")).lower(), str(c.get("path", "/")), str(c.get("name", "")), str(c.get("value", "")))
        for c in cookies
        if any(str(c.get("domain", "")).lstrip(".").lower().endswith(d) for d in COOKIE_DOMAINS)
    )
    return hashlib.sha256(json.dumps(relevant).encode("utf-8")).hexdigest()


class YouTubeSessionStore:
    def __init__(self, auth_dir: str) -> None:
        self.dir = auth_dir
        self.cookies_path = os.path.join(auth_dir, "cookies.txt")
        self.meta_path = os.path.join(auth_dir, "session.json")
        self._lock = threading.RLock()

    # -- reading -----------------------------------------------------------

    def has_session(self) -> bool:
        return os.path.isfile(self.cookies_path)

    def meta(self) -> Dict[str, Any]:
        try:
            with open(self.meta_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def needs_sign_in(self) -> bool:
        """A session exists and YouTube has said it is no longer signed in."""
        return self.has_session() and bool(self.meta().get("needs_sign_in"))

    def status(self) -> str:
        """connected, expired or disconnected, the portal's vocabulary."""
        if not self.has_session():
            return "disconnected"
        return "expired" if self.needs_sign_in() else "connected"

    def load_cookies(self) -> List[Dict[str, Any]]:
        try:
            with open(self.cookies_path, encoding="utf-8") as f:
                return parse_netscape(f.read())
        except (OSError, ValueError):
            return []

    # -- writing -----------------------------------------------------------

    def save(self, cookies: List[Dict[str, Any]], datasync_id: str, source: str) -> bool:
        """Store a live session. Returns whether anything the bridge reads changed.

        Refuses cookies that could not sign a request in, so a refresh that ran
        on a signed-out page can never overwrite a working file with a dead one.
        """
        if not has_google_session(cookies):
            raise ValueError("these cookies carry no Google sign-in")

        with self._lock:
            old = self.meta()
            new_fingerprint = fingerprint(cookies)
            changed = (
                not self.has_session()
                or old.get("fingerprint") != new_fingerprint
                or (datasync_id and old.get("datasync_id") != datasync_id)
            )
            meta = {
                "datasync_id": datasync_id or old.get("datasync_id", ""),
                "source": source,
                "fingerprint": new_fingerprint,
                "needs_sign_in": False,
                "checked_at": int(time.time()),
                "updated_at": int(time.time()) if changed else old.get("updated_at", int(time.time())),
            }
            self._ensure_dir()
            self._write(self.meta_path, json.dumps(meta))
            if changed:
                self._write(self.cookies_path, to_netscape(cookies, COOKIE_DOMAINS))
            return bool(changed)

    def mark_needs_sign_in(self) -> None:
        with self._lock:
            if not self.has_session():
                return
            meta = self.meta()
            meta["needs_sign_in"] = True
            meta["checked_at"] = int(time.time())
            self._write(self.meta_path, json.dumps(meta))

    def mark_checked(self) -> None:
        with self._lock:
            meta = self.meta()
            if not meta:
                return
            meta["checked_at"] = int(time.time())
            self._write(self.meta_path, json.dumps(meta))

    def clear(self) -> None:
        with self._lock:
            for path in (self.cookies_path, self.meta_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            # The device-code sign-in that Phase 9 retired left its tokens here.
            # Signing out removes them too, rather than leaving a credential
            # nothing reads any more.
            try:
                os.remove(os.path.join(self.dir, "credentials.json"))
            except FileNotFoundError:
                pass

    def _ensure_dir(self) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    @staticmethod
    def _write(path: str, content: str) -> None:
        """0600 from the first byte, and atomic, so the bridge never reads half a file."""
        tmp = f"{path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)


__all__ = [
    "COOKIE_DOMAINS",
    "SOURCE_BROWSER",
    "SOURCE_IMPORT",
    "YouTubeSessionStore",
    "fingerprint",
]
