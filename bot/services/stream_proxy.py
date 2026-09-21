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

None of that applies to a live broadcast, which is not a file and has no byte
offsets to window. Those take a separate path, _relay_live, which walks the
stream's segments with `&sq=N`. See that method for what a byte range does to
a live URL and why it is impossible to notice.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

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

# A resolved videoplayback URL is signed for the address that resolved it. When
# the bridge resolves through YOUTUBE_PROXY_URL, fetching that URL from this
# host directly is refused with a 403, so the relay must leave by the same door.
_PROXIED_HOST_SUFFIXES = ("googlevideo.com", "youtube.com")


def _upstream_proxies(target_url: str) -> Optional[Dict[str, str]]:
    """requests' `proxies` for a YouTube CDN URL, or None to fetch directly."""
    proxy = os.environ.get("YOUTUBE_PROXY_URL", "").strip()
    host = (urlsplit(target_url).hostname or "").lower()
    # A dot boundary, so "evilgooglevideo.com" does not match by suffix alone.
    if proxy and any(host == d or host.endswith("." + d) for d in _PROXIED_HOST_SUFFIXES):
        return {"http": proxy, "https": proxy}
    return None

_CHUNK_READ_BYTES = 65536

# A live stream is addressed by segment, not by byte offset. Asking one for a
# byte range is answered with 206 and a Content-Length and then *no body at
# all* -- see _relay_live. These bound the segment walk instead.
#
# The timeout is generous on purpose: a request for the segment after the live
# edge blocks until that segment exists, which is the pacing we want, not a
# stall. Segments run a few seconds each, so a minute and a half of silence
# means something is actually wrong.
_LIVE_SEGMENT_TIMEOUT_SECONDS = 90
# Consecutive failures before concluding the broadcast has ended rather than
# hiccupped. A stream that has genuinely finished answers every sq the same way.
_LIVE_SEGMENT_RETRIES = 3
# How far behind the live edge the walk may drift before skipping forward.
# Roughly a minute at typical segment lengths.
_LIVE_MAX_LAG_SEGMENTS = 12

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")
_CONTENT_RANGE_RE = re.compile(r"bytes \d+-\d+/(\d+)")
_SEQUENCE_RE = re.compile(r"([?&])sq=[^&]*")


def is_live_url(url: str) -> bool:
    """Whether a resolved stream URL is a live broadcast rather than a file.

    YouTube marks these itself: `live=1` on the signed URL, and `noclen=1`
    because a broadcast in progress has no content length. Either is enough;
    both are present in practice.
    """
    query = parse_qs(urlsplit(url).query)
    return query.get("live", [""])[0] == "1" or query.get("noclen", [""])[0] == "1"


def with_sequence(url: str, seq: int) -> str:
    """The same URL asking for segment `seq`, replacing any sq already on it."""
    if _SEQUENCE_RE.search(url):
        return _SEQUENCE_RE.sub(lambda m: f"{m.group(1)}sq={seq}", url, count=1)
    return f"{url}{'&' if '?' in url else '?'}sq={seq}"


def head_sequence_number(headers: Any) -> Optional[int]:
    """The newest segment YouTube has produced, from its own response header."""
    try:
        return int(headers.get("X-Head-Seqnum"))
    except (TypeError, ValueError):
        return None


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
        self._token = self.path.lstrip("/")
        entry = proxy.lookup(self._token)
        if entry is None:
            self.send_error(404)
            return
        target_url, headers = entry

        if is_live_url(target_url):
            self._relay_live(target_url, headers, head_only)
            return

        start, end = _parse_range(self.headers.get("Range"))
        if end is not None and (end - start + 1) <= _UPSTREAM_CHUNK_BYTES:
            self._relay_single(target_url, headers, start, end, head_only)
        else:
            self._relay_windowed(target_url, headers, start, end, head_only)

    def _fetch_segment(
        self, target_url: str, headers: Dict[str, str], seq: Optional[int]
    ) -> Optional["requests.Response"]:
        """One live segment, or the current one when `seq` is None."""
        url = target_url if seq is None else with_sequence(target_url, seq)
        try:
            return requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=(5, _LIVE_SEGMENT_TIMEOUT_SECONDS),
                proxies=_upstream_proxies(target_url),
            )
        except requests.RequestException as error:
            logging.warning(f"[StreamProxy] live segment fetch failed: {error}")
            return None

    def _relay_live(self, target_url: str, headers: Dict[str, str], head_only: bool) -> None:
        """Relay a live broadcast by walking its segments, not its byte offsets.

        A live stream is not a file, and asking one for a byte range does not
        fail in any way a caller can see: YouTube answers **206 with a
        Content-Length and then sends no body at all**. Measured three times
        running against a real broadcast on 2026-09-21 -- 206, "700000 bytes
        to follow", nothing, connection dropped after ~36s. The windowed path
        therefore promised mpv a body it never received, mpv reported no error
        because the stream was open and valid, and the track "played" silently
        for twelve minutes across five stream-refresh attempts without one log
        line anywhere. Re-resolving cannot help; every fresh URL does the same.

        `&sq=N` is how the live endpoint is actually addressed. It returns one
        complete segment per request, and a request for the segment after the
        live edge blocks until that segment exists -- which is exactly the
        real-time pacing a broadcast wants, rather than something to time out.
        """
        first = self._fetch_segment(target_url, headers, None)
        if first is None:
            self.send_error(502)
            return
        if first.status_code not in (200, 206):
            self.send_response(first.status_code)
            self.end_headers()
            first.close()
            return

        seq = head_sequence_number(first.headers)
        self.send_response(200)
        content_type = first.headers.get("Content-Type")
        if content_type:
            self.send_header("Content-Type", content_type)
        # Deliberately no Content-Length and no Accept-Ranges: a broadcast has
        # no length and cannot be seeked, so mpv must read until we close.
        self.end_headers()
        if head_only:
            first.close()
            return

        written, client_gone = self._write_chunks(first)
        first.close()
        self._warn_if_empty(written, f"the current segment of {target_url.split('?')[0]}")
        if client_gone:
            return
        if seq is None:
            # Nothing to advance from. One segment is a few seconds of audio,
            # so this would otherwise look like a track that ended instantly.
            logging.warning(
                "[StreamProxy] live stream served no X-Head-Seqnum, so only one "
                f"segment could be relayed for token {getattr(self, '_token', '?')}; "
                "playback will stop after a few seconds"
            )
            return

        misses = 0
        seq += 1
        while True:
            resp = self._fetch_segment(target_url, headers, seq)
            status = resp.status_code if resp is not None else None
            if status != 200:
                if resp is not None:
                    resp.close()
                misses += 1
                if misses > _LIVE_SEGMENT_RETRIES:
                    logging.info(
                        f"[StreamProxy] live stream stopped serving at sq={seq} "
                        f"(last status {status}); treating the broadcast as ended"
                    )
                    return
                time.sleep(1)
                continue
            written, client_gone = self._write_chunks(resp)
            head = head_sequence_number(resp.headers)
            resp.close()
            if client_gone:
                return
            if written == 0:
                # A 200 carrying nothing counts against the same budget as a
                # refusal. Without this the walk would spin on empty successes
                # for ever, which is the very failure this path exists to end.
                self._warn_if_empty(written, f"live segment sq={seq}")
                misses += 1
                if misses > _LIVE_SEGMENT_RETRIES:
                    logging.warning(
                        f"[StreamProxy] live stream kept returning empty segments at "
                        f"sq={seq}; giving up rather than looping silently"
                    )
                    return
                time.sleep(1)
                continue
            misses = 0
            # Falling behind the live edge is drift, not an error -- skip to it
            # rather than walking an ever-growing backlog in real time.
            if head is not None and head - seq > _LIVE_MAX_LAG_SEGMENTS:
                logging.info(
                    f"[StreamProxy] live relay was {head - seq} segments behind; "
                    f"skipping from sq={seq} to the live edge at sq={head}"
                )
                seq = head
            seq += 1

    def _warn_if_empty(self, written: int, what: str) -> None:
        """An upstream success that carried no body at all.

        This is the shape of failure that cost twelve minutes of silence: the
        status says yes, so nothing downstream treats it as an error, and mpv
        holds an open stream that never produces a sample. It has to be said
        out loud somewhere, and this is the only place that knows.
        """
        if written == 0:
            logging.warning(
                f"[StreamProxy] upstream returned success but no body for {what} "
                f"(token {getattr(self, '_token', '?')}); mpv will sit silent on an "
                "open stream rather than report an error"
            )

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
                    target_url,
                    headers={**headers, "Range": f"bytes={start}-{end}"},
                    stream=True,
                    timeout=(5, 30),
                    proxies=_upstream_proxies(target_url),
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
            # Not finished, and the last window carried nothing: about to ask
            # for the same bytes again. Checked here rather than straight after
            # the write so a genuinely complete file never trips it.
            self._warn_if_empty(written, f"the window at byte {cursor}")
            window_end = cursor + _UPSTREAM_CHUNK_BYTES - 1
            if limit is not None:
                window_end = min(window_end, limit - 1)
            resp = self._fetch_window(target_url, headers, cursor, window_end)
            if resp is None or resp.status_code not in (200, 206):
                status = resp.status_code if resp is not None else "no response"
                if resp is not None:
                    resp.close()
                # Headers promising the whole file went out long ago, so there
                # is no way left to tell mpv this failed: it sees the body stop
                # early and reports a clean EOF, which the player used to take
                # for a finished track and silently skip. Player.on_end_file
                # now catches that by duration, but this is the only place that
                # knows *why*, and it said nothing at any log level.
                logging.warning(
                    f"[StreamProxy] upstream refused {status} at byte {cursor}"
                    f"{'' if total is None else f' of {total}'} for token "
                    f"{getattr(self, '_token', '?')}; the response to mpv is "
                    "truncated and will look like a short track"
                )
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
