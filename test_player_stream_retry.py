from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import mpv

from bot.player import Player
from bot.player.enums import State


class PlayerStreamRetryTests(TestCase):
    def test_end_file_error_refreshes_youtube_stream_once(self):
        player = object.__new__(Player)
        player.track = SimpleNamespace(
            service="ytm",
            name="Ela Vem",
            refresh_stream=Mock(return_value="https://fresh.test/audio"),
        )
        player.state = State.Playing
        player._player = SimpleNamespace(idle_active=True)
        player._play = Mock()
        event = Mock()
        event.as_dict.return_value = {
            "event": {"reason": mpv.MpvEventEndFile.ERROR}
        }

        with patch("bot.player.time.sleep") as mock_sleep:
            player.on_end_file(event)

        player.track.refresh_stream.assert_called_once_with()
        player._play.assert_called_once_with(
            "https://fresh.test/audio", save_to_recents=False
        )
        self.assertEqual(player.track._stream_refresh_attempts, 1)
        mock_sleep.assert_called_once()

    def test_end_file_dict_event_supported(self):
        player = object.__new__(Player)
        player.track = SimpleNamespace(
            service="ytm",
            name="Ela Vem",
            refresh_stream=Mock(return_value="https://fresh.test/audio"),
        )
        player.state = State.Playing
        player._player = SimpleNamespace(idle_active=True)
        player._play = Mock()
        raw_dict_event = {
            "event_id": 7,
            "event": {"reason": mpv.MpvEventEndFile.ERROR}
        }

        with patch("bot.player.time.sleep"):
            player.on_end_file(raw_dict_event)

        player.track.refresh_stream.assert_called_once_with()
        player._play.assert_called_once_with(
            "https://fresh.test/audio", save_to_recents=False
        )

    def test_end_file_error_retries_across_repeated_mpv_failures(self):
        # A refreshed URL can hit the same transient CDN 403 the original
        # did: mpv fires end-of-file with ERROR again for it, which is a
        # second, separate on_end_file call for the same track. The retry
        # counter must survive across those calls, not just within one.
        player = object.__new__(Player)
        player.track = SimpleNamespace(
            service="yt",
            name="Drink More Beer",
            refresh_stream=Mock(return_value="https://fresh.test/audio"),
        )
        player.state = State.Playing
        player._player = SimpleNamespace(idle_active=True)
        player._play = Mock()
        event = Mock()
        event.as_dict.return_value = {
            "event": {"reason": mpv.MpvEventEndFile.ERROR}
        }

        with patch("bot.player.time.sleep"):
            player.on_end_file(event)  # attempt 1
            player.on_end_file(event)  # attempt 2: mpv failed on attempt 1's url too

        self.assertEqual(player.track.refresh_stream.call_count, 2)
        self.assertEqual(player._play.call_count, 2)
        self.assertEqual(player.track._stream_refresh_attempts, 2)

    def test_end_file_error_gives_up_and_advances_after_max_attempts(self):
        player = object.__new__(Player)
        player.track = SimpleNamespace(
            service="yt",
            name="Drink More Beer",
            refresh_stream=Mock(return_value="https://fresh.test/audio"),
        )
        player.state = State.Playing
        player._player = SimpleNamespace(idle_active=True)
        player._play = Mock()
        player._advance_after_end = Mock()
        event = Mock()
        event.as_dict.return_value = {
            "event": {"reason": mpv.MpvEventEndFile.ERROR}
        }

        with patch("bot.player.time.sleep"):
            for _ in range(4):  # one more than YOUTUBE_STREAM_REFRESH_MAX_ATTEMPTS
                player.on_end_file(event)

        self.assertEqual(player.track.refresh_stream.call_count, 3)
        self.assertEqual(player._play.call_count, 3)
        player._advance_after_end.assert_called_once_with()
