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

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger(__name__)

# Which kinds are shown before which, when a search returns several. Artists,
# albums and playlists lead because they are containers: hearing them first lets
# a user pick a whole thing rather than wading through individual tracks.
KIND_ORDER = ("top", "artist", "album", "playlist", "series", "title", "track")

KIND_LABELS = {
    "artist": "Artist",
    "album": "Album",
    "playlist": "Playlist",
    "series": "Series",
    "title": "Title",
    "track": "Track",
    "top": "Top result",
}


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
        self.help = ""
        self._engine = None

    def initialize(self) -> None:
        if self._engine is None:
            self.warning_message = self.translator.translate(
                "%(service)s is not ready yet."
            ) % {"service": self.display_name}

    def attach_engine(self, engine) -> None:
        self._engine = engine

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
        """Group by kind without disturbing the service's own ranking.

        The service's ordering is its answer to the query and re-sorting it by
        title or duration produces worse results, so within each kind the
        original order is preserved exactly; only the groups are reordered.

        Grouping matters more here than in a visual interface. A sighted user
        skims a mixed list and picks out the album. A screen reader user hears
        all twenty-five entries in sequence, so "three albums, then the tracks"
        is navigable where an interleaved list is not.
        """
        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for item in results:
            by_kind.setdefault(item.get("kind", "title"), []).append(item)

        ordered: List[Dict[str, Any]] = []
        for kind in KIND_ORDER:
            ordered.extend(by_kind.pop(kind, []))
        for remaining in by_kind.values():  # anything the site invented
            ordered.extend(remaining)
        return ordered

    def describe_results(self, results: List[Dict[str, Any]]) -> str:
        """A numbered list, each line naming its kind first.

        Kind first because that is the word being listened for, and it lets
        someone stop reading once they hear the one they want.
        """
        counts: Dict[str, int] = {}
        for item in results:
            counts[item.get("kind", "title")] = counts.get(item.get("kind", "title"), 0) + 1

        summary_parts = [
            self.translator.translate("%(count)s %(kind)s")
            % {"count": count, "kind": self.translator.translate(KIND_LABELS.get(kind, kind))}
            for kind, count in counts.items()
        ]
        lines = [", ".join(summary_parts) + "."]
        for index, item in enumerate(results, 1):
            label = self.translator.translate(KIND_LABELS.get(item.get("kind", "title"), "Title"))
            lines.append(f"{index}. {label}: {item.get('title', '')}")
        return "\n".join(lines)

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


class AppleMusicService(BrowserService):
    name = "am"
    display_name = "Apple Music"
    hostnames = ["music.apple.com"]
    # Music, not video: there is no described track to offer.
    supports_audio_description = False


class AmazonMusicService(BrowserService):
    name = "az"
    display_name = "Amazon Music"
    hostnames = ["music.amazon.com", "music.amazon.co.uk"]
    supports_audio_description = False


__all__ = [
    "BrowserService",
    "DisneyService",
    "AppleMusicService",
    "AmazonMusicService",
    "KIND_ORDER",
]
