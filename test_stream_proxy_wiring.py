from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from bot.services.yt import YtService
from bot.services.ytm import YtmService


class YtStreamProxyWiringTests(TestCase):
    def setUp(self):
        self.service = object.__new__(YtService)
        self.service.name = "yt"
        self.service._fetch_autoplay_async = Mock()
        self.service.bot = SimpleNamespace(
            translator=SimpleNamespace(translate=lambda value: value),
            player=SimpleNamespace(is_playlist=False, track_list=[], track_index=-1),
            stream_proxy=Mock(),
        )
        self.service.bot.stream_proxy.register.return_value = "http://127.0.0.1:4420/tok123"
        self.service._bridge = Mock()

    def test_get_inner_routes_resolved_url_through_stream_proxy(self):
        self.service._bridge.resolve.return_value = {
            "id": "abc123",
            "url": "https://rr1---sn-xyz.googlevideo.com/videoplayback?sig=real",
            "title": "A Song",
            "http_headers": {"User-Agent": "test-ua"},
        }

        tracks = self.service._get_inner(
            url="", extra_info={"videoId": "abc123"}, process=True, start_time=0.0
        )

        self.service.bot.stream_proxy.register.assert_called_once_with(
            "https://rr1---sn-xyz.googlevideo.com/videoplayback?sig=real",
            {"User-Agent": "test-ua"},
        )
        self.assertEqual(tracks[0].url, "http://127.0.0.1:4420/tok123")


class YtmStreamProxyWiringTests(TestCase):
    def setUp(self):
        self.service = object.__new__(YtmService)
        self.service.name = "ytm"
        self.service._fetch_autoplay_async = Mock()
        self.service.bot = SimpleNamespace(
            translator=SimpleNamespace(translate=lambda value: value),
            player=SimpleNamespace(is_playlist=False, track_list=[], track_index=-1),
            stream_proxy=Mock(),
        )
        self.service.bot.stream_proxy.register.return_value = "http://127.0.0.1:4420/tok456"
        self.service._bridge = Mock()

    def test_get_routes_resolved_url_through_stream_proxy(self):
        self.service._bridge.resolve.return_value = {
            "id": "abc123",
            "url": "https://rr1---sn-xyz.googlevideo.com/videoplayback?sig=real",
            "title": "A Song",
            "http_headers": {"User-Agent": "test-ua"},
        }

        tracks = self.service.get(
            url="", extra_info={"videoId": "abc123"}, process=True
        )

        self.service.bot.stream_proxy.register.assert_called_once_with(
            "https://rr1---sn-xyz.googlevideo.com/videoplayback?sig=real",
            {"User-Agent": "test-ua"},
        )
        self.assertEqual(tracks[0].url, "http://127.0.0.1:4420/tok456")
