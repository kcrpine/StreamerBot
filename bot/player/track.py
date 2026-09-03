from __future__ import annotations
import copy
import logging
import os
import time
from threading import Lock
from typing import Any, Dict, Optional, TYPE_CHECKING

from bot.player.enums import TrackType
from bot import utils

if TYPE_CHECKING:
    from bot.services import Service


class Track:
    format: str
    type: TrackType

    def __init__(
        self,
        service: str = "",
        url: str = "",
        name: str = "",
        format: str = "",
        extra_info: Optional[Dict[str, Any]] = None,
        type: TrackType = TrackType.Default,
        extracted_at: float = 0.0,
        engine: str = "mpv",
    ) -> None:
        self.service = service
        self.url = url
        self.name = name
        self.format = format
        self.extra_info = extra_info
        self.type = type
        self.extracted_at = extracted_at or time.perf_counter()
        # Which playback engine handles this track: "mpv", "librespot" or
        # "browser". Read with getattr elsewhere, because tracks unpickled from
        # a cache written before this field existed will not have it.
        self.engine = engine
        self._lock = Lock()
        self._is_fetched = False
        self._fetch_failed = False

    def download(self, directory: str, video: bool = False) -> str:
        service: Service = get_service_by_name(self.service)
        format = self.format if not video else "mp4"
        file_name = self.name + "." + format
        file_name = utils.clean_file_name(file_name)
        file_path = os.path.join(directory, file_name)
        service.download(self, file_path, video=video)
        return file_path

    def _fetch_stream_data(self):
        if self.type != TrackType.Dynamic or self._is_fetched or self._fetch_failed:
            return
        self._original_track = copy.deepcopy(self)
        service: Service = get_service_by_name(self.service)
        try:
            track = service.get(self._url, extra_info=self.extra_info, process=True)[0]
        except Exception as e:
            logging.error(f"Failed to fetch stream data for '{self._name or self._url}': {e}")
            self._fetch_failed = True
            raise
        self.url = track.url
        self.name = track.name
        self._original_track.name = track.name
        self.format = track.format
        self.type = track.type
        self.extra_info = track.extra_info
        self._is_fetched = True

    def refresh_stream(self) -> str:
        """Re-resolve an expired stream URL and return the fresh one.

        Only meaningful for engines that play a URL. Spotify and the browser
        hold their own sessions and have nothing to refresh, and their services
        expose no _bridge, so both conditions below reject them.
        """
        service: Service = get_service_by_name(self.service)
        if getattr(service, "engine", "mpv") != "mpv" or not hasattr(service, "_bridge"):
            raise RuntimeError("Stream refresh is not supported for this service")

        with self._lock:
            original = getattr(self, "_original_track", self)
            original_info = copy.deepcopy(original.extra_info or {})
            video_id = (
                original_info.get("videoId")
                or original_info.get("id")
                or original_info.get("contentId")
            )
            source_url = original._url
            service._bridge.invalidate(
                video_id=video_id or "",
                url="" if video_id else source_url,
            )

            self._url = source_url
            self._name = original._name
            self.extra_info = original_info
            self.type = TrackType.Dynamic
            self._is_fetched = False
            self._fetch_failed = False
            self._fetch_stream_data()
            return self._url

    @property
    def url(self) -> str:
        if self.type != TrackType.Dynamic or self._is_fetched or self._fetch_failed:
            return self._url
        with self._lock:
            if self.type != TrackType.Dynamic or self._is_fetched or self._fetch_failed:
                return self._url
            started_at = time.perf_counter()
            logging.info(
                "[PlaybackTiming] track_url_started "
                f"track_id={id(self)} cache_hit=False "
                f"service={self.service} track={self._name!r}"
            )
            self._fetch_stream_data()
            url = self._url
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            logging.info(
                "[PlaybackTiming] track_url_completed "
                f"elapsed_ms={elapsed_ms:.2f} track_id={id(self)} "
                f"cache_hit=False service={self.service} track={self._name!r}"
            )
            return url

    @url.setter
    def url(self, value: str) -> None:
        self._url = value

    @property
    def name(self) -> str:
        with self._lock:
            if not self._name:
                self._fetch_stream_data()
            return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    def get_meta(self) -> Dict[str, Any]:
        try:
            return {"name": self.name, "url": self.url}
        except:
            return {"name": None, "url": ""}

    def get_raw(self) -> Track:
        if hasattr(self, "_original_track"):
            return self._original_track
        else:
            return self

    def __bool__(self):
        if self.service or self.url:
            return True
        else:
            return False

    def __getstate__(self) -> Dict[str, Any]:
        state: Dict[str, Any] = self.__dict__.copy()
        del state["_lock"]
        return state

    def __setstate__(self, state: Dict[str, Any]):
        self.__dict__.update(state)
        # Recents and favourites pickled before engines existed have no engine
        # field. Everything from that era was played by mpv.
        self.__dict__.setdefault("engine", "mpv")
        self._lock = Lock()
