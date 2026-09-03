"""Playback engines.

mpv plays a URL. The Spotify daemon and the browser do not: by the time they are
involved they are already producing audio into the PulseAudio sink, and there is
no URL to hand anyone. So Player stops being a thin mpv wrapper and becomes a
transport over a set of engines, each of which knows how to start, stop and
control one kind of source.

Exactly one engine is active at a time. That is what keeps the audio topology
sane -- every engine feeds the same null sink, and the TeamTalk SDK captures its
monitor -- and it is enforced by Player, not by the engines.

Engines never touch Player.state. Only Player mutates it, which is what keeps
TTPlayerConnector (voice transmission and the status text) working unchanged.
An engine reports that its source ended by calling on_end; Player decides what
that means for the queue and the play mode.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from bot import errors

if TYPE_CHECKING:
    from bot.player.track import Track


class PlaybackEngine(ABC):
    """One way of turning a Track into audio in the sink."""

    #: Matches Track.engine and the "engine" attribute on Service.
    name: str = ""

    #: Capability flags. Commands surface UnsupportedOperationError as a plain
    #: "not supported for this service" message rather than an error.
    supports_seek: bool = True
    supports_speed: bool = False
    supports_audio_description: bool = False

    def __init__(self) -> None:
        # Set by Player. Called by the engine when its source ends on its own,
        # from whatever thread the engine happens to use.
        self.on_end: Callable[[PlaybackEngine, str], None] = lambda engine, reason: None

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        """Prepare the engine. Raise errors.EngineUnavailableError if impossible."""

    def close(self) -> None:
        """Release everything. Must be safe to call more than once."""

    # -- playback ----------------------------------------------------------

    def can_play(self, track: Track) -> bool:
        return getattr(track, "engine", "mpv") == self.name

    @abstractmethod
    def play(self, track: Track) -> None:
        """Start this track. Raise errors.ServiceError if it cannot be started."""

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def resume(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    # -- transport ---------------------------------------------------------

    def set_volume(self, volume: int) -> None:
        raise errors.UnsupportedOperationError("volume")

    def get_volume(self) -> Optional[float]:
        return None

    def seek(self, offset: float) -> None:
        """Seek by offset seconds, relative to the current position."""
        raise errors.UnsupportedOperationError("seek")

    def get_position(self) -> Optional[float]:
        return None

    def get_duration(self) -> Optional[float]:
        return None

    def get_speed(self) -> float:
        raise errors.UnsupportedOperationError("speed")

    def set_speed(self, speed: float) -> None:
        raise errors.UnsupportedOperationError("speed")

    def get_metadata(self) -> Dict[str, Any]:
        return {}

    def producer_pids(self) -> List[int]:
        """PIDs feeding the sink, for the mixer's mute-all-except safety net."""
        return []
