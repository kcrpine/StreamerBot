from unittest import TestCase
from unittest.mock import patch

from bot.services.youtube_bridge import YouTubeBridge


class YouTubeBridgeContractTests(TestCase):
    def setUp(self):
        self.bridge = YouTubeBridge(client="YTMUSIC")

    def test_music_search_sets_explicit_mode(self):
        with patch.object(self.bridge, "_post", return_value={"entries": []}) as post:
            self.bridge.search("ela vem", 1, mode="music")

        post.assert_called_once_with(
            "/search", query="ela vem", limit=1, mode="music"
        )

    def test_recommendations_send_video_id_and_limit(self):
        with patch.object(self.bridge, "_post", return_value={"entries": []}) as post:
            self.bridge.recommendations("48Lrud3Bxpc", 15)

        post.assert_called_once_with(
            "/recommendations", video_id="48Lrud3Bxpc", limit=15
        )

    def test_resolve_reuses_response_until_bridge_deadline(self):
        response = {
            "url": "https://example.test/audio",
            "cache_expires_at_ms": 50_000,
        }
        with patch("bot.services.youtube_bridge.time.time", return_value=10), patch.object(
            self.bridge, "_post", return_value=response
        ) as post:
            first = self.bridge.resolve(video_id="48Lrud3Bxpc")
            second = self.bridge.resolve(video_id="48Lrud3Bxpc")

        self.assertIs(first, second)
        post.assert_called_once()

    def test_resolve_refreshes_after_bridge_deadline(self):
        with patch.object(
            self.bridge,
            "_post",
            side_effect=[
                {"url": "first", "cache_expires_at_ms": 20_000},
                {"url": "second", "cache_expires_at_ms": 40_000},
            ],
        ) as post:
            with patch("bot.services.youtube_bridge.time.time", return_value=10):
                self.bridge.resolve(video_id="48Lrud3Bxpc")
            with patch("bot.services.youtube_bridge.time.time", return_value=30):
                refreshed = self.bridge.resolve(video_id="48Lrud3Bxpc")

        self.assertEqual(refreshed["url"], "second")
        self.assertEqual(post.call_count, 2)

    def test_invalidate_clears_local_cache_and_notifies_bridge(self):
        self.bridge._resolve_cache["48Lrud3Bxpc"] = (50_000, {"url": "old"})
        with patch.object(self.bridge, "_post", return_value={"invalidated": True}) as post:
            self.bridge.invalidate(video_id="48Lrud3Bxpc")

        self.assertNotIn("48Lrud3Bxpc", self.bridge._resolve_cache)
        post.assert_called_once_with(
            "/invalidate", url=None, video_id="48Lrud3Bxpc"
        )
