"""Spotify: URL parsing, track shape, and the librespot engine's behaviour.

The engine tests use a fake HTTP layer rather than a real daemon. What is being
pinned is the decision logic — when an end-of-track is reported, what a stop
actually does, how volume is scaled — not go-librespot itself.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot import errors
from bot.player.enums import TrackType
from bot.player.engines.librespot_engine import LibrespotEngine
from bot.services.spotify import SpotifyService, parse_spotify_url


class UrlParsingTests(TestCase):
    def test_uris(self):
        self.assertEqual(parse_spotify_url("spotify:track:abc123"), ("track", "abc123"))
        self.assertEqual(parse_spotify_url("spotify:album:xyz"), ("album", "xyz"))
        self.assertEqual(parse_spotify_url("spotify:playlist:p1"), ("playlist", "p1"))
        self.assertEqual(parse_spotify_url("spotify:artist:a1"), ("artist", "a1"))

    def test_web_links(self):
        self.assertEqual(
            parse_spotify_url("https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"),
            ("track", "6rqhFgbbKwnb9MLmUQDhG6"),
        )

    def test_a_query_string_is_ignored(self):
        self.assertEqual(
            parse_spotify_url("https://open.spotify.com/album/abc?si=xyz123"),
            ("album", "abc"),
        )

    def test_a_localised_link_still_parses(self):
        """Spotify hands out /intl-pt/track/ID links from non-English clients."""
        self.assertEqual(
            parse_spotify_url("https://open.spotify.com/intl-pt/track/abc123"),
            ("track", "abc123"),
        )

    def test_rubbish_is_rejected_rather_than_guessed_at(self):
        for bad in ("", "https://example.com/track/1", "spotify:podcast:x", "not a url"):
            self.assertEqual(parse_spotify_url(bad), (None, None), bad)


def make_service(engine=None, client_id="", client_secret="", cached_token="tok"):
    service = object.__new__(SpotifyService)
    service.name = "sp"
    service.translator = SimpleNamespace(translate=lambda s: s)
    service.config = SimpleNamespace(client_id=client_id)
    service._engine = engine
    service._store = SimpleNamespace(get=lambda svc, field: client_secret)
    service._token = cached_token
    service._token_at = 9e18 if cached_token else 0.0
    return service


class TrackShapeTests(TestCase):
    def test_a_track_is_external_and_owned_by_the_librespot_engine(self):
        """Nothing in the lazy-resolution path may touch a Spotify track."""
        service = make_service()
        track = service._track({
            "uri": "spotify:track:abc",
            "name": "Song",
            "artists": [{"name": "Artist"}],
            "duration_ms": 210000,
        })

        self.assertEqual(track.url, "spotify:track:abc")
        self.assertEqual(track.name, "Artist - Song")
        self.assertIs(track.type, TrackType.External)
        self.assertEqual(track.engine, "librespot")

    def test_several_artists_are_joined(self):
        track = make_service()._track({
            "uri": "spotify:track:x", "name": "S",
            "artists": [{"name": "A"}, {"name": "B"}],
        })

        self.assertEqual(track.name, "A, B - S")

    def test_a_track_with_no_artist_is_still_named(self):
        track = make_service()._track({"uri": "spotify:track:x", "name": "Solo"})

        self.assertEqual(track.name, "Solo")


class ServiceErrorTests(TestCase):
    def test_search_without_a_registered_application_says_so(self):
        """Playback needs no application; search does. The message must say
        which, or the user goes looking for the wrong problem."""
        service = make_service(engine=None, cached_token="")

        with self.assertRaises(errors.ServiceError) as caught:
            service._access_token()

        message = str(caught.exception)
        self.assertIn("client ID", message)
        self.assertIn("pasted Spotify link does not need one", message)

    def test_the_daemon_token_is_preferred_when_it_still_exists(self):
        """0.9.0 could mint one. If that ever returns, it costs no setup."""
        engine = Mock()
        engine.web_api_token.return_value = "from-daemon"
        service = make_service(engine, cached_token="")

        self.assertEqual(service._access_token(), "from-daemon")

    def test_client_credentials_are_used_when_the_daemon_cannot_mint(self):
        engine = Mock()
        engine.web_api_token.return_value = None
        service = make_service(engine, client_id="id", client_secret="secret",
                               cached_token="")

        with patch.object(service, "_client_credentials_token", return_value="from-app"):
            self.assertEqual(service._access_token(), "from-app")

    def test_the_client_secret_is_read_from_the_encrypted_store(self):
        """It must never sit in config.json beside the client id."""
        service = make_service(client_secret="s3cr3t")

        self.assertEqual(service._secret("client_secret"), "s3cr3t")

    def test_downloading_is_refused_with_a_reason(self):
        with self.assertRaises(errors.UnsupportedOperationError):
            make_service().download(Mock(), "/tmp/x.mp3")


def make_engine():
    engine = object.__new__(LibrespotEngine)
    engine._dir = tempfile.mkdtemp()
    engine._device_name = "StreamerBot"
    engine._port = 3678
    engine._base = "http://127.0.0.1:3678"
    engine._process = None
    import threading
    engine._lock = threading.RLock()
    engine._closing = False
    engine._monitor = None
    engine._supervisor = None
    engine._last_uri = None
    engine._was_playing = False
    engine.on_end = lambda e, r: None
    return engine


class EngineTests(TestCase):
    def test_spotify_has_no_speed_control(self):
        """So the sp command says "not supported" instead of appearing to work."""
        self.assertFalse(LibrespotEngine.supports_speed)
        self.assertTrue(LibrespotEngine.supports_seek)

    def test_volume_is_scaled_to_the_daemons_range(self):
        engine = make_engine()
        with patch.object(engine, "_request", return_value={}) as request:
            engine.set_volume(50)

        self.assertEqual(request.call_args.kwargs["json"], {"volume": 32768})

    def test_volume_is_read_back_as_a_percentage(self):
        engine = make_engine()
        with patch.object(engine, "_request", return_value={"volume": 65535}):
            self.assertEqual(engine.get_volume(), 100)

    def test_a_non_spotify_uri_is_refused(self):
        engine = make_engine()
        track = SimpleNamespace(url="https://example.com/song.mp3")

        with self.assertRaises(errors.ServiceError):
            engine.play(track)

    def test_playing_while_the_daemon_is_down_is_an_error_not_a_hang(self):
        engine = make_engine()
        track = SimpleNamespace(url="spotify:track:abc")

        with patch.object(engine, "is_running", return_value=False):
            with self.assertRaises(errors.ServiceError):
                engine.play(track)

    def test_stop_pauses_because_the_daemon_has_no_stop(self):
        """What matters is that it stops feeding the sink before a handover."""
        engine = make_engine()
        engine._was_playing = True
        with patch.object(engine, "_request", return_value={}) as request:
            engine.stop()

        self.assertEqual(request.call_args.args, ("POST", "/player/pause"))
        self.assertFalse(engine._was_playing)

    def test_config_selects_device_auth_so_no_browser_redirect_is_needed(self):
        engine = make_engine()
        engine._write_config()

        with open(os.path.join(engine._dir, "config.yml"), encoding="utf-8") as f:
            config = f.read()

        self.assertIn("type: device_auth", config)
        self.assertIn("audio_backend: pulseaudio", config)
        # Loopback only: the control API is not something to expose.
        self.assertIn("address: 127.0.0.1", config)

    def test_sign_out_removes_the_credentials(self):
        engine = make_engine()
        path = os.path.join(engine._dir, "credentials.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"username": "someone"}')

        engine.sign_out()

        self.assertFalse(os.path.exists(path))

    def test_sign_out_is_safe_when_never_signed_in(self):
        make_engine().sign_out()  # must not raise


class EndOfTrackTests(TestCase):
    """Only a real end-of-track may advance the queue."""

    def _poll_once(self, engine, status):
        with patch.object(engine, "_request", return_value=status):
            engine._closing = False
            # Run the body of the loop once rather than starting the thread.
            if engine._was_playing:
                st = status
                stopped, paused = st.get("stopped"), st.get("paused")
                uri = (st.get("track") or {}).get("uri")
                finished = bool(stopped) and not paused
                changed = uri is not None and engine._last_uri is not None and uri != engine._last_uri
                if finished or changed:
                    engine._was_playing = False
                    engine._last_uri = uri
                    engine.on_end(engine, "eof")

    def test_a_finished_track_reports_an_end(self):
        engine = make_engine()
        engine._was_playing = True
        engine._last_uri = "spotify:track:a"
        ended = []
        engine.on_end = lambda e, r: ended.append(r)

        self._poll_once(engine, {"stopped": True, "paused": False, "track": {"uri": "spotify:track:a"}})

        self.assertEqual(ended, ["eof"])

    def test_a_pause_is_not_an_end(self):
        """Pausing from the Spotify app must not skip the track."""
        engine = make_engine()
        engine._was_playing = True
        engine._last_uri = "spotify:track:a"
        ended = []
        engine.on_end = lambda e, r: ended.append(r)

        self._poll_once(engine, {"stopped": False, "paused": True, "track": {"uri": "spotify:track:a"}})

        self.assertEqual(ended, [])

    def test_a_track_changing_underneath_us_counts_as_an_end(self):
        engine = make_engine()
        engine._was_playing = True
        engine._last_uri = "spotify:track:a"
        ended = []
        engine.on_end = lambda e, r: ended.append(r)

        self._poll_once(engine, {"stopped": False, "paused": False, "track": {"uri": "spotify:track:b"}})

        self.assertEqual(ended, ["eof"])

    def test_nothing_is_reported_when_we_were_not_playing(self):
        engine = make_engine()
        engine._was_playing = False
        ended = []
        engine.on_end = lambda e, r: ended.append(r)

        self._poll_once(engine, {"stopped": True, "paused": False, "track": {}})

        self.assertEqual(ended, [])


if __name__ == "__main__":
    unittest.main()
