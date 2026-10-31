"""AudioVault jobs: streaming a film or season into the channel, or uploading it.

AudioVault serves files, not streams, so "stream" means: download, unpack if it
is a zip, then queue the audio files and play them. That takes minutes for a
season, so it runs on its own thread with one message when it starts and one
when playback begins, and nothing in between.

One job at a time per bot. A season is a large download and a second one at once
would slow both. Downloads land under the bot's own data directory and are
removed when the job ends, however it ends; the files being streamed stay until
the next AudioVault stream replaces them, because mpv is still reading them.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from bot import errors
from bot.services.audiovault import Entry, as_zip, local_track, unpack

if TYPE_CHECKING:
    from bot import Bot
    from bot.modules.uploader import Uploader
    from bot.TeamTalk.structs import User

logger = logging.getLogger(__name__)


class AudioVaultJobs:
    def __init__(self, bot: Bot, uploader: Uploader) -> None:
        self.bot = bot
        self.translator = bot.translator
        self.ttclient = bot.ttclient
        self.uploader = uploader
        self._busy = threading.Lock()

    @property
    def _data(self) -> str:
        return self.bot.config_manager.config_dir

    @property
    def _downloads(self) -> str:
        return os.path.join(self._data, "downloads")

    @property
    def _stream_dir(self) -> str:
        return os.path.join(self._data, "audiovault", "stream")

    def _service(self):
        return self.bot.service_manager.services.get("av")

    # -- what to say before starting ------------------------------------------

    def refusal(self) -> Optional[str]:
        """Said instead of starting: not connected, or a job already running."""
        service = self._service()
        if service is None or not service.is_enabled:
            return self.translator.translate("AudioVault is not available on this bot.")
        if not service.is_connected():
            return self.translator.translate(
                "To use AudioVault, connect an account. To connect one, send: li av"
            )
        if self._busy.locked():
            return self.translator.translate(
                "An AudioVault download is already running. When it has finished, "
                "search again with av and a title, for example: av spaceballs"
            )
        return None

    def _launch(self, target: Callable, args: Tuple, started: str) -> str:
        refusal = self.refusal()
        if refusal:
            return refusal
        if not self._busy.acquire(blocking=False):
            return self.refusal() or ""
        try:
            threading.Thread(target=target, args=args, daemon=True, name="AudioVaultJob").start()
        except Exception:
            self._busy.release()
            raise
        return started

    # -- streaming ---------------------------------------------------------------

    def stream(self, name: str, entry: Entry, user: User, episodes: bool) -> str:
        """Fetch, unpack and play one film or one season. episodes: a season."""
        started = self.translator.translate(
            "Getting {name} ready to stream. It has to be downloaded and unpacked "
            "first, which can take a few minutes."
        ).format(name=name)
        return self._launch(self._run_stream, (name, entry, user, episodes), started)

    def _run_stream(self, name: str, entry: Entry, user: User, episodes: bool) -> None:
        job_dir = None
        try:
            job_dir = self._job_dir()
            path = self._service().client.fetch(entry, job_dir, self._service().max_bytes)
            # The previous stream's files go now; this one replaces them.
            shutil.rmtree(self._stream_dir, ignore_errors=True)
            files = unpack(path, self._stream_dir, self._service().max_bytes,
                           self.translator.translate)
            if files == [path]:
                # A single file, still in the job directory that is about to go.
                os.makedirs(self._stream_dir, exist_ok=True)
                moved = os.path.join(self._stream_dir, os.path.basename(path))
                shutil.move(path, moved)
                files = [moved]
            if len(files) == 1:
                tracks = [local_track(files[0], name)]
            else:
                tracks = [
                    local_track(f, f"{name}, {os.path.splitext(os.path.basename(f))[0]}")
                    for f in files
                ]
            self.bot.player.play(tracks)
            if episodes and len(files) > 1:
                message = "\n".join([
                    self.translator.translate("{name} is playing.").format(name=name),
                    self.translator.translate("Episodes: {count}").format(count=len(files)),
                ])
            else:
                message = self.translator.translate("{name} is playing.").format(name=name)
            self.ttclient.send_message(message, user)
        except (errors.ServiceError, errors.NotSignedInError) as error:
            self._say_failed(name, str(error), user)
        except Exception as error:
            logger.error(f"[av] stream of {entry.id} failed: {error}", exc_info=True)
            self._say_failed(name, "", user)
        finally:
            if job_dir:
                shutil.rmtree(job_dir, ignore_errors=True)
            self._busy.release()

    # -- downloading to the channel's files ---------------------------------------

    def download(self, name: str, entry: Entry, user: User) -> str:
        """One film or one season, uploaded as one zip file."""
        started = self.translator.translate(
            "Downloading {name}. It will be uploaded to the channel's files as a zip "
            "file. This can take several minutes."
        ).format(name=name)
        return self._launch(self._run_download, (name, entry, user), started)

    def _run_download(self, name: str, entry: Entry, user: User) -> None:
        try:
            if self._fetch_and_upload(entry, user):
                self.ttclient.send_message(
                    self.translator.translate("{name} is in the channel's files as a zip file.")
                    .format(name=name),
                    user,
                )
        except (errors.ServiceError, errors.NotSignedInError) as error:
            self._say_failed(name, str(error), user)
        except Exception as error:
            logger.error(f"[av] download of {entry.id} failed: {error}", exc_info=True)
            self._say_failed(name, "", user)
        finally:
            self._busy.release()

    def download_all(self, name: str, seasons: List[Tuple[str, Entry]], user: User) -> str:
        """Every season of a show, each its own zip file, one after another."""
        started = self.translator.translate(
            "Downloading every season of {name}, one zip file per season. This can "
            "take a long time."
        ).format(name=name)
        return self._launch(self._run_download_all, (name, seasons, user), started)

    def _run_download_all(self, name: str, seasons: List[Tuple[str, Entry]], user: User) -> None:
        uploaded = 0
        failed: List[str] = []
        reason = ""
        try:
            for label, entry in seasons:
                try:
                    if self._fetch_and_upload(entry, user):
                        uploaded += 1
                    else:
                        failed.append(label)
                except errors.NotSignedInError as error:
                    # Every later season would fail the same way.
                    reason = str(error)
                    failed.extend(l for l, _ in seasons[seasons.index((label, entry)):])
                    break
                except errors.ServiceError as error:
                    reason = str(error)
                    failed.append(label)
                except Exception as error:
                    logger.error(f"[av] season {entry.id} failed: {error}", exc_info=True)
                    failed.append(label)
            if not uploaded:
                self._say_failed_all(name, reason, user)
                return
            lines = [
                self.translator.translate("The seasons of {name} are in the channel's files.")
                .format(name=name),
                self.translator.translate("Zip files: {count}").format(count=uploaded),
            ]
            if failed:
                lines.append(
                    self.translator.translate("Seasons that could not be downloaded: {list}")
                    .format(list=", ".join(failed))
                )
            self.ttclient.send_message("\n".join(lines), user)
        finally:
            self._busy.release()

    def _fetch_and_upload(self, entry: Entry, user: User) -> bool:
        """Download one entry and upload it as a zip. False: the upload failed and
        the uploader has already said why."""
        job_dir = self._job_dir()
        try:
            service = self._service()
            path = service.client.fetch(entry, job_dir, service.max_bytes)
            # Checked to be audio, or a safe zip of audio, before it is uploaded.
            unpack(path, None, service.max_bytes, self.translator.translate)
            archive = as_zip(path, entry.name, job_dir)
            return bool(self.uploader.upload_file(archive, user))
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------------

    def _job_dir(self) -> str:
        os.makedirs(self._downloads, mode=0o700, exist_ok=True)
        return tempfile.mkdtemp(prefix="av-", dir=self._downloads)

    def _say_failed(self, name: str, reason: str, user: User) -> None:
        # The reason on its own line, so a command in it stays last on a line.
        text = self.translator.translate("Could not download {name}.").format(name=name)
        if reason:
            text += "\n" + reason
        self.ttclient.send_message(text, user)

    def _say_failed_all(self, name: str, reason: str, user: User) -> None:
        text = self.translator.translate("Could not download any season of {name}.").format(name=name)
        if reason:
            text += "\n" + reason
        self.ttclient.send_message(text, user)


__all__ = ["AudioVaultJobs"]
