from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Any

import requests

from bot import errors

logger = logging.getLogger(__name__)


class YouTubeBridge:
    def __init__(self, client: str = "WEB") -> None:
        self.base_url = os.getenv("YOUTUBE_BRIDGE_URL", "http://127.0.0.1:4417")
        self.bot_id = os.getenv("TTBOT_INSTANCE", "")
        self.client = client
        self.timeout = (5, 30)
        self._resolve_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._resolve_cache_lock = threading.Lock()

    def _post(self, endpoint: str, timeout: tuple[int, int] | None = None, **payload: Any) -> dict[str, Any]:
        payload.setdefault("bot_id", self.bot_id)
        payload.setdefault("client", self.client)
        for attempt in range(5):
            try:
                response = requests.post(
                    f"{self.base_url}{endpoint}",
                    json=payload,
                    timeout=timeout or self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                break
            except requests.ConnectionError as exc:
                if attempt == 4:
                    raise errors.ServiceError(f"YouTube.js bridge unavailable: {exc}") from exc
                time.sleep(0.25 * (2 ** attempt))
            except requests.RequestException as exc:
                detail = ""
                if getattr(exc, "response", None) is not None:
                    try:
                        detail = exc.response.json().get("error", "")
                    except ValueError:
                        detail = exc.response.text[:500]
                message = detail or str(exc)
                raise errors.ServiceError(f"YouTube.js bridge error: {message}") from exc
            except ValueError as exc:
                raise errors.ServiceError("YouTube.js bridge returned invalid JSON") from exc

        if data.get("error"):
            raise errors.ServiceError(data["error"])
        return data

    def health(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/health", timeout=(1, 2))
            return response.ok
        except requests.RequestException:
            return False

    def wait_ready(self, timeout: float = 5.0) -> bool:
        import time
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            if self.health():
                return True
            time.sleep(0.05)
        return self.health()

    # -- OAuth device-code sign-in -----------------------------------------
    #
    # Replaces the cookies.txt file the operator used to re-export by hand.
    # The bridge holds the credentials per bot under
    # bots/<bot_id>/youtube_auth/ and refreshes them itself.

    def auth_start(self) -> dict[str, Any]:
        """Begin sign-in. Returns the URL and code the user has to enter.

        The bridge answers as soon as YouTube issues the code, not when the
        user finishes, so this returns in about a second.
        """
        return self._post("/auth/start", timeout=(5, 30))

    def auth_status(self) -> dict[str, Any]:
        return self._post("/auth/status", timeout=(5, 15))

    def auth_signout(self) -> dict[str, Any]:
        return self._post("/auth/signout", timeout=(5, 15))

    def is_signed_in(self) -> bool:
        """Never raises: sign-in state is informational, not load-bearing."""
        try:
            return bool(self.auth_status().get("signed_in"))
        except errors.ServiceError:
            return False

    def resolve(self, url: str = "", video_id: str = "") -> dict[str, Any]:
        cache_key = video_id or url
        now_ms = time.time() * 1000
        if cache_key:
            with self._resolve_cache_lock:
                cached = self._resolve_cache.get(cache_key)
                if cached and cached[0] > now_ms:
                    return cached[1]
                self._resolve_cache.pop(cache_key, None)

        resolved = self._post("/resolve", url=url or None, video_id=video_id or None)
        expires_at_ms = float(resolved.get("cache_expires_at_ms") or 0)
        if cache_key and expires_at_ms > now_ms:
            with self._resolve_cache_lock:
                self._resolve_cache[cache_key] = (expires_at_ms, resolved)
        return resolved

    def invalidate(self, url: str = "", video_id: str = "") -> None:
        cache_key = video_id or url
        if cache_key:
            with self._resolve_cache_lock:
                self._resolve_cache.pop(cache_key, None)
        self._post("/invalidate", url=url or None, video_id=video_id or None)

    def info(self, url: str = "", video_id: str = "") -> dict[str, Any]:
        return self._post("/info", url=url or None, video_id=video_id or None)

    def playlist(self, url: str) -> dict[str, Any]:
        return self._post("/playlist", timeout=(5, 300), url=url)

    def search(
        self,
        query: str,
        limit: int,
        mode: str = "video",
    ) -> dict[str, Any]:
        return self._post("/search", query=query, limit=limit, mode=mode)

    def recommendations(self, video_id: str, limit: int = 20) -> dict[str, Any]:
        return self._post("/recommendations", video_id=video_id, limit=limit)

    def download(self, url: str, file_path: str, video: bool = False) -> None:
        plan = self._post("/download-plan", url=url, video=video)
        headers = plan.get("http_headers") or {}
        user_agent = headers.get("User-Agent", "")
        base_cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if user_agent:
            base_cmd += ["-user_agent", user_agent]

        if not video:
            audio_url = (plan.get("audio") or {}).get("url")
            if not audio_url:
                raise errors.ServiceError("YouTube.js did not return an audio stream")
            cmd = base_cmd + ["-i", audio_url, "-vn", "-codec:a", "libmp3lame", "-b:a", "320k", file_path]
        else:
            video_url = (plan.get("video") or {}).get("url")
            audio_url = (plan.get("audio") or {}).get("url")
            if not video_url or not audio_url:
                raise errors.ServiceError("YouTube.js did not return video/audio streams")
            cmd = base_cmd + [
                "-i", video_url,
                "-i", audio_url,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-movflags", "+faststart",
                file_path,
            ]

        logger.info("YouTube.js download: starting ffmpeg")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            raise errors.ServiceError(f"FFmpeg failed with exit code {exc.returncode}") from exc
