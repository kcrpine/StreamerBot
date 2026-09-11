"""Local relay so mpv never issues a single big fetch against googlevideo.com.

mpv/ffmpeg opens a progressive stream URL with one HTTP request for a
*Range: bytes=0-* -- open-ended, "everything from here to the end of the
file". Google's CDN answers a bare 403 Forbidden to a single-request range
past some size, confirmed by probing the cutoff directly with Python's
`requests` against resolved stream URLs. That cutoff is not a fixed constant:
one video tolerated up to 1,000,000 bytes and 403'd at 2,000,000; another
403'd anywhere past ~950,000-999,999. A request already bounded well under
either succeeds outright, which is what made this look like it was about TLS
clients or a transient hiccup before the byte-range probe was tried: a quick
`curl`/`requests` smoke test almost always uses a small explicit Range and
never reproduces it.

So this hands mpv a plain http://127.0.0.1:<port>/<token> URL and does the
real fetch here, in bounded windows with margin under the lower of the two
observed cutoffs, concatenating them into one continuous response so mpv sees
an ordinary, fully streamable file with no idea any of this happened. Since
the cutoff drifts per video/session rather than holding at one number, a
window that still gets 403'd is retried once at half size rather than failing
the whole track over what is a sizing guess, not a hard limit. Loopback-only,
like auth_portal and go-librespot's API port: this proxies whatever URL it is
given, which is only safe to expose to the bot's own player.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import requests

# Matches the bridge's own resolve cache TTL, so a token roughly outlives the
# stream URL it points at.
_TOKEN_TTL_SECONDS = 3600
# A handful of tracks' worth (current + prefetched + a few stream-refresh
# retries); old entries are pruned by TTL anyway, this just bounds growth
# over a very long session.
_MAX_ENTRIES = 64

# Comfortably under the lowest single-request cutoff observed so far (see
# module docstring) -- not a hard limit, just a starting guess; a window that
# still gets 403'd is halved and retried, down to _MIN_WINDOW_BYTES. Any
# client request bounded under this goes upstream unchanged in one shot;
# anything open-ended or larger is fetched as a sequence of windows this size.
_UPSTREAM_CHUNK_BYTES = 700_000
_MIN_WINDOW_BYTES = 50_000

_CHUNK_READ_BYTES = 65536

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")
_CONTENT_RANGE_RE = re.compile(r"bytes \d+-\d+/(\d+)")


def _parse_range(range_header: Optional[str]) -> Tuple[int, Optional[int]]:
    """("bytes=100-200" -> (100, 200)); no header, or an end-less range, -> (0/start, None)."""
    if not range_header:
        return 0, None
    match = _RANGE_RE.match(range_header.strip())
    if not match:
        return 0, None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else None
    return start, end


def _total_size(content_range: Optional[str]) -> Optional[int]:
    if not content_range:
        return None
    match = _CONTENT_RANGE_RE.match(content_range.strip())
    return int(match.group(1)) if match else None


class _ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logging.debug("[StreamProxy] " + format, *args)

    def do_GET(self) -> None:
        self._relay(head_only=False)

    def do_HEAD(self) -> None:
        self._relay(head_only=True)

    def _relay(self, head_only: bool) -> None:
        proxy: "StreamProxy" = self.server.stream_proxy  # type: ignore[attr-defined]
        entry = proxy.lookup(self.path.lstrip("/"))
        if entry is None:
            self.send_error(404)
            return
        target_url, headers = entry

        start, end = _parse_range(self.headers.get("Range"))
        if end is not None and (end - start + 1) <= _UPSTREAM_CHUNK_BYTES:
            self._relay_single(target_url, headers, start, end, head_only)
        else:
            self._relay_windowed(target_url, headers, start, end, head_only)

    def _fetch_window(
        self, target_url: str, headers: Dict[str, str], start: int, end: int
    ) -> Optional["requests.Response"]:
        """GET bytes=start-end from upstream.

        The CDN's per-request size cutoff isn't a fixed constant -- it has
        been observed to vary per video, sometimes below _UPSTREAM_CHUNK_BYTES
        -- so a 403 on the requested size is treated as "the guess was too
        big" and retried once at half the span, down to _MIN_WINDOW_BYTES,
        rather than failing the whole track over a sizing guess.
        """
        while True:
            try:
                resp = requests.get(
                    target_url, headers={**headers, "Range": f"bytes={start}-{end}"}, stream=True, timeout=(5, 30)
                )
            except requests.RequestException as error:
                logging.warning(f"[StreamProxy] upstream fetch failed: {error}")
                return None
            span = end - start + 1
            if resp.status_code == 403 and span > _MIN_WINDOW_BYTES:
                resp.close()
                end = start + span // 2 - 1
                continue
            return resp

    def _relay_single(
        self, target_url: str, headers: Dict[str, str], start: int, end: int, head_only: bool
    ) -> None:
        """A request already within the CDN's per-request limit: one upstream fetch, relayed as-is."""
        resp = self._fetch_window(target_url, headers, start, end)
        if resp is None:
            self.send_error(502)
            return
        try:
            self.send_response(resp.status_code)
            for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                value = resp.headers.get(header)
                if value is not None:
                    self.send_header(header, value)
            self.end_headers()
            if not head_only:
                self._write_chunks(resp)
        finally:
            resp.close()

    def _relay_windowed(
        self,
        target_url: str,
        headers: Dict[str, str],
        start: int,
        end: Optional[int],
        head_only: bool,
    ) -> None:
        """An open-ended or too-large request: fetch it upstream as a sequence of
        bounded windows, but present one continuous, ordinary response to mpv."""
        cursor = start
        window_end = cursor + _UPSTREAM_CHUNK_BYTES - 1
        if end is not None:
            window_end = min(window_end, end)
        resp = self._fetch_window(target_url, headers, cursor, window_end)
        if resp is None:
            self.send_error(502)
            return

        if resp.status_code not in (200, 206):
            self.send_response(resp.status_code)
            self.end_headers()
            resp.close()
            return

        total = _total_size(resp.headers.get("Content-Range"))
        limit = (end + 1) if end is not None else total  # exclusive upper bound, if known

        self.send_response(206)
        self.send_header("Accept-Ranges", "bytes")
        content_type = resp.headers.get("Content-Type")
        if content_type:
            self.send_header("Content-Type", content_type)
        if total is not None:
            last = (limit - 1) if limit is not None else (total - 1)
            self.send_header("Content-Range", f"bytes {start}-{last}/{total}")
            self.send_header("Content-Length", str(last - start + 1))
        self.end_headers()

        if head_only:
            resp.close()
            return

        while True:
            written, client_gone = self._write_chunks(resp)
            cursor += written
            resp.close()
            if client_gone:
                return
            if limit is not None and cursor >= limit:
                return
            if total is not None and cursor >= total:
                return
            window_end = cursor + _UPSTREAM_CHUNK_BYTES - 1
            if limit is not None:
                window_end = min(window_end, limit - 1)
            resp = self._fetch_window(target_url, headers, cursor, window_end)
            if resp is None or resp.status_code not in (200, 206):
                if resp is not None:
                    resp.close()
                return

    def _write_chunks(self, resp: "requests.Response") -> Tuple[int, bool]:
        """Write one response's body to the client.

        Returns (bytes_written, client_gone). client_gone means mpv closed the
        connection early (seek, stop, track change) -- the caller must stop
        entirely rather than fetch another window nobody will read.
        """
        written = 0
        for chunk in resp.iter_content(_CHUNK_READ_BYTES):
            if not chunk:
                continue
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return written, True
            written += len(chunk)
        return written, False


class StreamProxy:
    """Owns the loopback relay server and the url/headers it currently knows about."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4420) -> None:
        self._host = host
        self._port = port
        self._entries: Dict[str, Tuple[str, Dict[str, str], float]] = {}
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def available(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        try:
            server = ThreadingHTTPServer((self._host, self._port), _ProxyHandler)
        except OSError as error:
            logging.warning(
                f"[StreamProxy] could not bind {self._host}:{self._port}: {error}. "
                "YouTube playback will fetch stream URLs directly instead, which is "
                "more likely to be refused by YouTube's CDN for some videos."
            )
            return
        server.daemon_threads = True
        server.stream_proxy = self
        self._server = server
        self._port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, daemon=True, name="StreamProxy")
        self._thread.start()
        logging.info(f"[StreamProxy] listening on http://{self._host}:{self._port}")

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def register(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        """Return a local URL that relays to `url`, or `url` itself if the proxy is down."""
        if self._server is None:
            return url
        self._prune()
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._entries[token] = (url, dict(headers or {}), time.monotonic())
        return f"http://{self._host}:{self._port}/{token}"

    def lookup(self, token: str) -> Optional[Tuple[str, Dict[str, str]]]:
        with self._lock:
            entry = self._entries.get(token)
        if entry is None:
            return None
        url, headers, _ = entry
        return url, headers

    def _prune(self) -> None:
        cutoff = time.monotonic() - _TOKEN_TTL_SECONDS
        with self._lock:
            expired = [token for token, (_, _, ts) in self._entries.items() if ts < cutoff]
            for token in expired:
                del self._entries[token]
            overflow = len(self._entries) - _MAX_ENTRIES
            if overflow > 0:
                oldest = sorted(self._entries.items(), key=lambda kv: kv[1][2])[:overflow]
                for token, _ in oldest:
                    del self._entries[token]
