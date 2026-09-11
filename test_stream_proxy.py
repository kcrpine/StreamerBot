import time
import urllib.error
import urllib.request
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from bot.services.stream_proxy import StreamProxy, _MAX_ENTRIES, _TOKEN_TTL_SECONDS


class StreamProxyRegistrationTests(TestCase):
    def test_register_returns_url_unchanged_when_not_started(self):
        proxy = StreamProxy(port=0)
        result = proxy.register("https://googlevideo.com/videoplayback?x=1")
        self.assertEqual(result, "https://googlevideo.com/videoplayback?x=1")

    def test_register_and_lookup_round_trip(self):
        proxy = StreamProxy(host="127.0.0.1", port=0)
        try:
            proxy.start()
            local_url = proxy.register(
                "https://googlevideo.com/videoplayback?x=1",
                {"User-Agent": "test-ua"},
            )
            self.assertTrue(local_url.startswith(f"http://127.0.0.1:{proxy._port}/"))
            token = local_url.rsplit("/", 1)[-1]
            entry = proxy.lookup(token)
            self.assertEqual(entry, ("https://googlevideo.com/videoplayback?x=1", {"User-Agent": "test-ua"}))
        finally:
            proxy.close()

    def test_lookup_unknown_token_returns_none(self):
        proxy = StreamProxy(port=0)
        proxy.start()
        try:
            self.assertIsNone(proxy.lookup("nonexistent"))
        finally:
            proxy.close()

    def test_prune_drops_expired_entries(self):
        proxy = StreamProxy(port=0)
        proxy.start()
        try:
            old_ts = time.monotonic() - _TOKEN_TTL_SECONDS - 1
            proxy._entries["stale"] = ("https://example.com/old", {}, old_ts)
            proxy._prune()
            self.assertIsNone(proxy.lookup("stale"))
        finally:
            proxy.close()

    def test_prune_caps_entry_count_dropping_oldest_first(self):
        proxy = StreamProxy(port=0)
        proxy.start()
        try:
            now = time.monotonic()
            for i in range(_MAX_ENTRIES + 5):
                proxy._entries[f"t{i}"] = (f"https://example.com/{i}", {}, now + i)
            proxy._prune()
            self.assertEqual(len(proxy._entries), _MAX_ENTRIES)
            # The 5 oldest (lowest timestamp) should be gone, newest kept.
            self.assertIsNone(proxy.lookup("t0"))
            self.assertIsNone(proxy.lookup("t4"))
            self.assertIsNotNone(proxy.lookup(f"t{_MAX_ENTRIES + 4}"))
        finally:
            proxy.close()


class _Response:
    """Just enough of requests.Response's shape for the assertions below."""

    def __init__(self, status_code, content, headers):
        self.status_code = status_code
        self.content = content
        self.headers = headers


class StreamProxyRelayTests(TestCase):
    """Exercises the actual HTTP handler against a real bound server."""

    def setUp(self):
        self.proxy = StreamProxy(host="127.0.0.1", port=0)
        self.proxy.start()
        self.addCleanup(self.proxy.close)

    def _get(self, path, headers=None):
        # Deliberately stdlib urllib, not `requests`: tests below patch
        # bot.services.stream_proxy.requests.get, and since module objects are
        # singletons in sys.modules, that patches every reference to
        # `requests.get`, including a client-side call from this test itself.
        request = urllib.request.Request(f"http://127.0.0.1:{self.proxy._port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return _Response(resp.status, resp.read(), dict(resp.headers))
        except urllib.error.HTTPError as error:
            return _Response(error.code, error.read(), dict(error.headers or {}))

    def test_unknown_token_is_404(self):
        resp = self._get("/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    @patch("bot.services.stream_proxy.requests.get")
    def test_relays_upstream_status_headers_and_body(self, mock_get):
        upstream_resp = Mock()
        upstream_resp.status_code = 206
        upstream_resp.headers = {
            "Content-Type": "audio/webm",
            "Content-Range": "bytes 0-9/10",
            "Accept-Ranges": "bytes",
        }
        upstream_resp.iter_content.return_value = [b"hello", b"world"]
        upstream_resp.close = Mock()
        mock_get.return_value = upstream_resp

        local_url = self.proxy.register("https://googlevideo.com/videoplayback?x=1", {"User-Agent": "ua"})
        token = local_url.rsplit("/", 1)[-1]

        resp = self._get(f"/{token}", headers={"Range": "bytes=0-9"})

        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.content, b"helloworld")
        self.assertEqual(resp.headers.get("Content-Type"), "audio/webm")
        mock_get.assert_called_once()
        call_args, call_kwargs = mock_get.call_args
        self.assertEqual(call_args[0], "https://googlevideo.com/videoplayback?x=1")
        self.assertEqual(call_kwargs["headers"]["User-Agent"], "ua")
        self.assertEqual(call_kwargs["headers"]["Range"], "bytes=0-9")

    @patch("bot.services.stream_proxy.requests.get")
    def test_upstream_failure_returns_502(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("boom")
        local_url = self.proxy.register("https://googlevideo.com/videoplayback?x=1")
        token = local_url.rsplit("/", 1)[-1]

        resp = self._get(f"/{token}")

        self.assertEqual(resp.status_code, 502)

    @patch("bot.services.stream_proxy._UPSTREAM_CHUNK_BYTES", 10)
    @patch("bot.services.stream_proxy.requests.get")
    def test_open_ended_request_is_fetched_as_bounded_windows(self, mock_get):
        # The actual bug: googlevideo.com 403s a single request past ~1-2MB,
        # which is exactly the open "everything from here" range mpv sends by
        # default. With _UPSTREAM_CHUNK_BYTES patched down to 10, a 25-byte
        # file needs three windows (0-9, 10-19, 20-24) -- this checks the
        # proxy fetches each bounded window upstream and still hands mpv one
        # continuous 25-byte body.
        body = b"abcdefghijklmnopqrstuvwxy"  # 25 bytes
        assert len(body) == 25

        def fake_get(url, headers, stream, timeout):
            range_value = headers["Range"]
            start, end = (int(x) for x in range_value.removeprefix("bytes=").split("-"))
            end = min(end, len(body) - 1)
            chunk = body[start:end + 1]
            resp = Mock()
            resp.status_code = 206
            resp.headers = {
                "Content-Type": "audio/webm",
                "Content-Range": f"bytes {start}-{end}/{len(body)}",
            }
            resp.iter_content.return_value = [chunk]
            resp.close = Mock()
            return resp

        mock_get.side_effect = fake_get

        local_url = self.proxy.register("https://googlevideo.com/videoplayback?x=1", {"User-Agent": "ua"})
        token = local_url.rsplit("/", 1)[-1]

        resp = self._get(f"/{token}")  # no Range header at all -> open-ended

        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.content, body)
        self.assertEqual(resp.headers.get("Content-Range"), f"bytes 0-24/{len(body)}")
        self.assertEqual(mock_get.call_count, 3)
        ranges_requested = [call.kwargs["headers"]["Range"] for call in mock_get.call_args_list]
        # The third window is clamped to bytes=20-24, not 20-29: by then the
        # proxy has learned the true file length (25) from the first
        # response's Content-Range and stops asking past the end of the file.
        self.assertEqual(ranges_requested, ["bytes=0-9", "bytes=10-19", "bytes=20-24"])

    @patch("bot.services.stream_proxy._MIN_WINDOW_BYTES", 2)
    @patch("bot.services.stream_proxy._UPSTREAM_CHUNK_BYTES", 10)
    @patch("bot.services.stream_proxy.requests.get")
    def test_a_window_that_403s_is_retried_at_half_size(self, mock_get):
        # The actual second bug: the CDN's per-request cutoff isn't a fixed
        # number -- one video tolerated a chunk this size, another didn't.
        # A 403 on a chunk must shrink and retry, not fail the whole track.
        body = b"abcdefghij"  # 10 bytes, one full-size window
        calls = []

        def fake_get(url, headers, stream, timeout):
            range_value = headers["Range"]
            start, end = (int(x) for x in range_value.removeprefix("bytes=").split("-"))
            calls.append(range_value)
            resp = Mock()
            resp.close = Mock()
            if end - start + 1 > 5:  # anything bigger than 5 bytes is refused
                resp.status_code = 403
                resp.headers = {}
                resp.iter_content.return_value = []
                return resp
            end = min(end, len(body) - 1)
            resp.status_code = 206
            resp.headers = {"Content-Range": f"bytes {start}-{end}/{len(body)}"}
            resp.iter_content.return_value = [body[start:end + 1]]
            return resp

        mock_get.side_effect = fake_get

        local_url = self.proxy.register("https://googlevideo.com/videoplayback?x=1")
        token = local_url.rsplit("/", 1)[-1]

        resp = self._get(f"/{token}")  # open-ended -> first window is bytes=0-9 (10 bytes, gets 403'd)

        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.content, body)
        # bytes=0-9 403s, halves to bytes=0-4 (5 bytes, still refused since
        # the rule above is "> 5"... actually 5 is allowed) and succeeds.
        self.assertIn("bytes=0-9", calls)
        self.assertIn("bytes=0-4", calls)

    @patch("bot.services.stream_proxy.requests.get")
    def test_bounded_request_under_the_chunk_limit_is_a_single_upstream_fetch(self, mock_get):
        upstream_resp = Mock()
        upstream_resp.status_code = 206
        upstream_resp.headers = {"Content-Range": "bytes 0-999/50000", "Content-Type": "audio/webm"}
        upstream_resp.iter_content.return_value = [b"x" * 1000]
        upstream_resp.close = Mock()
        mock_get.return_value = upstream_resp

        local_url = self.proxy.register("https://googlevideo.com/videoplayback?x=1")
        token = local_url.rsplit("/", 1)[-1]

        resp = self._get(f"/{token}", headers={"Range": "bytes=0-999"})

        self.assertEqual(resp.status_code, 206)
        mock_get.assert_called_once()
