"""Player's dispatch across playback engines.

Player is built with object.__new__ so that no real mpv instance is created,
matching how test_player_stream_retry.py constructs one.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from bot.player import Player
from bot.player.enums import Mode, State, TrackType
from bot.player.track import Track


def make_engine(name):
    engine = Mock()
    engine.name = name
    engine.get_volume.return_value = 0
    return engine


def make_player(mode=Mode.TrackList):
    player = object.__new__(Player)
    mpv_engine = make_engine("mpv")
    librespot = make_engine("librespot")
    player._mpv_engine = mpv_engine
    player.engines = {"mpv": mpv_engine, "librespot": librespot}
    player._active_engine = mpv_engine
    player.state = State.Playing
    player.mode = mode
    player.track = Track(service="yt", url="https://x.test/a", type=TrackType.Default)
    player.track_list = []
    player.track_index = -1
    player.queue = SimpleNamespace(is_empty=True)
    player.stop = Mock()
    player.next = Mock()
    player.play_from_queue = Mock()
    player.play_by_index = Mock()
    # _play collaborators that are irrelevant to dispatch.
    player._start_playback_trace = Mock(return_value={})
    player._log_playback_timing = Mock()
    player._schedule_prefetch = Mock()
    player.cache = SimpleNamespace(recents=[])
    player.cache_manager = SimpleNamespace(save=Mock())
    return player, mpv_engine, librespot


class EngineSelectionTests(TestCase):
    def test_engine_chosen_by_track_engine_field(self):
        player, mpv_engine, librespot = make_player()

        self.assertIs(player._engine_for(Track(engine="mpv")), mpv_engine)
        self.assertIs(player._engine_for(Track(engine="librespot")), librespot)

    def test_unknown_engine_falls_back_to_mpv(self):
        player, mpv_engine, _ = make_player()

        self.assertIs(player._engine_for(Track(engine="nonexistent")), mpv_engine)

    def test_track_without_engine_attribute_falls_back_to_mpv(self):
        """Tracks unpickled from a cache written before engines existed."""
        player, mpv_engine, _ = make_player()

        self.assertIs(player._engine_for(SimpleNamespace()), mpv_engine)


class EngineActivationTests(TestCase):
    def test_switching_stops_the_outgoing_engine_exactly_once(self):
        player, mpv_engine, librespot = make_player()

        player._activate_engine(librespot)

        mpv_engine.stop.assert_called_once_with()
        self.assertIs(player._active_engine, librespot)

    def test_activating_the_current_engine_does_not_stop_it(self):
        player, mpv_engine, _ = make_player()

        player._activate_engine(mpv_engine)

        mpv_engine.stop.assert_not_called()
        self.assertIs(player._active_engine, mpv_engine)

    def test_a_failing_stop_does_not_block_the_handover(self):
        player, mpv_engine, librespot = make_player()
        mpv_engine.stop.side_effect = RuntimeError("engine wedged")

        player._activate_engine(librespot)

        self.assertIs(player._active_engine, librespot)


class EngineEndTests(TestCase):
    def test_end_from_the_active_engine_advances(self):
        player, mpv_engine, _ = make_player()
        player._advance_after_end = Mock()

        player.on_engine_end(mpv_engine, "eof")

        player._advance_after_end.assert_called_once_with()

    def test_end_from_a_swapped_out_engine_is_ignored(self):
        """A stop during handover can report back after the new engine started."""
        player, mpv_engine, librespot = make_player()
        player._active_engine = librespot
        player._advance_after_end = Mock()

        player.on_engine_end(mpv_engine, "eof")

        player._advance_after_end.assert_not_called()

    def test_end_while_not_playing_is_ignored(self):
        player, mpv_engine, _ = make_player()
        player.state = State.Stopped
        player._advance_after_end = Mock()

        player.on_engine_end(mpv_engine, "eof")

        player._advance_after_end.assert_not_called()


class AdvanceAfterEndTests(TestCase):
    def test_single_track_mode_stops_when_the_queue_is_empty(self):
        player, _, _ = make_player(mode=Mode.SingleTrack)

        player._advance_after_end()

        player.stop.assert_called_once_with()

    def test_queue_takes_priority_over_single_track_mode(self):
        player, _, _ = make_player(mode=Mode.SingleTrack)
        player.queue.is_empty = False

        player._advance_after_end()

        player.play_from_queue.assert_called_once_with()
        player.stop.assert_not_called()

    def test_repeat_track_replays_the_current_index_ignoring_the_queue(self):
        player, _, _ = make_player(mode=Mode.RepeatTrack)
        player.queue.is_empty = False
        player.track_index = 3

        player._advance_after_end()

        player.play_by_index.assert_called_once_with(3)
        player.play_from_queue.assert_not_called()

    def test_track_list_mode_advances_to_the_next_track(self):
        player, _, _ = make_player(mode=Mode.TrackList)

        player._advance_after_end()

        player.next.assert_called_once_with()

    def test_an_external_track_still_honours_the_queue(self):
        """External tracks must route through the same mode logic as mpv ones."""
        player, _, _ = make_player(mode=Mode.TrackList)
        player.track = Track(
            service="sp",
            url="spotify:track:abc",
            type=TrackType.External,
            engine="librespot",
        )
        player.queue.is_empty = False

        player._advance_after_end()

        player.play_from_queue.assert_called_once_with()


class PlayDispatchTests(TestCase):
    def test_a_url_string_always_goes_to_mpv(self):
        """The stream-refresh retry path hands over an already resolved URL."""
        player, mpv_engine, librespot = make_player()

        player._play("https://fresh.test/audio", save_to_recents=False)

        mpv_engine.play_url.assert_called_once_with(
            "https://fresh.test/audio", player.track
        )
        librespot.play.assert_not_called()

    def test_an_external_track_goes_to_its_engine_and_never_to_mpv(self):
        player, mpv_engine, librespot = make_player()
        track = Track(
            service="sp",
            url="spotify:track:abc",
            type=TrackType.External,
            engine="librespot",
        )

        player._play(track, save_to_recents=False)

        librespot.play.assert_called_once_with(track)
        mpv_engine.play_url.assert_not_called()
        mpv_engine.play.assert_not_called()

    def test_an_mpv_track_is_resolved_before_recents_are_written(self):
        """A track that cannot resolve must not be recorded as played."""
        player, mpv_engine, _ = make_player()
        player.track_list = [Track(service="yt", engine="mpv")]
        player.track_index = 0
        track = Mock()
        track.engine = "mpv"
        type(track).url = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("unresolvable"))
        )

        with self.assertRaises(RuntimeError):
            player._play(track)

        self.assertEqual(player.cache.recents, [])
        mpv_engine.play_url.assert_not_called()
