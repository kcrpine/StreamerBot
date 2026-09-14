"""Browser cookies as Netscape cookies.txt, in both directions.

Two things in this bot sign in with a real browser's cookies rather than a
password: gamdl downloading from Apple Music, and the YouTube bridge playing as
a signed-in Google account. Both read the cookies.txt format that browser
extensions export and that Python's MozillaCookieJar understands, so the writer
and the parser live here once.

A cookie file is a full login to someone's account. Nothing here logs a cookie
value, and callers are expected to keep the files under the bot's own data/
directory, readable only by the bot.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

HEADER = "# Netscape HTTP Cookie File"

# Session cookies are given this long when written. MozillaCookieJar drops a
# cookie whose expiry is empty or zero unless told otherwise, and every reader
# of these files would then see a session with half its cookies missing.
SESSION_COOKIE_LIFETIME_SECONDS = 86400

# What a signed-in Google session carries on youtube.com. SAPISID (or its
# __Secure- twin) is what the SAPISIDHASH authorization header is computed from,
# and SID is the session itself; without both, a request is not signed in no
# matter how many other cookies come with it.
GOOGLE_AUTH_COOKIES = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")
GOOGLE_SESSION_COOKIES = ("SID", "__Secure-1PSID", "__Secure-3PSID")


def _matches(domain: str, suffixes: Iterable[str]) -> bool:
    bare = domain.lstrip(".").lower()
    return any(bare == s or bare.endswith("." + s) for s in suffixes)


def to_netscape(
    cookies: List[Dict[str, Any]],
    domain_suffixes: Iterable[str],
    now: Optional[float] = None,
) -> str:
    """Playwright-shaped cookies ({name, value, domain, path, expires, secure})
    as a cookies.txt file, keeping only the given domains."""
    suffixes = tuple(domain_suffixes)
    horizon = int((now if now is not None else time.time()) + SESSION_COOKIE_LIFETIME_SECONDS)
    lines = [HEADER, ""]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        name = str(cookie.get("name") or "")
        if not name or not _matches(domain, suffixes):
            continue
        value = str(cookie.get("value") or "")
        # A tab or newline would split the line into different fields.
        if any(ch in name + value + domain for ch in "\t\r\n"):
            continue
        expires = cookie.get("expires")
        expires = int(expires) if isinstance(expires, (int, float)) and expires > 0 else horizon
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(cookie.get("path") or "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(expires),
            name,
            value,
        ]))
    return "\n".join(lines) + "\n"


class CookieFileError(ValueError):
    """The text is not a cookies.txt file."""


def parse_netscape(text: str) -> List[Dict[str, Any]]:
    """A cookies.txt file as Playwright-shaped cookies.

    Tolerant of what real exporters produce: a byte-order mark, Windows line
    endings, the "#HttpOnly_" prefix curl and several extensions write, and a
    missing header. Strict about the one thing that matters, seven tab-separated
    fields, because a paste that lost its tabs is not recoverable and saying so
    beats storing half a session.
    """
    cookies: List[Dict[str, Any]] = []
    malformed = 0
    for raw in (text or "").lstrip("\ufeff").splitlines():
        line = raw.strip("\r")
        http_only = False
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
            http_only = True
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            malformed += 1
            continue
        domain, _subdomains, path, secure, expires, name, value = fields
        try:
            expiry = int(float(expires))
        except ValueError:
            expiry = 0
        cookies.append({
            "domain": domain.strip(),
            "path": path or "/",
            "secure": secure.strip().upper() == "TRUE",
            "expires": expiry if expiry > 0 else -1,
            "name": name,
            "value": value,
            "httpOnly": http_only,
        })
    if not cookies:
        raise CookieFileError(
            "no cookie lines" if not malformed else "lines are not tab separated"
        )
    return cookies


def has_google_session(cookies: List[Dict[str, Any]]) -> bool:
    """Whether these cookies could sign a youtube.com request in.

    Necessary, not sufficient: only YouTube can say whether the session is still
    alive, and the keep-alive asks it. This only rejects a file that could never
    work, such as one exported while signed out or for the wrong site.
    """
    names = {
        c.get("name") for c in cookies
        if c.get("value") and _matches(str(c.get("domain") or ""), ("youtube.com",))
    }
    return bool(names & set(GOOGLE_AUTH_COOKIES)) and bool(names & set(GOOGLE_SESSION_COOKIES))


__all__ = [
    "CookieFileError",
    "GOOGLE_AUTH_COOKIES",
    "GOOGLE_SESSION_COOKIES",
    "has_google_session",
    "parse_netscape",
    "to_netscape",
]
