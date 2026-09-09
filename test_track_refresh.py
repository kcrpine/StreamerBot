import copy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot.player.enums import TrackType
from bot.player.track import Track


class TrackRefreshTests(TestCase):
    def test_refresh_invalidates_and_resolves_original_video(self):
        track = Track(
            service="ytm",
            url="https://www.youtube.com/watch?v=48Lrud3Bxpc",
            name="Ela Vem",
            type=TrackType.Dynamic,
            extra_info={"videoId": "48Lrud3Bxpc"},
        )
        track._original_track = copy.deepcopy(track)
        track._url = "https://expired.test/audio"
        track.type = TrackType.Default
        track._is_fetched = True

        bridge = Mock()
        service = SimpleNamespace(
            _bridge=bridge,
            get=Mock(return_value=[Track(
                service="ytm",
                url="https://fresh.test/audio",
                name="Ela Vem",
                type=TrackType.Default,
                extra_info={"videoId": "48Lrud3Bxpc"},
            )]),
        )

        with patch("builtins.get_service_by_name", return_value=service, create=True):
            refreshed = track.refresh_stream()

        self.assertEqual(refreshed, "https://fresh.test/audio")
        bridge.invalidate.assert_called_once_with(
            video_id="48Lrud3Bxpc", url=""
        )
        service.get.assert_called_once()
