"""Track behaviour for engines other than mpv.

External tracks carry a stable identifier (spotify:track:..., netflix://...)
rather than a stream URL. Nothing resolves them and nothing refreshes them, so
the lazy-resolution machinery must stay entirely out of their way.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot.player.enums import TrackType
from bot.player.track import Track


class ExternalTrackTests(TestCase):
    def test_external_track_never_resolves(self):
        track = Track(
            service="sp",
            url="spotify:track:6rqhFgbbKwnb9MLmUQDhG6",
            name="Some Song",
            type=TrackType.External,
            engine="librespot",
        )
        service = SimpleNamespace(get=Mock())

        with patch("builtins.get_service_by_name", return_value=service, create=True):
            self.assertEqual(track.url, "spotify:track:6rqhFgbbKwnb9MLmUQDhG6")
            self.assertEqual(track.name, "Some Song")

        service.get.assert_not_called()

    def test_engine_defaults_to_mpv(self):
        self.assertEqual(Track(service="yt").engine, "mpv")

    def test_engine_is_restored_for_tracks_pickled_before_it_existed(self):
        track = Track(service="yt", url="https://x.test/a", name="Old")
        state = track.__getstate__()
        del state["engine"]

        restored = object.__new__(Track)
        restored.__setstate__(state)

        self.assertEqual(restored.engine, "mpv")


class RefreshStreamGuardTests(TestCase):
    def test_refresh_is_refused_for_a_non_mpv_engine(self):
        track = Track(
            service="sp",
            url="spotify:track:abc",
            type=TrackType.External,
            engine="librespot",
        )
        service = SimpleNamespace(engine="librespot", _bridge=Mock())

        with patch("builtins.get_service_by_name", return_value=service, create=True):
            with self.assertRaises(RuntimeError):
                track.refresh_stream()

        service._bridge.invalidate.assert_not_called()

    def test_refresh_is_refused_for_a_service_with_no_bridge(self):
        track = Track(service="nf", url="netflix://watch/80100172", engine="browser")
        service = SimpleNamespace(engine="browser")

        with patch("builtins.get_service_by_name", return_value=service, create=True):
            with self.assertRaises(RuntimeError):
                track.refresh_stream()

    def test_refresh_still_works_for_a_bridge_backed_mpv_service(self):
        """The guard must not have narrowed the existing YouTube behaviour."""
        track = Track(
            service="ytm",
            url="https://www.youtube.com/watch?v=48Lrud3Bxpc",
            name="Ela Vem",
            type=TrackType.Dynamic,
            extra_info={"videoId": "48Lrud3Bxpc"},
        )
        bridge = Mock()
        # No "engine" attribute, matching how the real YtService is written and
        # how the existing test in test_track_refresh.py builds its stand-in.
        service = SimpleNamespace(
            _bridge=bridge,
            get=Mock(
                return_value=[
                    Track(
                        service="ytm",
                        url="https://fresh.test/audio",
                        name="Ela Vem",
                        type=TrackType.Default,
                    )
                ]
            ),
        )

        with patch("builtins.get_service_by_name", return_value=service, create=True):
            refreshed = track.refresh_stream()

        self.assertEqual(refreshed, "https://fresh.test/audio")
        bridge.invalidate.assert_called_once_with(video_id="48Lrud3Bxpc", url="")
