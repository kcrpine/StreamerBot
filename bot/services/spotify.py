"""Spotify search and playback.

Playback is handed to LibrespotEngine, which is signed in as its own Spotify
Connect device and needs nothing from the Web API. Search and metadata do go
through the Web API, purely to turn "play nirvana" into a track URI.

Those two halves authenticate separately, which is worth understanding before
changing anything here:

- **Playback** pairs the user's own account with a device code, no registered
  application involved.
- **Search** needs a Spotify application (client ID and secret) registered once
  by the operator. go-librespot could mint a Web API token itself in 0.9.0,
  which would have avoided this, but 0.9.1 removed that endpoint in the very
  release that added the device auth flow the engine depends on. The engine path
  is still tried first, so this works again for free if it ever returns.

Without a client ID and secret, search is unavailable and says so, but a pasted
Spotify link still plays.

Tracks are TrackType.External with engine "librespot": their url is a
`spotify:track:...` URI, which identifies a track but is not a stream and cannot
be resolved into one. Nothing in the lazy-resolution path should ever touch them.

Tracks are TrackType.External with engine "librespot": their url is a
`spotify:track:...` URI, which identifies a track but is not a stream and cannot
be resolved into one. Nothing in the lazy-resolution path should ever touch them.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import requests

from bot import errors
from bot.player.enums import TrackType
from bot.player.track import Track
from bot.services import Service

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"

# The daemon mints tokens on demand; this only avoids asking on every keystroke.
TOKEN_TTL_SECONDS = 1800


class SpotifyService(Service):
    name = "sp"
    hidden = False
    hostnames = ["open.spotify.com", "spotify.com"]
    engine = "librespot"
    requires_auth = True
    supports_audio_description = False

    def __init__(self, bot: Bot, config: Any) -> None:
        self.config = config
        self.translator = bot.translator
        self.is_enabled = getattr(config, "enabled", True)
        self.error_message = ""
        self.warning_message = ""
        self.help = ""
        self._engine = None
        self._store = None
        self._token = ""
        self._token_at = 0.0

    def initialize(self) -> None:
        # The engine is attached by Player after ServiceManager exists, so this
        # only records that we are not ready yet; it is not an error.
        if self._engine is None:
            self.warning_message = self.translator.translate(
                "Spotify is not connected yet. Send li sp to connect an account."
            )

    def attach_engine(self, engine) -> None:
        self._engine = engine

    # -- auth --------------------------------------------------------------

    def _access_token(self) -> str:
        """A Spotify Web API token, for search and metadata only.

        Playback does not come through here at all: go-librespot is signed in as
        its own device and needs nothing from the Web API. This token is purely
        so the bot can turn "play nirvana" into a track URI.

        Two sources, in order. go-librespot could mint one in 0.9.0, and that
        needed no registered application; 0.9.1 removed the endpoint in the same
        release that added the device auth flow the engine depends on, so the
        fallback is the client credentials flow, which does need the operator to
        register a Spotify application once. Client credentials are app-level and
        involve no user login, so this is separate from, and additional to,
        pairing the account for playback.
        """
        now = time.time()
        if self._token and now - self._token_at < TOKEN_TTL_SECONDS:
            return self._token

        if self._engine is not None:
            token = self._engine.web_api_token(
                "user-read-private,user-read-email,playlist-read-private"
            )
            if token:
                self._token, self._token_at = token, now
                return token

        token = self._client_credentials_token()
        if not token:
            raise errors.ServiceError(
                self.translator.translate(
                    "Spotify search needs a Spotify application. Add a client ID and "
                    "secret in the bot's configuration, then try again. Playing a "
                    "pasted Spotify link does not need one."
                )
            )
        self._token, self._token_at = token, now
        return token

    def _client_credentials_token(self) -> Optional[str]:
        client_id = getattr(self.config, "client_id", "") or ""
        client_secret = self._secret("client_secret")
        if not client_id or not client_secret:
            return None
        try:
            response = requests.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                timeout=(5, 20),
            )
        except requests.RequestException as error:
            logger.warning(f"[spotify] Could not get a client token: {error}")
            return None
        if response.status_code >= 400:
            logger.warning(
                f"[spotify] Client credentials rejected (HTTP {response.status_code}). "
                "Check the client ID and secret."
            )
            return None
        try:
            return response.json().get("access_token")
        except ValueError:
            return None

    def _secret(self, field: str) -> str:
        """The client secret lives in the encrypted store, never in config.json."""
        store = self._store
        if store is None:
            return ""
        try:
            return store.get(self.name, field) or ""
        except Exception:
            return ""

    def attach_store(self, store) -> None:
        self._store = store

    def _get(self, path: str, **params: Any) -> Dict[str, Any]:
        try:
            response = requests.get(
                f"{API}{path}",
                headers={"Authorization": f"Bearer {self._access_token()}"},
                params=params,
                timeout=(5, 20),
            )
        except requests.RequestException as error:
            raise errors.ServiceError(f"Spotify is unreachable: {error}") from error

        if response.status_code == 401:
            # Expired despite the cache; drop it so the next call re-mints.
            self._token = ""
            raise errors.ServiceError(
                self.translator.translate("The Spotify session expired. Try again.")
            )
        if response.status_code == 403:
            raise errors.ServiceError(
                self.translator.translate(
                    "Spotify refused that request. Playback needs a Premium account."
                )
            )
        if response.status_code == 429:
            raise errors.ServiceError(
                self.translator.translate("Spotify is rate limiting us. Try again shortly.")
            )
        if response.status_code >= 400:
            raise errors.ServiceError(f"Spotify returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as error:
            raise errors.ServiceError("Spotify returned invalid JSON") from error

    def is_premium(self) -> Optional[bool]:
        """None when it cannot be determined, which is not the same as False."""
        try:
            return self._get("/me").get("product") == "premium"
        except errors.ServiceError:
            return None

    # -- track building ----------------------------------------------------

    def _track(self, item: Dict[str, Any]) -> Track:
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []) if a.get("name"))
        name = item.get("name", "")
        return Track(
            service=self.name,
            url=item.get("uri", ""),
            name=f"{artists} - {name}" if artists else name,
            # External: there is no stream to resolve, and the daemon is already
            # producing the audio.
            type=TrackType.External,
            engine="librespot",
            extra_info={
                "id": item.get("id", ""),
                "duration_ms": item.get("duration_ms"),
                "album": (item.get("album") or {}).get("name", ""),
            },
        )

    # -- the Service interface --------------------------------------------

    def get(self, url: str, extra_info: Optional[Dict] = None, process: bool = True) -> List[Track]:
        """Expand a Spotify URL or URI into tracks."""
        kind, ident = parse_spotify_url(url)
        if kind is None:
            raise errors.ServiceError(
                self.translator.translate("That is not a Spotify link.")
            )

        if kind == "track":
            return [self._track(self._get(f"/tracks/{ident}"))]

        if kind == "album":
            data = self._get(f"/albums/{ident}/tracks", limit=50)
            album = self._get(f"/albums/{ident}")
            tracks = []
            for item in data.get("items", []):
                item.setdefault("album", {"name": album.get("name", "")})
                tracks.append(self._track(item))
            return tracks

        if kind == "playlist":
            data = self._get(f"/playlists/{ident}/tracks", limit=100)
            return [
                self._track(entry["track"])
                for entry in data.get("items", [])
                if entry.get("track") and entry["track"].get("uri")
            ]

        if kind == "artist":
            data = self._get(f"/artists/{ident}/top-tracks", market="from_token")
            return [self._track(item) for item in data.get("tracks", [])]

        raise errors.ServiceError(
            self.translator.translate("That kind of Spotify link is not supported.")
        )

    def search(self, query: str, limit: Optional[int] = None) -> List[Track]:
        data = self._get("/search", q=query, type="track", limit=min(limit or 20, 50))
        items = (data.get("tracks") or {}).get("items", [])
        if not items:
            raise errors.NothingFoundError("")
        return [self._track(item) for item in items if item.get("uri")]

    def download(self, track: Track, file_path: str, video: bool = False) -> None:
        # Spotify audio is DRM protected and the daemon decrypts it only into the
        # sink. Saying so plainly is better than a generic failure.
        raise errors.UnsupportedOperationError(
            self.translator.translate("Spotify tracks cannot be downloaded.")
        )


def parse_spotify_url(url: str) -> tuple:
    """Accept both `spotify:track:ID` URIs and open.spotify.com links."""
    if not url:
        return None, None
    text = url.strip()

    if text.startswith("spotify:"):
        parts = text.split(":")
        if len(parts) >= 3 and parts[1] in ("track", "album", "playlist", "artist"):
            return parts[1], parts[2]
        return None, None

    if "open.spotify.com" in text or "spotify.com" in text:
        path = text.split("spotify.com", 1)[1]
        path = path.split("?", 1)[0].strip("/")
        segments = [s for s in path.split("/") if s]
        # Localised links carry a market segment: /intl-pt/track/ID
        segments = [s for s in segments if not s.startswith("intl-")]
        if len(segments) >= 2 and segments[0] in ("track", "album", "playlist", "artist"):
            return segments[0], segments[1]
    return None, None


__all__ = ["SpotifyService", "parse_spotify_url"]
