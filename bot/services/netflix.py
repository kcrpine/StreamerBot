"""Netflix as a Service, so the rest of the bot does not know it is a browser.

Tracks are TrackType.External with engine "browser" and a `netflix://watch/ID`
url. That URI is an identifier, not a stream: nothing can resolve it, and the
audio only exists because Chrome is playing it into the sink.

Rebroadcasting Netflix into a TeamTalk channel is very likely a breach of their
terms of service. That is the operator's decision to make, and the README says
so; this file only implements it.
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


class NetflixService(Service):
    name = "nf"
    hidden = False
    hostnames = ["netflix.com", "www.netflix.com"]
    engine = "browser"
    requires_auth = True
    supports_audio_description = True

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
            # Not an error: the engine is attached after ServiceManager exists,
            # and on arm64 it never will be.
            self.warning_message = self.translator.translate(
                "Netflix is not ready yet."
            )

    def attach_engine(self, engine) -> None:
        self._engine = engine

    def _require_engine(self):
        if self._engine is None:
            raise errors.EngineUnavailableError(
                self.translator.translate(
                    "Netflix needs Google Chrome, which is not available on this "
                    "machine's processor architecture."
                )
            )
        return self._engine

    def _require_login(self) -> None:
        engine = self._require_engine()
        if not engine.is_logged_in(self.name):
            raise errors.NotSignedInError(
                self.name,
                self.translator.translate(
                    "Netflix is not connected. Send li nf to connect an account."
                ),
            )

    # -- track building ----------------------------------------------------

    def _track(self, item: Dict[str, Any]) -> Track:
        return Track(
            service=self.name,
            url=item.get("url") or f"netflix://watch/{item.get('id', '')}",
            name=item.get("title", ""),
            type=TrackType.External,
            engine="browser",
            extra_info={"id": item.get("id", ""), "kind": item.get("kind", "title")},
        )

    # -- the Service interface --------------------------------------------

    def get(self, url: str, extra_info: Optional[Dict] = None, process: bool = True) -> List[Track]:
        import re

        match = re.search(r"(?:netflix://watch/|netflix\.com/watch/)(\d+)", url or "")
        if not match:
            raise errors.ServiceError(
                self.translator.translate("That is not a Netflix link.")
            )
        self._require_login()
        return [self._track({"id": match.group(1), "title": url, "kind": "title"})]

    def search(self, query: str, limit: Optional[int] = None) -> List[Track]:
        self._require_login()
        results = self._require_engine().search(self.name, query)
        if not results:
            raise errors.NothingFoundError("")
        return [self._track(item) for item in results[: (limit or 25)]]

    def download(self, track: Track, file_path: str, video: bool = False) -> None:
        raise errors.UnsupportedOperationError(
            self.translator.translate("Netflix titles cannot be downloaded.")
        )

    # -- things only this service has -------------------------------------

    def list_profiles(self) -> List[Dict[str, Any]]:
        return self._require_engine().list_profiles(self.name)

    def select_profile(self, profile_id: str) -> bool:
        return self._require_engine().select_profile(self.name, profile_id)

    def enable_audio_description(self) -> bool:
        return self._require_engine().enable_audio_description(self.name)


__all__ = ["NetflixService"]
