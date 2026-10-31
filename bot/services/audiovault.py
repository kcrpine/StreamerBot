"""AudioVault (audiovault.net): audio-description tracks for films and TV.

AudioVault hosts no video: each title is the film's or episode's soundtrack with
the description mixed in, which is exactly what a blind audience streams into a
channel. The site is plain HTML, so this is plain HTTP, with no browser.

What was measured against the live site (2026-10-31), signed out:

- Search is `GET /movies?search=Q` and `GET /shows?search=Q`. Each answers a
  table whose rows are ID, name, and a link to `/download/{ID}`. No paging.
- A show is not one entry. Every season is its own row and its own download,
  "Family Guy - Season 01 (1999)", so results are grouped by show here.
- Signing in is a Laravel form: `GET /login` for the `_token` CSRF field, then
  `POST /login` with `email`, `password` and `remember`. A wrong password is
  sent back to `/login`.
- `/download/{ID}` redirects to `/login` when signed out.

**Not measured: what a download is when signed in.** It may be one audio file
or a zip of them, so `unpack` accepts either and refuses anything else by name.
A zip is checked before anything is written: no absolute paths, no `..`, and a
cap on its unpacked size, because the archive comes from the internet.
"""

from __future__ import annotations

import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests

from bot import errors
from bot.player.enums import TrackType
from bot.player.track import Track
from bot.services import Service

logger = logging.getLogger(__name__)

BASE_URL = "https://audiovault.net"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
TIMEOUT = 30
# A whole season can be gigabytes; the read timeout is per chunk, not in total.
DOWNLOAD_TIMEOUT = (30, 120)
CHUNK = 1024 * 1024

AUDIO_SUFFIXES = (".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma")

# "Family Guy - Season 01 (1999)" and "Doctor Who - Season 3 (2007) [TTS]".
SEASON = re.compile(r"^(?P<show>.+?)\s+-\s+Season\s+(?P<number>\d+)\b(?P<rest>.*)$", re.I)
# "Family Guy S09E09 Road to the North Pole Pt 2": one episode listed on its own.
EPISODE = re.compile(r"^(?P<show>.+?)\s+S(?P<season>\d+)\s*E(?P<episode>\d+)\b(?P<rest>.*)$", re.I)
TAG = re.compile(r"\[([^\]]+)\]")


@dataclass
class Entry:
    """One downloadable row on AudioVault: a film, or one season of a show."""

    id: int
    name: str


@dataclass
class Season:
    number: int
    label: str  # "Season 1 (1999)", with any tag spoken plainly
    entry: Entry


@dataclass
class Title:
    """A search result as the user hears it: a film, or a show with its seasons."""

    kind: str  # "movie" or "show"
    name: str
    entry: Optional[Entry] = None
    seasons: List[Season] = field(default_factory=list)


# -- reading the site's tables ----------------------------------------------

class _TableParser(HTMLParser):
    """Rows of (cell texts, links) from every table on a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[Tuple[List[str], List[str]]] = []
        self._cells: Optional[List[str]] = None
        self._links: List[str] = []
        self._text: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._cells, self._links = [], []
        elif tag in ("td", "th") and self._cells is not None:
            self._text = []
        elif tag == "a" and self._cells is not None:
            href = dict(attrs).get("href")
            if href:
                self._links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._text is not None and self._cells is not None:
            self._cells.append(" ".join("".join(self._text).split()))
            self._text = None
        elif tag == "tr" and self._cells is not None:
            self.rows.append((self._cells, self._links))
            self._cells = None

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)


DOWNLOAD_LINK = re.compile(r"/download/(\d+)/?$")


def parse_listing(html: str) -> List[Entry]:
    """The downloadable entries in one /movies or /shows page."""
    parser = _TableParser()
    parser.feed(html)
    entries = []
    for cells, links in parser.rows:
        entry_id = None
        for link in links:
            match = DOWNLOAD_LINK.search(urlparse(link).path)
            if match:
                entry_id = int(match.group(1))
                break
        if entry_id is None or len(cells) < 2:
            continue
        name = cells[1].strip()
        if name:
            entries.append(Entry(entry_id, name))
    return entries


def speakable(name: str, translate: Callable[[str], str]) -> str:
    """A name as a screen reader should hear it: tags out of their brackets.

    Brackets are read aloud or dropped depending on punctuation settings, and
    "TTS" means nothing in Spanish or Turkish, so the one tag with a meaning
    worth translating is translated and the rest are kept as plain words.
    """
    tags = [t.strip() for t in TAG.findall(name)]
    base = " ".join(TAG.sub(" ", name).split())
    words = []
    for tag in tags:
        if tag.upper() == "TTS":
            words.append(translate("text-to-speech narration"))
        elif tag:
            words.append(tag)
    return ", ".join([base] + words) if words else base


def group_titles(
    movies: List[Entry], shows: List[Entry], translate: Callable[[str], str]
) -> List[Title]:
    """Films as they are, and each show's seasons gathered under the show."""
    titles: List[Title] = [
        Title("movie", speakable(entry.name, translate), entry=entry) for entry in movies
    ]
    by_show: Dict[str, Title] = {}
    for entry in shows:
        match = SEASON.match(entry.name)
        if match:
            show = " ".join(match.group("show").split())
            number = int(match.group("number"))
            rest = speakable(match.group("rest"), translate).strip(" ,")
            label = translate("Season {number}").format(number=number)
            if rest:
                label = f"{label} {rest}"
        elif EPISODE.match(entry.name):
            # A single episode, listed as "Family Guy S09E09 Road to the North Pole Pt 2".
            match = EPISODE.match(entry.name)
            show = " ".join(match.group("show").split())
            number = int(match.group("season"))
            label = translate("Season {season}, episode {episode}").format(
                season=number, episode=int(match.group("episode"))
            )
            rest = speakable(match.group("rest"), translate).strip(" ,-")
            if rest:
                label = f"{label}: {rest}"
        else:
            # Listed with no season at all: its own title, played as it is.
            show, number, label = entry.name, 1, speakable(entry.name, translate)
        key = show.lower()
        if key not in by_show:
            by_show[key] = Title("show", speakable(show, translate))
            titles.append(by_show[key])
        by_show[key].seasons.append(Season(number, label, entry))
    for title in by_show.values():
        title.seasons.sort(key=lambda s: (s.number, s.entry.id))
    return titles


# -- the archive --------------------------------------------------------------

def _natural(name: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def unpack(path: str, dest: Optional[str], max_bytes: int, translate: Callable[[str], str]) -> List[str]:
    """The audio files in what AudioVault sent, in episode order.

    A single audio file is returned as it is. A zip is checked whole before
    anything is written, then only its audio files are extracted, flattened
    into dest under their own names. With dest None it is only checked.
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = []
            total = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
                    raise errors.ServiceError(
                        translate("AudioVault sent a zip file that is not safe to open.")
                    )
                if not name.lower().endswith(AUDIO_SUFFIXES):
                    continue
                total += info.file_size
                members.append(info)
            if total > max_bytes:
                raise errors.ServiceError(
                    translate("The download is larger than this bot allows.")
                )
            if not members:
                raise errors.ServiceError(
                    translate("AudioVault sent a zip file with no audio in it.")
                )
            if dest is None:
                # Checked only: it is going to be uploaded, not played.
                return [info.filename for info in members]
            os.makedirs(dest, exist_ok=True)
            files = []
            for info in sorted(members, key=lambda i: _natural(i.filename)):
                base = safe_name(os.path.basename(info.filename.replace("\\", "/")), "audio")
                target = _free_path(os.path.join(dest, base))
                with archive.open(info) as source, open(target, "wb") as out:
                    written = 0
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        written += len(chunk)
                        if written > info.file_size + CHUNK:
                            # A header that lies about its size.
                            raise errors.ServiceError(
                                translate("AudioVault sent a zip file that is not safe to open.")
                            )
                        out.write(chunk)
                files.append(target)
            return files
    if path.lower().endswith(AUDIO_SUFFIXES):
        return [path]
    raise errors.ServiceError(
        translate("AudioVault sent something that is not audio or a zip file.")
    )


def safe_name(name: str, fallback: str = "audiovault") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(". ")
    return cleaned[:150] or fallback


def _free_path(path: str) -> str:
    stem, suffix = os.path.splitext(path)
    counter = 1
    while os.path.exists(path):
        path = f"{stem} ({counter}){suffix}"
        counter += 1
    return path


def as_zip(path: str, name: str, dest_dir: str) -> str:
    """What gets uploaded: AudioVault's own zip renamed, or one file zipped."""
    archive = _free_path(os.path.join(dest_dir, safe_name(name) + ".zip"))
    if zipfile.is_zipfile(path):
        os.replace(path, archive)
        return archive
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
        # Audio is already compressed; deflating it again only costs time.
        zf.write(path, arcname=safe_name(os.path.basename(path)))
    return archive


# -- talking to the site -----------------------------------------------------

_CSRF = re.compile(r'name="_token"\s+value="([^"]+)"')
_FILENAME_STAR = re.compile(r"filename\*\s*=\s*[^']*''([^;]+)", re.I)
_FILENAME = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.I)
_CONTENT_SUFFIX = {
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


class AudioVaultClient:
    """One bot's AudioVault session, kept in that bot's own data directory."""

    def __init__(
        self,
        session_path: str,
        credentials: Callable[[], Tuple[str, str]],
        translate: Callable[[str], str],
        base_url: str = BASE_URL,
    ) -> None:
        self.session_path = session_path
        self.credentials = credentials
        self.translate = translate
        self.base_url = base_url.rstrip("/")
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT
        self._load()

    # -- the session file ----------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.session_path, encoding="utf-8") as f:
                for cookie in json.load(f):
                    self.http.cookies.set(
                        cookie["name"], cookie["value"],
                        domain=cookie.get("domain", ""), path=cookie.get("path", "/"),
                    )
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save(self) -> None:
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.http.cookies
        ]
        os.makedirs(os.path.dirname(self.session_path), mode=0o700, exist_ok=True)
        fd = os.open(self.session_path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cookies, f)
        os.replace(self.session_path + ".tmp", self.session_path)

    def forget(self) -> None:
        self.http.cookies.clear()
        try:
            os.remove(self.session_path)
        except OSError:
            pass

    # -- signing in ------------------------------------------------------------

    @staticmethod
    def _is_login_page(response) -> bool:
        return urlparse(response.url).path.rstrip("/").endswith("/login")

    def sign_in(self, email: str, password: str) -> None:
        """Sign in, or raise with a sentence a user can act on."""
        try:
            page = self.http.get(f"{self.base_url}/login", timeout=TIMEOUT)
            token = _CSRF.search(page.text or "")
            if not token:
                raise errors.ServiceError(
                    self.translate("AudioVault's sign-in page did not load. Try again later.")
                )
            response = self.http.post(
                f"{self.base_url}/login",
                data={"_token": token.group(1), "email": email, "password": password,
                      "remember": "on"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            logger.warning(f"[av] sign-in request failed: {error}")
            raise errors.ServiceError(
                self.translate("AudioVault could not be reached. Try again later.")
            ) from error
        if self._is_login_page(response) or response.status_code >= 400:
            raise errors.ServiceError(
                self.translate("AudioVault did not accept that email address and password.")
            )
        self._save()

    def _sign_in_again(self) -> None:
        email, password = self.credentials()
        if not email or not password:
            raise errors.NotSignedInError(
                "av",
                self.translate("To use AudioVault, connect an account. To connect one, send: li av"),
            )
        try:
            self.sign_in(email, password)
        except errors.ServiceError as error:
            raise errors.NotSignedInError(
                "av",
                self.translate(
                    "AudioVault did not accept the sign-in. To connect again, send: li av"
                ),
            ) from error

    # -- searching -------------------------------------------------------------

    def search(self, query: str) -> List[Title]:
        found = {}
        for listing in ("movies", "shows"):
            try:
                response = self.http.get(
                    f"{self.base_url}/{listing}", params={"search": query}, timeout=TIMEOUT
                )
                response.raise_for_status()
            except requests.RequestException as error:
                logger.warning(f"[av] {listing} search failed: {error}")
                raise errors.ServiceError(
                    self.translate("AudioVault could not be reached. Try again later.")
                ) from error
            found[listing] = parse_listing(response.text)
        return group_titles(found["movies"], found["shows"], self.translate)

    # -- downloading -----------------------------------------------------------

    def fetch(self, entry: Entry, dest_dir: str, max_bytes: int) -> str:
        """Download one entry into dest_dir and return the file's path."""
        response = self._open_download(entry)
        if self._is_login_page(response):
            response.close()
            self._sign_in_again()
            response = self._open_download(entry)
            if self._is_login_page(response):
                response.close()
                raise errors.NotSignedInError(
                    "av",
                    self.translate(
                        "AudioVault did not accept the sign-in. To connect again, send: li av"
                    ),
                )
        with response:
            if response.status_code >= 400:
                raise errors.ServiceError(
                    self.translate("AudioVault did not have that download.")
                )
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type.startswith("text/html"):
                raise errors.ServiceError(
                    self.translate("AudioVault sent something that is not audio or a zip file.")
                )
            length = int(response.headers.get("Content-Length") or 0)
            if length > max_bytes:
                raise errors.ServiceError(
                    self.translate("The download is larger than this bot allows.")
                )
            name = self._filename(response, entry, content_type)
            os.makedirs(dest_dir, exist_ok=True)
            path = _free_path(os.path.join(dest_dir, name))
            written = 0
            with open(path, "wb") as out:
                for chunk in response.iter_content(CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        out.close()
                        os.remove(path)
                        raise errors.ServiceError(
                            self.translate("The download is larger than this bot allows.")
                        )
                    out.write(chunk)
        # Logged because what AudioVault sends was never measured signed in.
        logger.info(
            f"[av] downloaded entry {entry.id}: type={content_type or 'unknown'} "
            f"bytes={written} name={os.path.basename(path)!r}"
        )
        return path

    def _open_download(self, entry: Entry):
        try:
            return self.http.get(
                f"{self.base_url}/download/{entry.id}", stream=True,
                timeout=DOWNLOAD_TIMEOUT, allow_redirects=True,
            )
        except requests.RequestException as error:
            logger.warning(f"[av] download {entry.id} failed: {error}")
            raise errors.ServiceError(
                self.translate("AudioVault could not be reached. Try again later.")
            ) from error

    @staticmethod
    def _filename(response, entry: Entry, content_type: str) -> str:
        disposition = response.headers.get("Content-Disposition") or ""
        match = _FILENAME_STAR.search(disposition)
        name = unquote(match.group(1)) if match else ""
        if not name:
            match = _FILENAME.search(disposition)
            name = match.group(1).strip() if match else ""
        if not name:
            name = os.path.basename(unquote(urlparse(response.url).path))
        name = safe_name(os.path.basename(name.replace("\\", "/")), safe_name(entry.name))
        if not os.path.splitext(name)[1]:
            name += _CONTENT_SUFFIX.get(content_type, "")
        return name


# -- the service ---------------------------------------------------------------

def local_track(path: str, name: str) -> Track:
    return Track(service="av", url=path, name=name, type=TrackType.Local)


class AudioVaultService(Service):
    """Registered as "av". Hidden from `sv`: like podcasts it has its own command,
    because a show is many seasons and a bare `p` could not say which one."""

    name = "av"
    hidden = True
    hostnames: List[str] = []
    requires_auth = True

    def __init__(self, bot: Any, config: Any) -> None:
        self.bot = bot
        self.config = config
        self.translator = bot.translator
        self.is_enabled = getattr(config, "enabled", True)
        self.error_message = ""
        self.warning_message = ""
        self.store = None
        self._client: Optional[AudioVaultClient] = None

    @property
    def help(self) -> str:
        return self.translator.translate(
            "AudioVault has its own command. To search it, send av and a title, for example: av spaceballs"
        )

    @property
    def max_bytes(self) -> int:
        return int(getattr(self.config, "max_download_mb", 8192)) * 1024 * 1024

    def attach_store(self, store) -> None:
        """The encrypted credential store, given once the portal has opened it."""
        self.store = store

    def credentials(self) -> Tuple[str, str]:
        if self.store is None:
            return "", ""
        return self.store.get("av", "username") or "", self.store.get("av", "password") or ""

    def is_connected(self) -> bool:
        return self.store is not None and self.store.has("av")

    @property
    def client(self) -> AudioVaultClient:
        if self._client is None:
            data_dir = self.bot.config_manager.config_dir
            self._client = AudioVaultClient(
                os.path.join(data_dir, "audiovault", "session.json"),
                self.credentials, self.translator.translate,
            )
        return self._client

    def sign_in(self, email: str, password: str) -> None:
        self.client.sign_in(email, password)

    def sign_out(self) -> None:
        self.client.forget()

    def initialize(self) -> None:
        pass

    def get(self, url: str, extra_info: Optional[Dict[str, Any]] = None, process: bool = False) -> List[Track]:
        raise errors.ServiceError(
            self.translator.translate(
                "AudioVault has its own command. To search it, send av and a title, for example: av spaceballs"
            )
        )

    def search(self, query: str, limit: Optional[int] = None) -> List[Track]:
        raise errors.NothingFoundError("")


__all__ = [
    "AudioVaultClient",
    "AudioVaultService",
    "Entry",
    "Season",
    "Title",
    "as_zip",
    "group_titles",
    "local_track",
    "parse_listing",
    "speakable",
    "unpack",
]
