"""Downloading from Apple Music: `dl` for the song playing, `dlp` for the album or
playlist playing, `dla` for every album by an artist (nothing need be playing).

Apple Music plays in the browser, so the bot's queued Track is not a file and
the ordinary uploader cannot fetch it. gamdl can, as the same account, using the
cookies of the bot's own signed-in browser profile. The target is resolved while
the user waits, so the first message names what is being downloaded; the
download and upload then run on their own thread, because an album can take many
minutes and must not hold up other commands.

One download at a time per bot. gamdl decrypting a long album is heavy on a small
VPS, and a second request would slow both rather than finish sooner.

Everything, cookies included, lives in one directory under the bot's data
directory and is removed when the job ends, however it ends.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
from urllib.parse import unquote
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from bot import errors
from bot.commands.format_choice import FormatChoice
from bot.services.gamdl_downloader import (
    GamdlDownloader,
    format_name,
    artist_url,
    collection_url,
    has_media_user_token,
    is_apple_music_url,
    netscape_cookies,
    song_url,
)

if TYPE_CHECKING:
    from bot import Bot
    from bot.modules.uploader import Uploader
    from bot.player.track import Track
    from bot.TeamTalk.structs import User

logger = logging.getLogger(__name__)


def _norm(text: str) -> str:
    """Letters and digits only, case folded: "Celtic  thunder!" == "celticthunder"."""
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


def _slug_name(url: str) -> str:
    """The readable name in an artist link: .../artist/celtic-thunder/123 -> "celtic thunder"."""
    parts = url.rstrip("/").split("/")
    return unquote(parts[-2]).replace("-", " ").strip() if len(parts) >= 2 else ""


_ARTIST_SEPARATORS = re.compile(r"\s*(?:&|,|\bfeat\.?|\bft\.?|\bwith\b|\bx\b)\s*", re.IGNORECASE)
_BY = re.compile(r"\bby\s+(.+)$", re.IGNORECASE)


def artist_from_results(query: str, tracks) -> Optional[Tuple[str, str]]:
    """(artist link, artist name) taken from the songs and albums in a search.

    Prefers a result whose artist is the one asked for, by the whole query or
    by the part after "by"; otherwise the first result that links an artist.
    The name comes from the result rather than the link where it can, so it is
    spoken as Apple writes it ("AC/DC", not "Ac Dc").
    """
    candidates = []
    for track in tracks or []:
        extra = getattr(track, "extra_info", None) or {}
        url = artist_url(extra.get("artist_url", "") or "")
        if not url:
            continue
        slug = _slug_name(url)
        parts = [p for p in _ARTIST_SEPARATORS.split(extra.get("artist", "") or "") if p.strip()]
        name = next((p for p in parts if _norm(p) == _norm(slug)), None)
        if name is None:
            name = parts[0] if len(parts) == 1 else slug.title()
        candidates.append((url, name.strip(), _norm(slug)))
    if not candidates:
        return None

    wanted = [_norm(query)]
    by = _BY.search(query or "")
    if by:
        wanted.append(_norm(by.group(1)))
    for target in wanted:
        if not target:
            continue
        for url, name, slug in candidates:
            if target in (_norm(name), slug):
                return url, name
    url, name, _ = candidates[0]
    return url, name


class AppleMusicDownloader:
    def __init__(self, bot: Bot, uploader: Uploader) -> None:
        self.bot = bot
        self.translator = bot.translator
        self.ttclient = bot.ttclient
        self.uploader = uploader
        self._busy = threading.Lock()

    @property
    def _root(self) -> str:
        return os.path.join(self.bot.config_manager.config_dir, "downloads")

    def _service(self):
        return self.bot.service_manager.services.get("am")

    # -- choosing what to download ----------------------------------------

    @staticmethod
    def describe(now_playing: Optional[Dict[str, Any]]) -> str:
        title = ((now_playing or {}).get("title") or "").strip()
        artist = ((now_playing or {}).get("artist") or "").strip()
        return f"{title}, by {artist}" if title and artist else title

    def _resolve(self, track: Track, whole: bool) -> Tuple[Optional[str], str, str, str]:
        """What to download, settled while it is still playing.

        Returns (url, name, kind, hint). url None means "say name and stop":
        name is then the refusal. kind is "song", "album" or "playlist"; hint is
        extra advice appended to the start message.
        """
        service = self._service()
        if service is None or not hasattr(service, "now_playing"):
            return None, self.translator.translate("Apple Music is not available on this bot."), "", ""

        try:
            now_playing = service.now_playing()
        except errors.ServiceError as error:
            return None, str(error), "", ""
        except Exception as error:
            logger.warning(f"[am download] could not ask Apple Music what is playing: {error}")
            now_playing = None

        album = ((now_playing or {}).get("album") or "").strip()
        if whole:
            url, kind = collection_url(track.url, now_playing)
            if not url:
                return None, self.translator.translate(
                    "What is playing is not an album or playlist that can be downloaded."
                ), "", ""
            if kind == "playlist":
                name = (track.name or "").strip() or self.translator.translate("the playlist")
            else:
                name = album or (track.name or "").strip()
            return url, name, kind, ""

        if not now_playing:
            return None, self.translator.translate(
                "Apple Music is not playing a song right now."
            ), "", ""
        url = song_url(now_playing)
        if not url:
            return None, self.translator.translate(
                "This song is not in the Apple Music catalog, so it cannot be downloaded."
            ), "", ""
        hint = ""
        if collection_url(track.url)[1]:
            hint = self.translator.translate(
                "To download the whole album or playlist instead, send: dlp"
            )
        return url, self.describe(now_playing), "song", hint

    def _started(self, name: str, kind: str, fmt: str, hint: str) -> str:
        label = format_name(fmt)
        if kind == "playlist":
            started = self.translator.translate(
                "Downloading the playlist {name} as {format}. It will be uploaded to the "
                "channel's files as one zip file. This can take several minutes."
            ).format(name=name, format=label)
        elif kind == "album":
            started = self.translator.translate(
                "Downloading the album {name} as {format}. It will be uploaded to the "
                "channel's files as one zip file. This can take several minutes."
            ).format(name=name, format=label)
        else:
            started = self.translator.translate(
                "Downloading {name} as {format}. It will be uploaded to the channel's "
                "files when it is ready."
            ).format(name=name, format=label)
        return f"{started} {hint}" if hint else started

    def start(self, track: Track, user: User, whole: bool, fmt: str = "m4a") -> str:
        """Begin downloading, and return the one message to send now.

        whole=False is the song playing; whole=True is the album or playlist that
        was played, or the album a single song is on.
        """
        url, name, kind, hint = self._resolve(track, whole)
        if not url:
            return name
        return self._launch(
            self._run, (url, name, user, fmt),
            self._started(name, kind, fmt, hint), "dlp" if whole else "dl",
        )

    def ask_format(self, track: Track, user: User, whole: bool, register) -> str:
        """Settle what to download now, then ask which file format.

        register stores the question for the user's next message. A refusal, or
        a download already running, is said instead of asking, so nobody answers
        a question only to be refused.
        """
        command = "dlp" if whole else "dl"
        url, name, kind, hint = self._resolve(track, whole)
        if not url:
            return name
        if self._busy.locked():
            return self._already_running(command)

        def start(fmt: str) -> str:
            return self._launch(
                self._run, (url, name, user, fmt), self._started(name, kind, fmt, hint), command
            )

        choice = FormatChoice(command, self.translator.translate, start)
        register(choice)
        return choice.question()

    def _already_running(self, command: str) -> str:
        # Names the command to send again. "Send this again" pointed at nothing
        # once dla's question had closed, and a command mid-sentence is harder
        # to find with the review cursor than one last after a colon.
        return self.translator.translate(
            "An Apple Music download is already running. To try again when it "
            "has finished, send: {command}"
        ).format(command=command)

    def _launch(self, target, args, started: str, command: str) -> str:
        if not self._busy.acquire(blocking=False):
            return self._already_running(command)
        try:
            threading.Thread(
                target=target, args=args,
                daemon=True, name="AppleMusicDownload",
            ).start()
        except Exception:
            self._busy.release()
            raise
        return started

    # -- a whole artist (dla) ---------------------------------------------

    def find_artist(self, query: str) -> Tuple[str, str]:
        """(artist link, artist name) for a link or a name. Nothing need be playing.

        A name is searched on Apple Music and the first artist result is taken:
        Apple ranks its own results, and the name goes into dla's question, so a
        wrong match is heard and cancelled before anything downloads.

        Apple shows an Artist card for only some searches: "Celtic Thunder" and
        "Elvis Presley" both came back as songs and albums alone, so dla said
        no artist was found while `p` played them. The songs and albums still
        link their artist, so with no Artist card the artist of a result is
        used instead: one whose name matches what was asked first (including
        the part after "by" in "My Gift by Carrie Underwood"), then the top
        result's own artist.
        """
        query = (query or "").strip()
        link = artist_url(query)
        if link:
            # The link's own slug is a readable name until Apple's is known.
            slug = link.rstrip("/").split("/")[-2] if link.count("/") >= 6 else ""
            # A name outside Latin letters arrives percent-encoded, which a
            # screen reader would read out as "percent E5".
            slug = unquote(slug)
            name = slug.replace("-", " ").strip().title()
            return link, name or self.translator.translate("this artist")
        if is_apple_music_url(query):
            raise errors.ServiceError(
                self.translator.translate(
                    "That is an Apple Music link, but not to an artist. For an album "
                    "or playlist, send: dlp"
                )
            )

        service = self._service()
        if service is None or not hasattr(service, "now_playing"):
            raise errors.ServiceError(
                self.translator.translate("Apple Music is not available on this bot.")
            )
        try:
            tracks = service.search(query)
        except errors.NothingFoundError:
            tracks = []
        for track in tracks:
            kind = (getattr(track, "extra_info", None) or {}).get("kind")
            url = artist_url(getattr(track, "url", "") or "")
            if kind == "artist" and url:
                return url, (track.name or "").strip() or query

        found = artist_from_results(query, tracks)
        if found:
            logger.info(
                f"[am download] dla {query!r}: no Artist card, so using the artist "
                f"of a song or album: {found[1]!r}"
            )
            return found
        logger.warning(
            f"[am download] dla {query!r}: no artist in {len(tracks)} search results "
            f"(kinds: {', '.join(sorted({str((getattr(t, 'extra_info', None) or {}).get('kind')) for t in tracks})) or 'none'})"
        )
        raise errors.ServiceError(
            self.translator.translate("Apple Music found no artist called {name}.").format(
                name=query
            )
        )

    def busy_message(self) -> Optional[str]:
        """Said before dla asks its question, so nobody answers only to be refused."""
        if self._busy.locked():
            return self._already_running("dla")
        return None

    def ask_artist_format(self, url: str, name: str, selection: str, user: User, register) -> str:
        """dla's second question: the file format, once the releases are chosen."""
        def start(fmt: str) -> str:
            return self.start_artist(url, name, selection, user, fmt)

        choice = FormatChoice("dla", self.translator.translate, start)
        register(choice)
        return choice.question()

    def start_artist(
        self, url: str, name: str, selection: str, user: User, fmt: str = "m4a"
    ) -> str:
        """Begin downloading an artist's albums, and return the one message to send now."""
        what = {
            "main-albums": self.translator.translate(
                "Downloading the main albums by {name}."
            ),
            "all-albums": self.translator.translate(
                "Downloading every album by {name}, including singles, EPs, live "
                "albums and compilations."
            ),
            "singles-eps": self.translator.translate(
                "Downloading the singles and EPs by {name}."
            ),
        }[selection].format(name=name)
        started = what + " " + self.translator.translate(
            "They will be uploaded to the channel's files as {format} files in one zip "
            "file, with a folder for each album. This can take a long time."
        ).format(format=format_name(fmt))
        return self._launch(self._run_artist, (url, name, selection, user, fmt), started, "dla")

    # -- doing it ---------------------------------------------------------

    def _unconverted(self, downloader: GamdlDownloader, fmt: str) -> str:
        if not getattr(downloader, "unconverted", 0):
            return ""
        return "\n" + self.translator.translate(
            "Some tracks could not be converted to {format}, so they are M4A files instead."
        ).format(format=format_name(fmt))

    def _run(self, url: str, name: str, user: User, fmt: str = "m4a") -> None:
        def fetch(downloader: GamdlDownloader) -> Optional[str]:
            path, is_archive, count = downloader.download(url, fmt)
            if not self.uploader.upload_file(path, user):
                return None
            if is_archive:
                message = self.translator.translate(
                    "{name} is in the channel: one zip file with {count} tracks."
                ).format(name=name, count=count)
            else:
                message = self.translator.translate("{name} is in the channel.").format(name=name)
            return message + self._unconverted(downloader, fmt)

        self._job(url, name, user, fetch)

    def _run_artist(
        self, url: str, name: str, selection: str, user: User, fmt: str = "m4a"
    ) -> None:
        def fetch(downloader: GamdlDownloader) -> Optional[str]:
            path, albums, tracks, partial = downloader.download_artist(url, selection, name, fmt)
            if not self.uploader.upload_file(path, user):
                return None
            # "Coldplay is in the channel" would mean a user called Coldplay.
            # One number per line, each last after a colon.
            message = "\n".join([
                self.translator.translate(
                    "The {name} download is in the channel's files, as one zip file."
                ).format(name=name),
                self.translator.translate("Albums: {albums}").format(albums=albums),
                self.translator.translate("Tracks: {count}").format(count=tracks),
            ])
            if partial:
                message += "\n" + self.translator.translate(
                    "Some tracks could not be downloaded."
                )
            return message + self._unconverted(downloader, fmt)

        self._job(url, name, user, fetch)

    def _job(self, url: str, name: str, user: User, fetch) -> None:
        """Set up a job directory and cookies, run fetch, and always clean up.

        fetch downloads and uploads, and returns the finish message, or None when
        the upload failed and the user has already been told why.
        """
        job_dir = None
        try:
            os.makedirs(self._root, mode=0o700, exist_ok=True)
            job_dir = tempfile.mkdtemp(prefix="am-", dir=self._root)
            output_dir = os.path.join(job_dir, "out")
            os.makedirs(output_dir, mode=0o700)
            cookies_path = self._write_cookies(job_dir)

            downloader = GamdlDownloader(cookies_path, output_dir, self.translator)
            message = fetch(downloader)
            if message:
                self.ttclient.send_message(message, user)
        except (errors.ServiceError, errors.NotSignedInError) as error:
            self.ttclient.send_message(
                self.translator.translate("Could not download {name}: {reason}").format(
                    name=name, reason=str(error)
                ),
                user,
            )
        except Exception as error:
            # Not passed to the channel: it may carry a path under data/.
            logger.error(f"[am download] failed for {url}: {error}", exc_info=True)
            self.ttclient.send_message(
                self.translator.translate("Could not download {name}.").format(name=name),
                user,
            )
        finally:
            if job_dir:
                shutil.rmtree(job_dir, ignore_errors=True)
            self._busy.release()

    def _write_cookies(self, job_dir: str) -> str:
        cookies = self._service().cookies()
        if not has_media_user_token(cookies):
            raise errors.NotSignedInError(
                "am",
                self.translator.translate(
                    "Apple Music is not signed in on this bot. To connect it, send: li am"
                ),
            )
        path = os.path.join(job_dir, "cookies.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(netscape_cookies(cookies))
        return path


__all__ = ["AppleMusicDownloader"]
