"""The Service half of the browser-backed services.

Netflix, Disney+, Apple Music and Amazon Music differ almost entirely in their
adapters. What the rest of the bot sees — search, get, tracks, profiles — is the
same shape for all four, so it lives here once and each service is a few lines
of declaration.

Every track is TrackType.External with engine "browser": the url identifies a
title but is not a stream, and the audio exists only because Chrome is playing
it into the sink.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from bot import errors
from bot.player.enums import TrackType
from bot.player.track import Track
from bot.services import Service
from bot.services.results import (
    KIND_LABELS,
    KIND_LABELS_PLURAL,
    KIND_ORDER,
    describe_results,
    describe_tracks,
    order_results,
)

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger(__name__)

class BrowserService(Service):
    """Shared behaviour for the four services the browser engine plays."""

    engine = "browser"
    requires_auth = True
    hidden = False

    #: Human name used in messages, so "Disney Plus" rather than "dp".
    display_name = ""

    def __init__(self, bot: Bot, config: Any) -> None:
        self.config = config
        self.translator = bot.translator
        self.is_enabled = getattr(config, "enabled", True)
        self.error_message = ""
        self.warning_message = ""
        self._engine = None

    def help_intro(self) -> str:
        """What this one service is for, prepended to the shared help below.

        A method returning a translate() call with a literal in it, not a class
        attribute passed through translate(): Babel extracts what it can see at
        the call site, and translate(self.something) is invisible to it, which
        ships an English string in seven catalogs that look complete.
        """
        return ""

    @property
    def help(self) -> str:
        """What `sv SERVICE h` answers, above the live status line the command adds.

        Short, and the command to run goes last after a colon with no full stop,
        so a screen reader's review cursor lands on it and nothing trailing is
        read as part of it — the same rule the YouTube sign-in messages follow.
        """
        t = self.translator.translate
        lines = []
        intro = self.help_intro()
        if intro:
            lines.append(intro)
        lines.append(
            t(
                "Search it with p and a word or two. With search results mode on "
                "(sr), p lists what it found and sl and a number plays one of them."
            )
        )
        lines.append(
            t(
                "The account name and password are typed on a web page, never in "
                "this channel, where everyone present would see them."
            )
        )
        lines.append(
            t(
                "It plays through Google Chrome on the bot's own machine, so it "
                "does not work on a Raspberry Pi or any other ARM machine."
            )
        )
        lines.append(
            t("To connect an account, send this command: li %(code)s")
            % {"code": self.name}
        )
        return "\n".join(lines)

    def initialize(self) -> None:
        if self._engine is None:
            self.warning_message = self.translator.translate(
                "%(service)s is not ready yet."
            ) % {"service": self.display_name}

    def attach_engine(self, engine) -> None:
        self._engine = engine
        # The warning initialize() left behind described the moment before this
        # call, and nothing else ever wrote to the field. Left uncleared it
        # outlived its condition for the life of the process, so `sv am` called
        # Apple Music "not ready yet" while Apple Music was playing.
        self.warning_message = ""

    # -- guards ------------------------------------------------------------

    def _require_engine(self):
        if self._engine is None:
            raise errors.EngineUnavailableError(
                self.translator.translate(
                    "%(service)s needs Google Chrome, which is not available on this "
                    "machine's processor architecture."
                ) % {"service": self.display_name}
            )
        return self._engine

    def _require_login(self) -> None:
        if not self._require_engine().is_logged_in(self.name):
            raise errors.NotSignedInError(
                self.name,
                self.translator.translate(
                    "%(service)s is not connected. Send li %(code)s to connect an account."
                ) % {"service": self.display_name, "code": self.name},
            )

    # -- tracks ------------------------------------------------------------

    def _track(self, item: Dict[str, Any]) -> Track:
        kind = item.get("kind", "title")
        return Track(
            service=self.name,
            url=item.get("url", ""),
            name=item.get("title", ""),
            type=TrackType.External,
            engine="browser",
            extra_info={"id": item.get("id", ""), "kind": kind},
        )

    def order_results(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Containers before individual tracks, service ranking kept within each."""
        return order_results(results)

    def describe_results(self, results: List[Dict[str, Any]]) -> str:
        """A numbered list, each line naming its kind first."""
        return describe_results(self.translator, results)

    def describe_tracks(self, tracks: List[Any]) -> str:
        """The same list, once the results have become Tracks."""
        return describe_tracks(self.translator, tracks)

    # -- the Service interface --------------------------------------------

    def get(self, url: str, extra_info: Optional[Dict] = None, process: bool = True) -> List[Track]:
        if not url:
            raise errors.ServiceError(
                self.translator.translate("That is not a %(service)s link.")
                % {"service": self.display_name}
            )
        self._require_login()
        return [self._track({"id": url, "title": url, "kind": "title", "url": url})]

    def search(self, query: str, limit: Optional[int] = None) -> List[Track]:
        self._require_login()
        results = self._require_engine().search(self.name, query)
        if not results:
            raise errors.NothingFoundError("")
        results = self.order_results(results)[: (limit or 25)]
        return [self._track(item) for item in results]

    def download(self, track: Track, file_path: str, video: bool = False) -> None:
        raise errors.UnsupportedOperationError(
            self.translator.translate("%(service)s cannot be downloaded.")
            % {"service": self.display_name}
        )

    # -- extras ------------------------------------------------------------

    def list_profiles(self) -> List[Dict[str, Any]]:
        return self._require_engine().list_profiles(self.name)

    def select_profile(self, profile_id: str) -> bool:
        return self._require_engine().select_profile(self.name, profile_id)

    def enable_audio_description(self) -> bool:
        if not self.supports_audio_description:
            return False
        return self._require_engine().enable_audio_description(self.name)


class DisneyService(BrowserService):
    name = "dp"
    display_name = "Disney Plus"
    hostnames = ["disneyplus.com", "www.disneyplus.com"]
    supports_audio_description = True

    def help_intro(self) -> str:
        return self.translator.translate(
            "Disney Plus plays films and shows, with audio description where "
            "Disney Plus offers one. Each account has its own profiles, with "
            "their own watchlists and audio settings; pf lists them and pf with "
            "a number picks one."
        )


class AppleMusicService(BrowserService):
    name = "am"
    display_name = "Apple Music"
    hostnames = ["music.apple.com"]
    # Music, not video: there is no described track to offer.
    supports_audio_description = False

    def help_intro(self) -> str:
        return self.translator.translate(
            "Apple Music plays songs, albums, artists and playlists. A search "
            "returns all four, each named by what it is, so an album and the "
            "song on it are told apart. dl downloads what is playing and dlp "
            "downloads the album it is from."
        )

    def now_playing(self) -> Optional[Dict[str, Any]]:
        """The song MusicKit is playing, which inside an album is not the queued Track."""
        return self._require_engine().now_playing(self.name)

    def cookies(self) -> List[Dict[str, Any]]:
        """The signed-in browser profile's cookies, which gamdl downloads with."""
        return self._require_engine().cookies(self.name)


class AmazonMusicService(BrowserService):
    name = "az"
    display_name = "Amazon Music"
    hostnames = ["music.amazon.com", "music.amazon.co.uk"]
    supports_audio_description = False

    def help_intro(self) -> str:
        return self.translator.translate(
            "Amazon Music plays songs, albums, artists and playlists. A search "
            "returns all four, each named by what it is. It needs an Amazon "
            "account with a music subscription; a plain Prime account plays "
            "only part of the catalogue."
        )


__all__ = [
    "BrowserService",
    "KIND_LABELS",
    "KIND_LABELS_PLURAL",
    "DisneyService",
    "AppleMusicService",
    "AmazonMusicService",
    "KIND_ORDER",
]
