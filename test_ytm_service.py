from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from bot.services.ytm import YtmService


class YtmServiceBridgeTests(TestCase):
    def setUp(self):
        self.service = object.__new__(YtmService)
        self.service.name = "ytm"
        self.service.config = SimpleNamespace(search_results=1)
        self.service.bot = SimpleNamespace(
            translator=SimpleNamespace(translate=lambda value: value)
        )
        self.service._bridge = Mock()

    def test_search_uses_music_mode_and_preserves_artist_metadata(self):
        self.service._bridge.search.return_value = {
            "entries": [{
                "videoId": "J0pZqX0ITxg",
                "title": "Ela Vem",
                "artists": [{"name": "Mc G15"}, {"name": "Mc Livinho"}],
                "webpage_url": "https://www.youtube.com/watch?v=J0pZqX0ITxg",
            }]
        }

        tracks = self.service.search("ela vem")

        self.service._bridge.search.assert_called_once_with("ela vem", 1, mode="music")
        self.assertEqual(tracks[0].name, "Ela Vem - Mc G15, Mc Livinho")
        self.assertEqual(tracks[0].extra_info["videoId"], "J0pZqX0ITxg")

    def test_recommendation_tracks_use_shared_bridge(self):
        self.service._bridge.recommendations.return_value = {
            "entries": [{
                "videoId": "48Lrud3Bxpc",
                "title": "Ela Vem (SET DJ NENE)",
                "uploader": "Mc Kevin",
                "webpage_url": "https://www.youtube.com/watch?v=48Lrud3Bxpc",
            }]
        }

        tracks = self.service._get_recommendation_tracks("J0pZqX0ITxg", 15)

        self.service._bridge.recommendations.assert_called_once_with("J0pZqX0ITxg", 15)
        self.assertEqual(tracks[0].name, "Ela Vem (SET DJ NENE) - Mc Kevin")
