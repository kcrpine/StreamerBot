"""Spotify playback through go-librespot.

go-librespot is a Spotify Connect device. It is not a library we call to fetch
audio: it is a daemon that logs in as its own device, receives playback commands,
and writes PCM straight into PulseAudio. By the time it is playing there is no
URL and no stream object anywhere in this process, which is the whole reason
Player had to grow engines.

Chosen over Rust librespot because it exposes a local HTTP control API. Rust
librespot has none, which would have meant driving playback by restarting the
process. It also publishes arm64 builds, so **Spotify works on Raspberry Pi even
though the browser services do not.**

Endpoints, confirmed by probing the running daemon rather than taken from docs:

    POST /player/play {uri, skip_to_uri, paused}   POST /player/pause
    POST /player/resume                            POST /player/seek {position}
    POST /player/next                              POST /player/prev
    GET  /player/volume        POST /player/volume {volume}
    GET  /status               GET  /auth/code     POST /token {scopes}
    GET  /events  (426 Upgrade Required: WebSocket)

Track ends are detected by polling /status rather than by holding the WebSocket
open. A WebSocket client is a new dependency for one event, and polling costs a
sub-second delay before the queue advances, which is the same order as the gap
mpv already leaves between tracks.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import requests

from bot import errors
from bot.player.engines import PlaybackEngine

if TYPE_CHECKING:
    from bot.player.track import Track

logger = logging.getLogger(__name__)

# Long enough that a slow first login on a Pi is not mistaken for a failure.
STARTUP_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.5
RESTART_BACKOFF_SECONDS = (1, 2, 5, 15, 30, 60)


class LibrespotEngine(PlaybackEngine):
    name = "librespot"
    supports_seek = True
    # Spotify has no playback rate control, so `sp` reports "not supported for
    # this service" rather than appearing to work.
    supports_speed = False
    supports_audio_description = False

    def __init__(self, data_dir: str, device_name: str = "StreamerBot", port: int = 3678) -> None:
        super().__init__()
        self._dir = data_dir
        self._device_name = device_name
        self._port = port
        self._base = f"http://127.0.0.1:{port}"
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._closing = False
        self._monitor: Optional[threading.Thread] = None
        self._supervisor: Optional[threading.Thread] = None
        self._last_uri: Optional[str] = None
        self._was_playing = False

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        if not os.path.exists("/usr/local/bin/go-librespot"):
            raise errors.EngineUnavailableError(
                "go-librespot is not installed in this image."
            )
        os.makedirs(self._dir, mode=0o700, exist_ok=True)
        self._write_config()
        self._start_daemon()

        self._supervisor = threading.Thread(
            target=self._supervise, name="LibrespotSupervisor", daemon=True
        )
        self._supervisor.start()
        self._monitor = threading.Thread(
            target=self._poll_status, name="LibrespotMonitor", daemon=True
        )
        self._monitor.start()

    def _write_config(self) -> None:
        """Write config.yml.

        device_auth gives a code-and-URL flow, the same shape as YouTube's, so a
        blind user on a headless server never needs a browser redirect or a
        registered developer application.
        """
        config = (
            f"device_name: {self._device_name}\n"
            "device_type: speaker\n"
            "audio_backend: pulseaudio\n"
            "zeroconf_enabled: false\n"
            "server:\n"
            "  enabled: true\n"
            "  address: 127.0.0.1\n"
            f"  port: {self._port}\n"
            "credentials:\n"
            "  type: device_auth\n"
        )
        path = os.path.join(self._dir, "config.yml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(config)

    @property
    def _daemon_log_path(self) -> str:
        return os.path.join(self._dir, "go-librespot.log")

    def _start_daemon(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            # stderr goes to a file in this bot's own librespot directory rather
            # than to the bot log or to /dev/null.
            #
            # /dev/null was the original choice because the daemon prints its
            # credentials blob on some paths, and that must not reach a log the
            # bot broadcasts or ships. But it also meant a daemon that exited one
            # second after every start, forever, reported nothing but "exited;
            # restarting" — no exit code, no reason — which made a plain port
            # clash undiagnosable from the log.
            #
            # This directory is already chmod 700 and holds credentials.json, so
            # it is the right side of the line the /dev/null choice was drawing.
            try:
                stderr_sink = open(self._daemon_log_path, "w", encoding="utf-8")
                os.chmod(self._daemon_log_path, 0o600)
            except OSError as error:
                logger.warning(
                    f"Could not open {self._daemon_log_path}, so go-librespot's "
                    f"output is discarded and a failure will have no reason: {error}"
                )
                stderr_sink = subprocess.DEVNULL

            self._process = subprocess.Popen(
                ["/usr/local/bin/go-librespot", "--config_dir", self._dir],
                stdout=subprocess.DEVNULL,
                stderr=stderr_sink,
                stdin=subprocess.DEVNULL,
            )
            if stderr_sink is not subprocess.DEVNULL:
                # The child holds its own descriptor now.
                stderr_sink.close()
        logger.info(f"go-librespot started on port {self._port}")

    def _daemon_failure_reason(self) -> str:
        """The last meaningful line the daemon printed, for the log.

        Bounded on purpose: the point is to name the cause, not to copy a
        credentials blob into the bot log line by line.
        """
        try:
            with open(self._daemon_log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = [line.strip() for line in f.readlines()[-40:] if line.strip()]
        except OSError:
            return ""
        if not lines:
            return ""
        for line in reversed(lines):
            lowered = line.lower()
            if "address already in use" in lowered or "bind" in lowered:
                return (
                    f"{line[:200]} — this is the go-librespot API port "
                    f"({self._port}) already being used by another bot on this "
                    f"machine. Run the manager, choose Manage Bots, then Repair "
                    f"Account Portal and Spotify Ports."
                )
        return lines[-1][:200]

    def _supervise(self) -> None:
        """Restart the daemon if it dies, backing off so a broken install does
        not spin."""
        attempt = 0
        reported_persistent_failure = False
        while not self._closing:
            time.sleep(1)
            with self._lock:
                process = self._process
            if self._closing:
                return
            if process is not None and process.poll() is None:
                if attempt:
                    logger.info("go-librespot is running again; Spotify is available")
                attempt = 0
                reported_persistent_failure = False
                continue

            code = process.poll() if process is not None else None
            reason = self._daemon_failure_reason()
            delay = RESTART_BACKOFF_SECONDS[min(attempt, len(RESTART_BACKOFF_SECONDS) - 1)]
            # The exit code and the reason were both missing before, so a daemon
            # failing identically every minute for hours said only "exited".
            logger.warning(
                f"go-librespot exited with code {code}; restarting in {delay}s"
                + (f". Reason: {reason}" if reason else "")
            )

            # Say once, at ERROR, that this is not transient. The backoff tops
            # out at a minute, so without this the only trace of a permanently
            # broken Spotify is a warning that repeats forever and reads the same
            # as a single restart.
            if attempt >= len(RESTART_BACKOFF_SECONDS) and not reported_persistent_failure:
                logger.error(
                    "go-librespot has failed to stay running after "
                    f"{attempt} attempts, so Spotify is unavailable on this bot. "
                    + (f"Reason: {reason}. " if reason else "")
                    + f"Its output is in {self._daemon_log_path}."
                )
                reported_persistent_failure = True

            time.sleep(delay)
            if self._closing:
                return
            try:
                self._start_daemon()
            except Exception as error:
                logger.error(f"Could not restart go-librespot: {error}")
            attempt += 1

    def close(self) -> None:
        self._closing = True
        with self._lock:
            process = self._process
            self._process = None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        logger.debug("go-librespot stopped")

    # -- http --------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Optional[Any]:
        try:
            response = requests.request(
                method, f"{self._base}{path}", timeout=(3, 10), **kwargs
            )
        except requests.RequestException as error:
            logger.debug(f"[librespot] {method} {path} failed: {error}")
            return None
        if response.status_code >= 400:
            logger.debug(f"[librespot] {method} {path} returned {response.status_code}")
            return None
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def is_running(self) -> bool:
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    def wait_ready(self, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
        """Wait for the control API to answer.

        Probes /auth/code rather than /status: before sign-in the daemon does
        not serve a useful /status, so waiting on that reports the API as down
        on exactly the path where the user still has to pair.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for path in ("/auth/code", "/status"):
                if self._request("GET", path) is not None:
                    return True
            time.sleep(0.25)
        return False

    # -- sign-in -----------------------------------------------------------

    def auth_code(self) -> Optional[Dict[str, Any]]:
        """The pairing code and URL, while the daemon is waiting for one.

        Returns None once it is signed in, which is how callers tell the
        difference between "needs sign-in" and "ready".
        """
        data = self._request("GET", "/auth/code")
        return data or None

    def is_signed_in(self) -> bool:
        status = self._request("GET", "/status")
        if status is None:
            return False
        # While waiting for device auth the daemon still serves /status, so the
        # presence of a pairing code is the reliable signal, not /status alone.
        return self.auth_code() is None

    def sign_out(self) -> None:
        """Forget the Spotify account.

        The daemon owns the credentials, so signing out means deleting its
        credentials file and restarting it, which drops it back into the
        device-auth flow waiting for a new pairing code.
        """
        path = os.path.join(self._dir, "credentials.json")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            logger.warning(f"[librespot] Could not remove credentials: {error}")
        with self._lock:
            process = self._process
            self._process = None
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        # The supervisor thread brings it back up on its next tick.
        logger.info("Spotify signed out; the player will restart and wait for a new code.")

    def web_api_token(self, scopes: str = "user-read-private,user-read-email") -> Optional[str]:
        """Ask the daemon for a Spotify Web API token.

        Present in 0.9.0 and **removed in 0.9.1** ("remove dead Web API
        passthrough"), which is the same release that added the device auth flow
        this engine depends on. Kept because it costs nothing and returns None
        cleanly when the endpoint is gone; SpotifyService treats None as "use
        client credentials instead" rather than as a failure.
        """
        data = self._request("POST", "/token", json={"scopes": scopes})
        if not data:
            return None
        return data.get("token") or data.get("access_token")

    # -- playback ----------------------------------------------------------

    def play(self, track: Track) -> None:
        uri = track.url
        if not uri or not uri.startswith("spotify:"):
            raise errors.ServiceError(f"Not a Spotify URI: {uri!r}")
        if not self.is_running():
            raise errors.ServiceError("The Spotify player is not running.")
        result = self._request("POST", "/player/play", json={"uri": uri, "paused": False})
        if result is None:
            raise errors.ServiceError("Spotify refused to start that track.")
        self._last_uri = uri
        self._was_playing = True

    def pause(self) -> None:
        self._request("POST", "/player/pause")

    def resume(self) -> None:
        self._request("POST", "/player/resume")

    def stop(self) -> None:
        # go-librespot has no stop, only pause. Pausing is what actually matters
        # here: it stops feeding the sink, which is the property Player relies on
        # when handing over to another engine.
        self._was_playing = False
        self._request("POST", "/player/pause")

    # -- transport ---------------------------------------------------------

    def set_volume(self, volume: int) -> None:
        # Player works in 0-100; go-librespot takes 0-65535.
        scaled = max(0, min(65535, round(volume * 65535 / 100)))
        self._request("POST", "/player/volume", json={"volume": scaled})

    def get_volume(self) -> Optional[float]:
        data = self._request("GET", "/player/volume")
        if not data or "volume" not in data:
            return None
        try:
            return round(int(data["volume"]) * 100 / 65535)
        except (TypeError, ValueError):
            return None

    def seek(self, offset: float) -> None:
        position = self.get_position()
        if position is None:
            raise errors.UnsupportedOperationError("seek")
        target = max(0, int((position + offset) * 1000))
        self._request("POST", "/player/seek", json={"position": target})

    def _status(self) -> Dict[str, Any]:
        return self._request("GET", "/status") or {}

    def get_position(self) -> Optional[float]:
        value = self._status().get("position")
        return None if value is None else float(value) / 1000.0

    def get_duration(self) -> Optional[float]:
        track = self._status().get("track") or {}
        value = track.get("duration")
        return None if value is None else float(value) / 1000.0

    def get_metadata(self) -> Dict[str, Any]:
        track = self._status().get("track") or {}
        artists = track.get("artist_names") or track.get("artists") or []
        if isinstance(artists, str):
            artists = [artists]
        return {
            "title": track.get("name", ""),
            "artist": ", ".join(a for a in artists if a),
            "album": track.get("album_name", ""),
        }

    def producer_pids(self) -> List[int]:
        with self._lock:
            process = self._process
        return [process.pid] if process is not None and process.poll() is None else []

    # -- end detection -----------------------------------------------------

    def _poll_status(self) -> None:
        """Report a finished track by polling, and ignore every other reason
        playback stopped.

        Only an actual end-of-track should advance the queue. A pause from the
        Spotify app, a handover to another engine, or the daemon restarting must
        not, or the bot would skip a track every time any of those happened.
        """
        while not self._closing:
            time.sleep(POLL_INTERVAL_SECONDS)
            if self._closing or not self._was_playing:
                continue
            status = self._request("GET", "/status")
            if status is None:
                continue

            stopped = status.get("stopped")
            paused = status.get("paused")
            track = status.get("track") or {}
            uri = track.get("uri")

            # Track changed under us, or playback stopped without a pause: both
            # mean the track we started has finished.
            finished = bool(stopped) and not paused
            changed = uri is not None and self._last_uri is not None and uri != self._last_uri

            if finished or changed:
                self._was_playing = False
                self._last_uri = uri
                try:
                    self.on_end(self, "eof")
                except Exception as error:
                    logger.error(f"[librespot] end callback failed: {error}")


__all__ = ["LibrespotEngine"]
