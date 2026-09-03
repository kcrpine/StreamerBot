"""The mpv engine: YouTube, YouTube Music, direct links and local files.

This is an extraction, not a rewrite. Every behaviour here -- the dynamic
User-Agent and header injection, the relative seeks, the speed limits -- is the
code that previously lived inline in Player, moved behind the PlaybackEngine
interface so the other engines have something to be symmetrical with.

The volume fade stayed in Player, since it is a policy that should apply to
whichever engine is active rather than a property of mpv.

Player keeps its own reference to the same mpv handle and keeps handling mpv's
own events, because those callbacks are inherently mpv-shaped and the
end-of-file path also carries the YouTube stream-refresh retry.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from bot.player.engines import PlaybackEngine

if TYPE_CHECKING:
    import mpv

    from bot.player.track import Track


class MpvEngine(PlaybackEngine):
    name = "mpv"
    supports_seek = True
    supports_speed = True
    supports_audio_description = False

    def __init__(self, player: "mpv.MPV", config: Any) -> None:
        super().__init__()
        self._player = player
        self._config = config
        self._current_user_agent: Optional[str] = None
        self._current_header_fields: Optional[List[str]] = None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self._player.terminate()
        except Exception as e:  # noqa: BLE001 - shutdown must not raise
            logging.debug(f"[MpvEngine] terminate failed: {e}")

    # -- playback ----------------------------------------------------------

    def play(self, track: Track) -> None:
        self.play_url(track.url, track)

    def play_url(self, url: str, track: Optional[Track] = None) -> None:
        """Play a URL directly.

        Separate from play() because the stream-refresh retry already has a
        freshly resolved URL and must not go back through Track.url, which would
        hand back the expired one.
        """
        self._apply_track_headers(track)
        self._player.pause = False
        self._player.play(url)

    def _apply_track_headers(self, track: Optional[Track]) -> None:
        """Apply per-track HTTP headers to mpv, only when they change.

        Some resolved stream URLs are only valid together with the headers the
        extractor saw, so these have to follow the track rather than be set once.
        """
        extra_info = getattr(track, "extra_info", None) or {}
        headers = extra_info.get("http_headers", {})
        target_ua = headers.get("User-Agent") if headers else None
        target_headers = (
            [f"{k}: {v}" for k, v in headers.items() if k.lower() != "user-agent"]
            if headers
            else []
        )

        if target_ua and self._current_user_agent != target_ua:
            try:
                self._player.user_agent = target_ua
                self._current_user_agent = target_ua
                logging.debug("[MpvEngine] Dynamic User-Agent applied to mpv")
            except Exception as e:  # noqa: BLE001
                logging.debug(f"[MpvEngine] Failed to apply User-Agent to mpv: {e}")

        if target_headers and self._current_header_fields != target_headers:
            try:
                self._player.http_header_fields = target_headers
                self._current_header_fields = target_headers
                logging.debug("[MpvEngine] Dynamic headers applied to mpv")
            except Exception as e:  # noqa: BLE001
                logging.debug(f"[MpvEngine] Failed to apply dynamic headers to mpv: {e}")

    def pause(self) -> None:
        self._player.pause = True

    def resume(self) -> None:
        self._player.pause = False

    def stop(self) -> None:
        self._player.stop()

    # -- transport ---------------------------------------------------------

    def set_volume(self, volume: int) -> None:
        # Immediate. Player owns the fade, so that starting a track does not
        # slide the volume every time.
        self._player.volume = volume

    def get_volume(self) -> Optional[float]:
        return self._player.volume

    def seek(self, offset: float) -> None:
        # mpv raises SystemError when there is nothing seekable loaded; the
        # long-standing behaviour is to treat that as end of playback.
        self._player.seek(offset, reference="relative")

    def get_position(self) -> Optional[float]:
        return self._player.time_pos

    def get_duration(self) -> Optional[float]:
        return self._player.duration

    def get_speed(self) -> float:
        return self._player.speed

    def set_speed(self, speed: float) -> None:
        if speed < 0.25 or speed > 4:
            raise ValueError()
        self._player.speed = speed

    def get_metadata(self) -> Dict[str, Any]:
        return self._player.metadata or {}

    # -- sound devices -----------------------------------------------------

    def get_output_devices(self) -> List[Any]:
        return list(self._player.audio_device_list or [])

    def set_output_device(self, id: str) -> None:
        self._player.audio_device = id
