"""`dlp`: download every track of a playlist, album or channel, zip them, upload the zip.

Progress is spoken as each track starts: which track, how much of the playlist
is done, and its title. A screen reader reads every message in full, so a
playlist longer than PROGRESS_EVERY_TRACK_UP_TO tracks speaks only when it
crosses a 10 percent step, about ten lines whatever its size, rather than one
per track (users asked for progress; [069]). `dlp` with no argument answers the
same question on demand.

YouTube refuses a stale session with LOGIN_REQUIRED. Playback commands renew it
and retry; this job does the same through DownloadSignIn, once, rather than
failing every track for the same reason and saying only that nothing worked.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import zipfile
from typing import TYPE_CHECKING, Dict, List, Optional

from bot import utils
from bot.modules.youtube_session_keeper import (
    DownloadSignIn,
    RefreshResult,
    SignInLost,
    sign_in_lost_message,
)
from bot.player.enums import TrackType

if TYPE_CHECKING:
    from bot import Bot
    from bot.modules.uploader import Uploader
    from bot.player.track import Track
    from bot.TeamTalk.structs import User

YOUTUBE_SERVICES = ("yt", "ytm")

# Playlists up to this many tracks report every track; longer ones every 10 percent.
PROGRESS_EVERY_TRACK_UP_TO = 20


def progress_due(index: int, total: int) -> bool:
    """Whether starting track index (0-based) of total should send a progress message."""
    if total <= PROGRESS_EVERY_TRACK_UP_TO or index == 0:
        return True
    return (index * 100 // total) // 10 > ((index - 1) * 100 // total) // 10


class PlaylistUploader:
    def __init__(self, bot: Bot, uploader: Uploader):
        self.bot = bot
        self.config = bot.config
        self.ttclient = bot.ttclient
        self.translator = bot.translator
        self.uploader = uploader
        self.current_status: Dict[int, str] = {}

    def __call__(self, tracks: List[Track], user: User, playlist_name: str = "Playlist") -> None:
        threading.Thread(
            target=self.run,
            args=(tracks, user, playlist_name),
            daemon=True,
            name="PlaylistUploader",
        ).start()

    def get_status(self, user_id: int) -> Optional[str]:
        return self.current_status.get(user_id)

    def _keeper(self):
        return getattr(self.bot, "youtube_session", None)

    def run(self, tracks: List[Track], user: User, playlist_name: str) -> None:
        translate = self.translator.translate
        send = lambda text: self.ttclient.send_message(text, user)
        logging.info(f"PlaylistUploader started for {len(tracks)} tracks requested by {user.username}")

        youtube = any(track.service in YOUTUBE_SERVICES for track in tracks)
        keeper = self._keeper() if youtube else None
        if keeper is not None and keeper.store.needs_sign_in():
            send(sign_in_lost_message(translate, RefreshResult.Ended, playlist_name))
            return
        sign_in = DownloadSignIn(
            keeper,
            on_renewing=lambda: send(translate(
                "Renewing the YouTube sign-in. The download continues when it finishes."
            )),
        )

        send(translate(
            "Downloading the playlist {name}. Tracks: {count}. It will be uploaded to the "
            "channel as one zip file when it is ready."
        ).format(name=playlist_name, count=len(tracks)))

        user_id = user.id
        temp_dir = tempfile.TemporaryDirectory()
        try:
            downloaded: List[str] = []
            failed = 0
            for index, track in enumerate(tracks):
                # The title goes last: it is the longest and least predictable part.
                progress = translate("Track {number} of {total}, {percent} percent done: {title}").format(
                    number=index + 1,
                    total=len(tracks),
                    percent=index * 100 // len(tracks),
                    title=track.name,
                )
                self.current_status[user_id] = progress
                if progress_due(index, len(tracks)):
                    send(progress)
                try:
                    downloaded.append(self._download(track, temp_dir.name, sign_in))
                except SignInLost as lost:
                    logging.warning(f"PlaylistUploader: stopped at track {index + 1}, sign-in {lost.result.value}")
                    send(sign_in_lost_message(translate, lost.result, playlist_name))
                    return
                except Exception as error:
                    failed += 1
                    logging.error(f"PlaylistUploader: failed to download track {index + 1}: {error}")

            if not downloaded:
                send(translate(
                    "No track in {name} could be downloaded. Tracks tried: {count}."
                ).format(count=len(tracks), name=playlist_name))
                return

            self.current_status[user_id] = translate("Uploading {name} to the channel.").format(
                name=playlist_name
            )
            zip_path = self._zip(downloaded, playlist_name, temp_dir.name)
            if not self.uploader.upload_file(zip_path, user):
                return
            # Counts after a colon, so no language needs a plural form here.
            message = translate(
                "{name} is in the channel as one zip file. Tracks in it: {count}."
            ).format(name=playlist_name, count=len(downloaded))
            if failed:
                message += " " + translate("Tracks that could not be downloaded: {count}.").format(count=failed)
            send(message)
        except Exception as error:
            # Not passed to the channel: it may carry a path under data/.
            logging.error(f"PlaylistUploader error: {error}", exc_info=True)
            send(translate("Could not download {name}.").format(name=playlist_name))
        finally:
            self.current_status.pop(user_id, None)
            temp_dir.cleanup()

    def _download(self, track: Track, directory: str, sign_in: DownloadSignIn) -> str:
        if track.service in YOUTUBE_SERVICES:
            # The bridge's download plan resolves the stream itself from the video
            # ID. Resolving through track.url first cost a second request per
            # track, and its autoplay side effect queued recommendations on the
            # player.
            if not track.format:
                track.format = "mp3"
            return sign_in.run(lambda: track.download(directory))
        if track.type == TrackType.Dynamic:
            track.url  # fetch the stream data the download needs
        return track.download(directory)

    @staticmethod
    def _zip(files: List[str], playlist_name: str, directory: str) -> str:
        folder_name = utils.clean_file_name(playlist_name)
        zip_path = os.path.join(directory, folder_name + ".zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, os.path.join(folder_name, os.path.basename(path)))
        return zip_path


__all__ = ["PlaylistUploader", "YOUTUBE_SERVICES"]
