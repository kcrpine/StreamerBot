from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import mpv

from bot.player import Player, YOUTUBE_STREAM_REFRESH_MAX_ATTEMPTS
from bot.player.enums import State, TrackType
from bot.player.track import Track


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


class TruncatedStreamEndFileTests(TestCase):
    """A stream cut off mid-track arrives as EOF, not ERROR.

    stream_proxy has already sent mpv a Content-Length covering the whole file
    by the time an upstream window is refused, so when it stops writing, ffmpeg
    reconnects a few times and then reports end-file with reason EOF -- the
    same event a track that genuinely finished produces. Measured against a
    real libmpv: a 60s file truncated after 0.4s gives reason=0. Taking that at
    face value is what let a playlist advance through 282 entries in 451s
    without logging one error.
    """

    def _player(self, played, duration, service="yt"):
        player = object.__new__(Player)
        player.track = SimpleNamespace(
            service=service,
            name="Evensong at Westminster Abbey",
            refresh_stream=Mock(return_value="https://fresh.test/audio"),
        )
        player.state = State.Playing
        player._player = SimpleNamespace(idle_active=True)
        player._play = Mock()
        player._advance_after_end = Mock()
        player._last_time_pos = played
        player._last_duration = duration
        return player

    @staticmethod
    def _eof():
        event = Mock()
        event.as_dict.return_value = {"event": {"reason": mpv.MpvEventEndFile.EOF}}
        return event

    def test_eof_within_seconds_of_the_start_is_refreshed(self):
        # Died immediately: plausibly transient, and re-resolving costs the
        # listener nothing because nothing had played yet.
        player = self._player(played=0.4, duration=60.0)

        with patch("bot.player.time.sleep"):
            player.on_end_file(self._eof())

        player.track.refresh_stream.assert_called_once_with()
        player._play.assert_called_once_with(
            "https://fresh.test/audio", save_to_recents=False
        )
        player._advance_after_end.assert_not_called()

    def test_eof_a_minute_into_a_long_track_is_abandoned_not_refreshed(self):
        # The shape the log that led here is full of: a half-hour track whose
        # stream is cut at ~60s. A refresh restarts from zero against a URL
        # with the same limit, so it would replay that same minute three more
        # times for nothing. Abandon it -- but say so, which is the part that
        # was missing.
        player = self._player(played=52.0, duration=1771.0)

        with self.assertLogs("root", level="WARNING") as logged:
            with patch("bot.player.time.sleep"):
                player.on_end_file(self._eof())

        player.track.refresh_stream.assert_not_called()
        player._play.assert_not_called()
        player._advance_after_end.assert_called_once_with()
        message = "\n".join(logged.output)
        self.assertIn("end_file_short_of_duration", message)
        self.assertIn("played=52.0s", message)
        self.assertIn("track_abandoned", message)

    def test_eof_at_the_end_of_the_track_advances_as_before(self):
        player = self._player(played=1770.0, duration=1771.0)

        with patch("bot.player.time.sleep"):
            player.on_end_file(self._eof())

        player.track.refresh_stream.assert_not_called()
        player._advance_after_end.assert_called_once_with()

    def test_eof_within_the_grace_window_advances(self):
        # Short tracks end a few seconds early often enough -- a trailing
        # silence trimmed, a container whose duration is approximate -- that
        # judging them by fraction alone would retry finished tracks.
        player = self._player(played=18.0, duration=30.0)

        with patch("bot.player.time.sleep"):
            player.on_end_file(self._eof())

        player.track.refresh_stream.assert_not_called()
        player._advance_after_end.assert_called_once_with()

    def test_eof_with_no_duration_advances(self):
        # A live stream has no duration, so there is nothing to be short of.
        player = self._player(played=12.0, duration=None)

        with patch("bot.player.time.sleep"):
            player.on_end_file(self._eof())

        player.track.refresh_stream.assert_not_called()
        player._advance_after_end.assert_called_once_with()

    def test_short_eof_on_a_non_youtube_service_still_advances(self):
        # Nothing to re-resolve, so it must not wedge: advance, having logged.
        player = self._player(played=1.0, duration=300.0, service="sp")

        with patch("bot.player.time.sleep"):
            player.on_end_file(self._eof())

        player.track.refresh_stream.assert_not_called()
        player._advance_after_end.assert_called_once_with()

    def test_short_eof_gives_up_and_advances_after_max_attempts(self):
        player = self._player(played=0.4, duration=1771.0)

        with patch("bot.player.time.sleep"):
            for _ in range(4):
                player.on_end_file(self._eof())

        self.assertEqual(player.track.refresh_stream.call_count, 3)
        player._advance_after_end.assert_called_once_with()


class RefreshBudgetResetTests(TestCase):
    """The refresh counter lives on the Track, and track_list holds the same
    Track objects for the life of the session. Without a reset, a track that
    exhausted its attempts once is skipped instantly every later time it comes
    round -- which for a looping playlist means permanently, and silently.
    """

    def test_playing_a_track_gives_it_a_fresh_refresh_budget(self):
        from test_engine_dispatch import make_player

        player, _, _ = make_player()
        track = Track(service="yt", url="https://x.test/a", type=TrackType.Default)
        track._stream_refresh_attempts = YOUTUBE_STREAM_REFRESH_MAX_ATTEMPTS

        player._play(track, save_to_recents=False)

        self.assertEqual(track._stream_refresh_attempts, 0)

    def test_the_refresh_retry_itself_keeps_counting(self):
        # _play(str) is the stream-refresh retry. Resetting there would let a
        # stream that loads and then fails retry for ever.
        from test_engine_dispatch import make_player

        player, _, _ = make_player()
        player.track._stream_refresh_attempts = 2

        player._play("https://fresh.test/audio", save_to_recents=False)

        self.assertEqual(player.track._stream_refresh_attempts, 2)

    def test_starting_a_track_clears_the_previous_track_progress(self):
        from test_engine_dispatch import make_player

        player, _, _ = make_player()
        player._last_time_pos = 43.0
        player._last_duration = 1771.0

        player._play(
            Track(service="yt", url="https://x.test/b", type=TrackType.Default),
            save_to_recents=False,
        )

        self.assertIsNone(player._last_time_pos)
        self.assertIsNone(player._last_duration)
