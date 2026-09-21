import time
import urllib.error
import urllib.request
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from bot.services.stream_proxy import (
    StreamProxy,
    _MAX_ENTRIES,
    _TOKEN_TTL_SECONDS,
    _ProxyHandler,
    _upstream_proxies,
    head_sequence_number,
    is_live_url,
    with_sequence,
)


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

        def fake_get(url, headers, stream, timeout, **kwargs):
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
    def test_a_later_window_that_cannot_be_fetched_is_logged(self, mock_get):
        # The response to mpv is truncated here, and mpv reports a truncated
        # stream as a clean EOF -- indistinguishable from a finished track.
        # Player.on_end_file catches that by duration now, but this is the
        # only place that knows the upstream status behind it, and it used to
        # return without logging anything at any level. A whole playlist could
        # fail this way and leave no trace.
        body = b"abcdefghijklmnopqrstuvwxy"  # 25 bytes: 3 windows of 10

        def fake_get(url, headers, stream, timeout, **kwargs):
            start, end = (
                int(x) for x in headers["Range"].removeprefix("bytes=").split("-")
            )
            resp = Mock()
            resp.close = Mock()
            if start == 0:
                end = min(end, len(body) - 1)
                resp.status_code = 206
                resp.headers = {
                    "Content-Type": "audio/webm",
                    "Content-Range": f"bytes {start}-{end}/{len(body)}",
                }
                resp.iter_content.return_value = [body[start:end + 1]]
            else:
                resp.status_code = 403          # every later window refused
                resp.headers = {}
                resp.iter_content.return_value = []
            return resp

        mock_get.side_effect = fake_get

        local_url = self.proxy.register("https://googlevideo.com/videoplayback?x=1", {})
        token = local_url.rsplit("/", 1)[-1]

        with self.assertLogs("root", level="WARNING") as logged:
            with self.assertRaises(Exception) as caught:
                self._get(f"/{token}")

        # The body stops 15 bytes short of the Content-Length already sent.
        # An HTTP client sees an incomplete read; ffmpeg specifically reports
        # "Stream ends prematurely", reconnects, and then calls it EOF.
        self.assertIn("IncompleteRead", repr(caught.exception))
        message = "\n".join(logged.output)
        self.assertIn("upstream refused 403", message)
        self.assertIn("at byte 10", message)
        self.assertIn(token, message)

    @patch("bot.services.stream_proxy._MIN_WINDOW_BYTES", 2)
    @patch("bot.services.stream_proxy._UPSTREAM_CHUNK_BYTES", 10)
    @patch("bot.services.stream_proxy.requests.get")
    def test_a_window_that_403s_is_retried_at_half_size(self, mock_get):
        # The actual second bug: the CDN's per-request cutoff isn't a fixed
        # number -- one video tolerated a chunk this size, another didn't.
        # A 403 on a chunk must shrink and retry, not fail the whole track.
        body = b"abcdefghij"  # 10 bytes, one full-size window
        calls = []

        def fake_get(url, headers, stream, timeout, **kwargs):
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


class UpstreamProxyTests(TestCase):
    """A stream URL is signed for the address that resolved it, so the relay
    must leave through the same proxy the bridge used."""

    CDN = "https://rr2---sn-abc.googlevideo.com/videoplayback?x=1"

    def test_no_proxy_configured_fetches_directly(self):
        with patch.dict("os.environ", {"YOUTUBE_PROXY_URL": ""}):
            self.assertIsNone(_upstream_proxies(self.CDN))

    def test_youtube_cdn_goes_through_the_configured_proxy(self):
        with patch.dict("os.environ", {"YOUTUBE_PROXY_URL": "http://172.17.0.1:8888"}):
            self.assertEqual(
                _upstream_proxies(self.CDN),
                {"http": "http://172.17.0.1:8888", "https": "http://172.17.0.1:8888"},
            )

    def test_other_hosts_are_never_proxied(self):
        with patch.dict("os.environ", {"YOUTUBE_PROXY_URL": "http://172.17.0.1:8888"}):
            self.assertIsNone(_upstream_proxies("http://paralleledition.xyz:8000/radio.mp3"))
            # A lookalike must not match by suffix alone.
            self.assertIsNone(_upstream_proxies("https://evilgooglevideo.com/x"))

    @patch("bot.services.stream_proxy.requests.get")
    def test_the_relay_hands_the_proxy_to_requests(self, mock_get):
        resp = Mock(status_code=206, headers={"Content-Range": "bytes 0-9/10"})
        resp.iter_content.return_value = [b"0123456789"]
        mock_get.return_value = resp
        with patch.dict("os.environ", {"YOUTUBE_PROXY_URL": "http://172.17.0.1:8888"}):
            # Uses no handler state, so it can be called without a socket.
            _ProxyHandler._fetch_window(None, self.CDN, {}, 0, 9)
        self.assertEqual(
            mock_get.call_args.kwargs["proxies"],
            {"http": "http://172.17.0.1:8888", "https": "http://172.17.0.1:8888"},
        )


LIVE = (
    "https://googlevideo.com/videoplayback?id=abc&live=1&hang=1&noclen=1&pot=T"
)
FILE = "https://googlevideo.com/videoplayback?id=abc&clen=4115661&pot=T"


def _segment(status=200, body=b"", headers=None):
    """A stub upstream response shaped like the bits the relay reads."""
    resp = Mock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.iter_content.return_value = [body] if body else []
    resp.close = Mock()
    return resp


class LiveUrlTests(TestCase):
    """YouTube marks its own live URLs; the relay must not guess."""

    def test_live_urls_are_recognised(self):
        self.assertTrue(is_live_url(LIVE))
        self.assertTrue(is_live_url("https://x/videoplayback?live=1"))
        self.assertTrue(is_live_url("https://x/videoplayback?noclen=1"))

    def test_ordinary_video_urls_are_not_live(self):
        self.assertFalse(is_live_url(FILE))
        self.assertFalse(is_live_url("https://x/videoplayback?live=0&noclen=0"))
        self.assertFalse(is_live_url("https://x/videoplayback"))

    def test_sequence_is_appended_then_replaced(self):
        self.assertEqual(with_sequence("https://x/v?a=1", 7), "https://x/v?a=1&sq=7")
        self.assertEqual(with_sequence("https://x/v", 7), "https://x/v?sq=7")
        # Replaced, not appended twice: a second sq would be ignored and the
        # relay would refetch the same segment for ever.
        self.assertEqual(with_sequence("https://x/v?sq=7&b=2", 8), "https://x/v?sq=8&b=2")
        self.assertEqual(with_sequence("https://x/v?a=1&sq=7", 8), "https://x/v?a=1&sq=8")

    def test_head_sequence_number_survives_a_missing_or_junk_header(self):
        self.assertEqual(head_sequence_number({"X-Head-Seqnum": "42"}), 42)
        self.assertIsNone(head_sequence_number({}))
        self.assertIsNone(head_sequence_number({"X-Head-Seqnum": "soon"}))


class LiveRelayTests(TestCase):
    """The live path walks segments; it must never send a byte range."""

    def setUp(self):
        self.proxy = StreamProxy(host="127.0.0.1", port=0)
        self.proxy.start()
        self.addCleanup(self.proxy.close)

    def _get(self, path, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy._port}{path}", headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers or {})

    def _token(self, url=LIVE):
        return self.proxy.register(url, {"User-Agent": "ua"}).rsplit("/", 1)[-1]

    @patch("bot.services.stream_proxy.requests.get")
    def test_live_is_walked_by_sequence_number_and_concatenated(self, mock_get):
        head = {"X-Head-Seqnum": "100", "Content-Type": "audio/mp4"}
        mock_get.side_effect = [
            _segment(200, b"seg100", head),
            _segment(200, b"seg101", {"X-Head-Seqnum": "101"}),
            _segment(200, b"seg102", {"X-Head-Seqnum": "102"}),
            _segment(404),
            _segment(404),
            _segment(404),
            _segment(404),
        ]

        status, body, resp_headers = self._get(f"/{self._token()}", {"Range": "bytes=0-"})

        self.assertEqual(status, 200)
        self.assertEqual(body, b"seg100seg101seg102")
        self.assertEqual(resp_headers.get("Content-Type"), "audio/mp4")
        # A broadcast has no length and cannot be seeked. Advertising either
        # invites mpv to seek, and a Content-Length is the promise that made
        # the silent-track failure invisible in the first place.
        self.assertIsNone(resp_headers.get("Content-Length"))
        self.assertIsNone(resp_headers.get("Accept-Ranges"))

        requested = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(requested[0], LIVE)
        self.assertEqual(requested[1], with_sequence(LIVE, 101))
        self.assertEqual(requested[2], with_sequence(LIVE, 102))
        for call in mock_get.call_args_list:
            self.assertNotIn("Range", call.kwargs["headers"])
            self.assertEqual(call.kwargs["headers"]["User-Agent"], "ua")

    @patch("bot.services.stream_proxy.time.sleep", Mock())
    @patch("bot.services.stream_proxy.requests.get")
    def test_a_single_missing_segment_is_retried_rather_than_ending_the_stream(self, mock_get):
        mock_get.side_effect = [
            _segment(200, b"a", {"X-Head-Seqnum": "5"}),
            _segment(404),
            _segment(200, b"b", {"X-Head-Seqnum": "6"}),
        ] + [_segment(404)] * 4

        status, body, _ = self._get(f"/{self._token()}")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"ab")
        # The retry asks for the same segment again, not the next one.
        requested = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(requested[1], with_sequence(LIVE, 6))
        self.assertEqual(requested[2], with_sequence(LIVE, 6))

    @patch("bot.services.stream_proxy.time.sleep", Mock())
    @patch("bot.services.stream_proxy.requests.get")
    def test_a_broadcast_that_has_ended_stops_cleanly_and_says_so(self, mock_get):
        mock_get.side_effect = [_segment(200, b"a", {"X-Head-Seqnum": "5"})] + [_segment(404)] * 8

        with self.assertLogs("root", level="INFO") as logs:
            status, body, _ = self._get(f"/{self._token()}")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"a")
        self.assertTrue(any("treating the broadcast as ended" in line for line in logs.output))

    @patch("bot.services.stream_proxy.requests.get")
    def test_falling_behind_the_live_edge_skips_forward(self, mock_get):
        # Segment 6 arrives while YouTube is already at 200: walking one at a
        # time from there would play an hour-old backlog in real time.
        mock_get.side_effect = [
            _segment(200, b"a", {"X-Head-Seqnum": "5"}),
            _segment(200, b"b", {"X-Head-Seqnum": "200"}),
            _segment(200, b"c", {"X-Head-Seqnum": "201"}),
        ] + [_segment(404)] * 5

        with patch("bot.services.stream_proxy.time.sleep", Mock()):
            status, body, _ = self._get(f"/{self._token()}")

        requested = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(requested[1], with_sequence(LIVE, 6))
        self.assertEqual(requested[2], with_sequence(LIVE, 201))
        self.assertEqual(body, b"abc")

    @patch("bot.services.stream_proxy.requests.get")
    def test_a_success_with_no_body_is_logged_rather_than_played_silently(self, mock_get):
        """The exact failure this path exists for: 206, a length, and no bytes."""
        mock_get.side_effect = [
            _segment(206, b"", {"X-Head-Seqnum": "5", "Content-Length": "700000"}),
        ] + [_segment(404)] * 6

        with patch("bot.services.stream_proxy.time.sleep", Mock()):
            with self.assertLogs("root", level="WARNING") as logs:
                status, body, _ = self._get(f"/{self._token()}")

        self.assertEqual(body, b"")
        self.assertTrue(
            any("returned success but no body" in line for line in logs.output),
            logs.output,
        )

    @patch("bot.services.stream_proxy.requests.get")
    def test_an_ordinary_video_still_takes_the_windowed_path(self, mock_get):
        """The live walk must not touch playback that already works."""
        resp = _segment(206, b"filebytes", {"Content-Range": "bytes 0-8/9", "Content-Type": "audio/webm"})
        mock_get.return_value = resp
        mock_get.side_effect = None

        status, body, _ = self._get(f"/{self._token(FILE)}", {"Range": "bytes=0-"})

        self.assertEqual(body, b"filebytes")
        self.assertIn("Range", mock_get.call_args.kwargs["headers"])
        self.assertNotIn("sq=", mock_get.call_args.args[0])

    @patch("bot.services.stream_proxy.time.sleep", Mock())
    @patch("bot.services.stream_proxy.requests.get")
    def test_endless_empty_successes_give_up_instead_of_spinning(self, mock_get):
        """A 200 with no body must not be retried for ever.

        This is the failure the live path was written for, so the walk has to
        be sure it cannot itself become the silent loop.
        """
        mock_get.side_effect = [_segment(200, b"a", {"X-Head-Seqnum": "5"})] + [
            _segment(200, b"", {"X-Head-Seqnum": "5"}) for _ in range(20)
        ]

        with self.assertLogs("root", level="WARNING") as logs:
            status, body, _ = self._get(f"/{self._token()}")

        self.assertEqual(body, b"a")
        self.assertTrue(any("giving up rather than looping" in line for line in logs.output))
        # Bounded by the retry budget, not by the 20 responses queued above.
        self.assertLessEqual(mock_get.call_count, 6)
