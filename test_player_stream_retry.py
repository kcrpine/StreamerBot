from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

import mpv

from bot.player import Player
from bot.player.enums import State


class PlayerStreamRetryTests(TestCase):
    def test_end_file_error_refreshes_youtube_stream_once(self):
        player = object.__new__(Player)
        player.track = SimpleNamespace(
            service="ytm",
            name="Ela Vem",
            _stream_refresh_attempted=False,
            refresh_stream=Mock(return_value="https://fresh.test/audio"),
        )
        player.state = State.Playing
        player._player = SimpleNamespace(idle_active=True)
        player._play = Mock()
        event = Mock()
        event.as_dict.return_value = {
            "event": {"reason": mpv.MpvEventEndFile.ERROR}
        }

        player.on_end_file(event)

        player.track.refresh_stream.assert_called_once_with()
        player._play.assert_called_once_with(
            "https://fresh.test/audio", save_to_recents=False
        )
        self.assertTrue(player.track._stream_refresh_attempted)

    def test_end_file_dict_event_supported(self):
        player = object.__new__(Player)
        player.track = SimpleNamespace(
            service="ytm",
            name="Ela Vem",
            _stream_refresh_attempted=False,
            refresh_stream=Mock(return_value="https://fresh.test/audio"),
        )
        player.state = State.Playing
        player._player = SimpleNamespace(idle_active=True)
        player._play = Mock()
        raw_dict_event = {
            "event_id": 7,
            "event": {"reason": mpv.MpvEventEndFile.ERROR}
        }

        player.on_end_file(raw_dict_event)

        player.track.refresh_stream.assert_called_once_with()
        player._play.assert_called_once_with(
            "https://fresh.test/audio", save_to_recents=False
        )
