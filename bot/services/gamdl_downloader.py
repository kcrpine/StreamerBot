"""Downloading Apple Music to MP3, via the gamdl CLI.

Separate from Apple Music *playback*: that streams through the browser, this
writes files and uploads them to the channel. The two share nothing but a
service name.

Three decisions worth knowing:

**gamdl is invoked with an argument list, never a shell string.** Album and track
names arrive from user input and reach this code unfiltered, so a shell would be
an injection hole with a very short path from a TeamTalk message to arbitrary
commands.

**Anything with more than one track is zipped before upload.** Uploading forty
files individually into a TeamTalk channel produces forty separate arrival
announcements, which is unusable with a screen reader. One archive is one
announcement.

**Progress is two messages, not a percentage.** A self-updating counter is read
aloud in full on every tick, so the bot says it has started and says it has
finished, and nothing in between.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from bot import errors

logger = logging.getLogger(__name__)

# A long album at high quality on a small VPS. Bounded so a wedged download
# cannot occupy the worker forever.
DOWNLOAD_TIMEOUT_SECONDS = 1800

AUDIO_SUFFIXES = (".m4a", ".mp3", ".aac", ".flac", ".opus", ".wav")

APPLE_URL = re.compile(
    r"^https?://(?:music|beta\.music)\.apple\.com/[a-z]{2}/"
    r"(album|playlist|artist|song|music-video)/",
    re.IGNORECASE,
)


def is_apple_music_url(url: str) -> bool:
    return bool(APPLE_URL.match((url or "").strip()))


def url_kind(url: str) -> Optional[str]:
    """album, playlist, artist, song or music-video."""
    match = APPLE_URL.match((url or "").strip())
    if not match:
        return None
    kind = match.group(1).lower()
    # A link to a single song inside an album carries ?i=; without it the same
    # /album/ URL means the whole album, and the difference decides whether the
    # result is one file or an archive.
    if kind == "album" and "?i=" in url:
        return "song"
    return kind


# -- what is playing, as something gamdl can fetch --------------------------

def song_url(now_playing: Optional[Dict[str, Any]]) -> Optional[str]:
    """A link to the one song MusicKit is playing, or None.

    The bot's queue holds whatever was searched for, which may be a whole album;
    only MusicKit knows which song inside it is playing. Its catalog URL is used
    when it has one. A song added from the account's library carries a library
    id ("i.…") that gamdl cannot fetch, so its catalog id is used instead, in
    Apple's /song/<name>/<id> form. gamdl reads the id from the last segment and
    Apple ignores the name, but gamdl needs one there to find the id at all.
    """
    if not now_playing:
        return None
    url = (now_playing.get("url") or "").strip()
    if is_apple_music_url(url):
        return url
    catalog_id = str(now_playing.get("catalog_id") or "").strip()
    storefront = str(now_playing.get("storefront") or "").strip().lower()
    if not catalog_id.isdigit() or not re.fullmatch(r"[a-z]{2}", storefront):
        return None
    return f"https://music.apple.com/{storefront}/song/song/{catalog_id}"


def collection_url(
    queued_url: str, now_playing: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[str], Optional[str]]:
    """The album or playlist being played. Returns (url, kind).

    What was queued decides it: an album or playlist is itself. A single song was
    queued as /album/<album>?i=<song>, so dropping the query gives the album it
    is on, which is the useful answer to "download the album" while one song
    plays. Failing that, the playing song's own catalog URL has the same shape.
    An artist or a library link has no downloadable collection.
    """
    for candidate in (queued_url, (now_playing or {}).get("url") or ""):
        candidate = (candidate or "").strip()
        kind = url_kind(candidate)
        if kind in ("album", "playlist"):
            return candidate.split("?", 1)[0], kind
        if kind == "song" and "/album/" in candidate:
            return candidate.split("?", 1)[0], "album"
    return None, None


def netscape_cookies(cookies: List[Dict[str, Any]], now: Optional[float] = None) -> str:
    """Browser cookies, as the cookies.txt file gamdl reads.

    Taken from the bot's own signed-in Apple Music browser profile, so the
    account connected with li am is the account that downloads, and nobody has
    to export a file by hand. Only apple.com cookies are written.

    A session cookie has no expiry, and Python's MozillaCookieJar drops a cookie
    whose expiry is empty or zero unless told otherwise. The file lives only as
    long as one download, so session cookies are given a day instead.
    """
    import time as _time

    horizon = int((now if now is not None else _time.time()) + 86400)
    lines = ["# Netscape HTTP Cookie File", ""]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        name = str(cookie.get("name") or "")
        if not name or not domain.lstrip(".").endswith("apple.com"):
            continue
        value = str(cookie.get("value") or "")
        # A tab or newline would split the line into different fields.
        if any(ch in name + value + domain for ch in "\t\r\n"):
            continue
        expires = cookie.get("expires")
        expires = int(expires) if isinstance(expires, (int, float)) and expires > 0 else horizon
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(cookie.get("path") or "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(expires),
            name,
            value,
        ]))
    return "\n".join(lines) + "\n"


def has_media_user_token(cookies: List[Dict[str, Any]]) -> bool:
    """Apple's signed-in subscription cookie; without it gamdl cannot download."""
    return any(
        c.get("name") == "media-user-token" and c.get("value") for c in cookies
    )


class GamdlDownloader:
    """Wraps the gamdl command line."""

    def __init__(self, cookies_path: str, output_dir: str, translator) -> None:
        self.cookies_path = cookies_path
        self.output_dir = output_dir
        self.translator = translator

    # -- availability ------------------------------------------------------

    @staticmethod
    def is_available() -> bool:
        return shutil.which("gamdl") is not None

    def has_cookies(self) -> bool:
        return bool(self.cookies_path) and os.path.isfile(self.cookies_path)

    def check_ready(self) -> None:
        """Raise with something a user can act on, rather than failing later."""
        if not self.is_available():
            raise errors.ServiceError(
                self.translator.translate(
                    "The Apple Music downloader is not installed in this image."
                )
            )
        if not self.has_cookies():
            raise errors.NotSignedInError(
                "am",
                self.translator.translate(
                    "Downloading from Apple Music needs a subscription. "
                    "Connect the account first with li am."
                ),
            )

    # -- downloading -------------------------------------------------------

    def _command(self, url: str, work_dir: str) -> List[str]:
        """Flags verified against gamdl 3.8.5 rather than guessed.

        "aac-web" is chosen deliberately over the higher-quality codecs: the
        others need a Widevine device file this image does not ship, and asking
        for one produces a decryption error rather than a lower-quality file.
        Better a track that plays than a quality setting that fails.
        """
        return [
            "gamdl",
            "--cookies-path", self.cookies_path,
            "--output-path", work_dir,
            "--song-codec-priority", "aac-web,aac",
            "--no-config-file",
            url,
        ]

    def download(self, url: str) -> Tuple[str, bool, int]:
        """Fetch a URL. Returns (path, is_archive, track count).

        A single track comes back as one MP3. Anything larger comes back as one
        zip, because uploading each file separately is unusable with a screen
        reader.
        """
        self.check_ready()
        if not is_apple_music_url(url):
            raise errors.ServiceError(
                self.translator.translate("That is not an Apple Music link.")
            )

        work_dir = tempfile.mkdtemp(prefix="gamdl-", dir=self.output_dir)
        try:
            result = subprocess.run(
                self._command(url, work_dir),
                capture_output=True,
                text=True,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
                # An argument list, never shell=True: these values come from
                # user input.
                shell=False,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise errors.ServiceError(
                self.translator.translate("The download took too long and was stopped.")
            )
        except OSError as error:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise errors.ServiceError(f"The downloader could not run: {error}") from error

        if result.returncode != 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise errors.ServiceError(self._explain_failure(result))

        files = self._collect_audio(work_dir)
        if not files:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise errors.ServiceError(
                self.translator.translate("The download produced no audio files.")
            )

        if len(files) == 1:
            path = os.path.join(self.output_dir, os.path.basename(files[0]))
            shutil.move(files[0], path)
            shutil.rmtree(work_dir, ignore_errors=True)
            return path, False, 1

        archive = self._zip(files, work_dir, url)
        shutil.rmtree(work_dir, ignore_errors=True)
        return archive, True, len(files)

    def _explain_failure(self, result) -> str:
        """Turn gamdl's output into one sentence a user can act on.

        Never include the raw stderr: it can carry the cookie path and other
        details that do not belong in a channel.
        """
        text = ((result.stderr or "") + (result.stdout or "")).lower()
        if "subscription" in text or "not active" in text:
            return self.translator.translate(
                "That needs an active Apple Music subscription."
            )
        if "not found" in text or "404" in text:
            return self.translator.translate("Apple Music did not have that.")
        if "widevine" in text or "device" in text and "missing" in text:
            return self.translator.translate(
                "That track needs a decryption device file the bot does not have."
            )
        if "cookie" in text:
            return self.translator.translate(
                "The Apple Music sign-in has expired. Connect it again with li am."
            )
        logger.error(f"[gamdl] failed: {(result.stderr or '')[:800]}")
        return self.translator.translate("The download failed.")

    @staticmethod
    def _collect_audio(work_dir: str) -> List[str]:
        found = []
        for root, _dirs, names in os.walk(work_dir):
            for name in sorted(names):
                if name.lower().endswith(AUDIO_SUFFIXES):
                    found.append(os.path.join(root, name))
        return found

    def _zip(self, files: List[str], work_dir: str, url: str) -> str:
        """One archive, named after the folder gamdl created.

        Paths inside the zip are relative, so extracting does not scatter files
        across the filesystem or leak the server's directory layout.
        """
        name = self._archive_name(files, work_dir, url)
        archive = os.path.join(self.output_dir, f"{name}.zip")
        counter = 1
        while os.path.exists(archive):
            archive = os.path.join(self.output_dir, f"{name} ({counter}).zip")
            counter += 1

        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, arcname=os.path.relpath(path, work_dir))
        return archive

    @staticmethod
    def _archive_name(files: List[str], work_dir: str, url: str) -> str:
        relative = os.path.relpath(files[0], work_dir)
        parts = [p for p in relative.split(os.sep) if p]
        name = parts[0] if len(parts) > 1 else (url_kind(url) or "apple-music")
        if name.lower().endswith(AUDIO_SUFFIXES):
            name = os.path.splitext(name)[0]
        # Keep it to something every filesystem and TeamTalk client accepts.
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
        return cleaned[:120] or "apple-music"


__all__ = [
    "GamdlDownloader",
    "collection_url",
    "has_media_user_token",
    "is_apple_music_url",
    "netscape_cookies",
    "song_url",
    "url_kind",
]
