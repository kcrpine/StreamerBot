"""Podcasts: finding a show, reading its feed, and its episodes as tracks.

A podcast is an RSS feed whose items carry an audio enclosure. The enclosure is a
plain file on the publisher's server, so mpv plays it directly and a download is
an ordinary HTTP fetch; nothing here signs in to anything.

Search goes through public directories that need no key (Apple Podcasts, fyyd
and gpodder.net), one chosen by the user or all of them at once, and each
returns a show's feed URL. Everything after that reads the feed itself, so a
show that leaves a directory keeps working for anyone already subscribed to it.

Spotify is the exception. Its shows have no public feed: they are found and
listed through the Spotify Web API and played by the Spotify engine, as Spotify
songs are, and like Spotify songs they cannot be downloaded. A Spotify show is
identified by its `spotify:show:` URI wherever a feed URL would otherwise go.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests

from bot import errors
from bot.player.enums import TrackType
from bot.player.track import Track
from bot.services import Service

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger(__name__)

SEARCH_URL = "https://itunes.apple.com/search"
FYYD_URL = "https://api.fyyd.de/0.2/search/podcast"
GPODDER_URL = "https://gpodder.net/search.json"
USER_AGENT = "StreamerBot podcast reader"
TIMEOUT = 20
# Searching all directories waits this long and then lists what has arrived, so
# one slow directory does not hold up the other three.
ALL_TIMEOUT = 12
SPOTIFY_PREFIX = "spotify:show:"
# A Spotify show is read in pages of 50; this many is plenty to pick from and
# keeps a long-running show from taking a dozen requests to open.
SPOTIFY_EPISODE_LIMIT = 200

#: Directory codes, in the order "all" lists their results.
DIRECTORIES = ("apple", "fyyd", "gpodder", "spotify")
DIRECTORY_NAMES = {
    "apple": "Apple Podcasts",
    "fyyd": "fyyd",
    "gpodder": "gpodder.net",
    "spotify": "Spotify",
}
# A feed with a long back catalogue and full show notes runs to a few megabytes.
# Far past that is not a feed, and reading it whole would only exhaust memory.
MAX_FEED_BYTES = 30 * 1024 * 1024

AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "ogg", "oga", "opus", "wav", "flac", "mp4"}
MIME_EXTENSIONS = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/x-m4a": "m4a",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "video/mp4": "mp4",
}


@dataclass
class Podcast:
    title: str
    feed_url: str
    author: str = ""
    #: Which directory found it, one of DIRECTORIES.
    source: str = ""


@dataclass
class Episode:
    title: str
    url: str
    guid: str
    published: float = 0.0
    extension: str = "mp3"


@dataclass
class Feed:
    title: str
    feed_url: str
    episodes: List[Episode] = field(default_factory=list)

    @property
    def is_spotify(self) -> bool:
        return self.feed_url.startswith(SPOTIFY_PREFIX)


def clean_feed_url(url: str) -> str:
    """Drop empty query parameters, which some directories append (`?format=`)."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if v])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def feed_key(url: str) -> str:
    """The same feed found in two directories, recognised as one show.

    Directories disagree about http or https and a trailing slash, so neither
    counts.
    """
    parts = urlsplit(clean_feed_url(url))
    return f"{parts.netloc.lower().removeprefix('www.')}{parts.path.rstrip('/')}?{parts.query}"


def _local(tag: str) -> str:
    """An element's name without its namespace, so itunes:author reads as author."""
    return tag.rsplit("}", 1)[-1]


def _child_text(element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _extension(url: str, mime: str) -> str:
    path_ext = os.path.splitext(urlparse(url).path)[1].lstrip(".").lower()
    if path_ext in AUDIO_EXTENSIONS:
        return path_ext
    return MIME_EXTENSIONS.get((mime or "").split(";")[0].strip().lower(), "mp3")


def _published(text: str) -> float:
    if not text:
        return 0.0
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0.0


def parse_feed(content: bytes, feed_url: str) -> Feed:
    """A feed's title and its episodes, newest first.

    Items with no audio enclosure are left out: a show's text-only
    announcements have nothing to play. Publishers mostly list newest first,
    but not all, so the order comes from the dates and not from the file.
    """
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise errors.ServiceError(f"not a podcast feed: {error}") from error
    channel = next((e for e in root.iter() if _local(e.tag) == "channel"), None)
    if channel is None:
        raise errors.ServiceError("not a podcast feed: no channel")
    episodes: List[Episode] = []
    for item in channel:
        if _local(item.tag) != "item":
            continue
        enclosure = next((c for c in item if _local(c.tag) == "enclosure"), None)
        url = (enclosure.get("url") or "").strip() if enclosure is not None else ""
        if not url:
            continue
        title = _child_text(item, "title") or url
        episodes.append(
            Episode(
                title=title,
                url=url,
                guid=_child_text(item, "guid") or url,
                published=_published(_child_text(item, "pubDate")),
                extension=_extension(url, enclosure.get("type", "")),
            )
        )
    # sorted() is stable, so undated items keep the feed's own order among themselves.
    episodes = sorted(episodes, key=lambda episode: episode.published, reverse=True)
    return Feed(title=_child_text(channel, "title") or feed_url, feed_url=feed_url, episodes=episodes)


class PodcastDirectory:
    """Finding shows in one directory or all of them, and reading their episodes.

    spotify is a callable returning the bot's SpotifyService, or None when
    Spotify is switched off. A callable, because the service is created before
    this and is only usable once its engine and stored secrets are attached.
    """

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        spotify: Optional[Callable[[], Any]] = None,
        spotify_market: str = "US",
    ) -> None:
        self.session = session or requests.Session()
        self.spotify = spotify or (lambda: None)
        self.spotify_market = spotify_market or "US"

    # -- search ------------------------------------------------------------

    def search(self, query: str, limit: int = 10, source: str = "all") -> List[Podcast]:
        """Shows matching query, from one directory or ("all") every one that answers.

        A single directory's failure is raised, so the user hears that it was
        that directory. Searching all of them skips any that fails or is slow
        and raises only when none answered at all.
        """
        if source != "all":
            return self._search_one(source, query, limit)

        pool = ThreadPoolExecutor(max_workers=len(DIRECTORIES), thread_name_prefix="PodcastSearch")
        futures = {name: pool.submit(self._search_one, name, query, limit) for name in DIRECTORIES}
        wait(list(futures.values()), timeout=ALL_TIMEOUT)
        # Not waiting for a straggler: its thread finishes on its own and is discarded.
        pool.shutdown(wait=False, cancel_futures=True)

        found: Dict[str, List[Podcast]] = {}
        for name, future in futures.items():
            if not future.done():
                logger.info(f"[podcasts] {name} did not answer within {ALL_TIMEOUT}s; left out")
                continue
            try:
                found[name] = future.result()
            except Exception as error:
                logger.info(f"[podcasts] {name} search failed: {error}")
        if not found:
            raise errors.ServiceError("no podcast directory answered")

        # Round robin, so each directory's best match is near the top instead of
        # one directory's whole list coming first.
        merged: List[Podcast] = []
        seen = set()
        lists = [found[name] for name in DIRECTORIES if name in found]
        for rank in range(max(len(items) for items in lists)):
            for items in lists:
                if rank >= len(items):
                    continue
                podcast = items[rank]
                key = podcast.feed_url if podcast.source == "spotify" else feed_key(podcast.feed_url)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(podcast)
        return merged[: limit + 5]

    def _search_one(self, source: str, query: str, limit: int) -> List[Podcast]:
        if source == "apple":
            return self._apple(query, limit)
        if source == "fyyd":
            return self._fyyd(query, limit)
        if source == "gpodder":
            return self._gpodder(query, limit)
        if source == "spotify":
            return self._spotify_search(query, limit)
        raise ValueError(f"unknown podcast directory {source!r}")

    def _json(self, url: str, params: Dict[str, Any]) -> Any:
        response = self.session.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()

    def _apple(self, query: str, limit: int) -> List[Podcast]:
        data = self._json(SEARCH_URL, {"media": "podcast", "entity": "podcast", "term": query, "limit": limit})
        podcasts = []
        for item in data.get("results", []):
            feed_url = item.get("feedUrl")
            if not feed_url:
                # Apple lists some shows it cannot hand a feed for. They cannot
                # be played or subscribed to, so offering them is a dead end.
                continue
            podcasts.append(
                Podcast(
                    title=item.get("collectionName") or item.get("trackName") or feed_url,
                    feed_url=feed_url,
                    author=item.get("artistName") or "",
                    source="apple",
                )
            )
        return podcasts

    def _fyyd(self, query: str, limit: int) -> List[Podcast]:
        # By title, not "term": measured on 2026-10-11, term took 16 to 25
        # seconds per search and title about one.
        data = self._json(FYYD_URL, {"title": query, "count": limit})
        return [
            Podcast(
                title=item.get("title") or item["xmlURL"],
                feed_url=clean_feed_url(item["xmlURL"]),
                author=item.get("author") or "",
                source="fyyd",
            )
            for item in (data.get("data") or [])
            if item and item.get("xmlURL")
        ][:limit]

    def _gpodder(self, query: str, limit: int) -> List[Podcast]:
        data = self._json(GPODDER_URL, {"q": query})
        return [
            Podcast(
                title=item.get("title") or item["url"],
                # gpodder hands some feeds back with an empty "?format=".
                feed_url=clean_feed_url(item["url"]),
                author=item.get("author") or "",
                source="gpodder",
            )
            for item in (data if isinstance(data, list) else [])
            if item and item.get("url")
        ][:limit]

    def _spotify_service(self):
        service = self.spotify()
        if service is None or not getattr(service, "is_enabled", False):
            raise errors.ServiceError("Spotify is not available in this bot")
        return service

    def _spotify_search(self, query: str, limit: int) -> List[Podcast]:
        data = self._spotify_service()._get(
            "/search", q=query, type="show", limit=min(limit, 50), market=self.spotify_market
        )
        return [
            Podcast(
                title=item.get("name") or item["uri"],
                feed_url=item["uri"],
                author=item.get("publisher") or "",
                source="spotify",
            )
            # Spotify sends a literal null for a show it will not offer here.
            for item in ((data.get("shows") or {}).get("items") or [])
            if item and item.get("uri", "").startswith(SPOTIFY_PREFIX)
        ]

    # -- reading a show ----------------------------------------------------

    def feed(self, feed_url: str) -> Feed:
        if feed_url.startswith(SPOTIFY_PREFIX):
            return self._spotify_feed(feed_url)
        with self.session.get(
            feed_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True
        ) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(64 * 1024):
                content += chunk
                if len(content) > MAX_FEED_BYTES:
                    raise errors.ServiceError("the podcast feed is too large to read")
        return parse_feed(bytes(content), feed_url)

    def _spotify_feed(self, uri: str) -> Feed:
        service = self._spotify_service()
        show_id = uri[len(SPOTIFY_PREFIX):]
        show = service._get(f"/shows/{show_id}", market=self.spotify_market)
        episodes: List[Episode] = []
        offset = 0
        while offset < SPOTIFY_EPISODE_LIMIT:
            page = service._get(
                f"/shows/{show_id}/episodes", limit=50, offset=offset, market=self.spotify_market
            )
            items = page.get("items") or []
            for item in items:
                if not item or not item.get("uri"):
                    continue
                episodes.append(
                    Episode(
                        title=item.get("name") or item["uri"],
                        url=item["uri"],
                        guid=item["uri"],
                        published=_spotify_date(item.get("release_date", "")),
                        extension="",
                    )
                )
            if not page.get("next") or not items:
                break
            offset += len(items)
        episodes = sorted(episodes, key=lambda episode: episode.published, reverse=True)
        return Feed(title=show.get("name") or uri, feed_url=uri, episodes=episodes)


def _spotify_date(text: str) -> float:
    """Spotify gives "2026-10-09", or only "2026" for some older episodes."""
    for pattern in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            continue
    return 0.0


def episode_track(feed: Feed, episode: Episode) -> Track:
    """An episode as something the player can play and the uploader can download.

    The podcast's name comes first in the track name, so a download is filed
    under its show and "Episode 12" on its own is never the whole title. A
    Spotify episode is a Spotify track in all but name: the Spotify engine
    plays its URI and, as with songs, there is no file to download.
    """
    name = f"{feed.title} - {episode.title}"
    extra = {"podcast": feed.title, "feed": feed.feed_url, "guid": episode.guid}
    if feed.is_spotify:
        return Track(
            service="sp",
            url=episode.url,
            name=name,
            type=TrackType.External,
            engine="librespot",
            extra_info=dict(extra, kind="episode"),
        )
    return Track(
        service="pc",
        url=episode.url,
        name=name,
        format=episode.extension,
        extra_info=extra,
        type=TrackType.Default,
    )


class PodcastService(Service):
    """Registered so tracks and downloads find a service by the name "pc".

    Hidden from `sv`: podcasts have their own commands (pc, pe, pd and the
    subscription ones), because a show is not one thing to play and a bare `p`
    search could not say which episode was meant.
    """

    name = "pc"
    hidden = True
    hostnames: List[str] = []

    def __init__(self, bot: Bot, config: Any) -> None:
        self.config = config
        self.translator = bot.translator
        self.is_enabled = getattr(config, "enabled", True)
        self.error_message = ""
        self.warning_message = ""

        def spotify():
            # Looked up when used: ServiceManager is still being built when this runs.
            manager = getattr(bot, "service_manager", None)
            return manager.services.get("sp") if manager is not None else None

        self.directory = PodcastDirectory(
            spotify=spotify, spotify_market=getattr(config, "spotify_market", "US")
        )

    @property
    def help(self) -> str:
        return self.translator.translate(
            "Podcasts have their own commands. Send h to see pc, pcd, pe, pd, pa, ps and pu."
        )

    def initialize(self) -> None:
        pass

    def get(self, url: str, extra_info: Optional[Dict[str, Any]] = None, process: bool = False) -> List[Track]:
        feed = self.directory.feed(url)
        return [episode_track(feed, episode) for episode in feed.episodes]

    def search(self, query: str, limit: Optional[int] = None) -> List[Track]:
        raise errors.NothingFoundError("")

    def download(self, track: Track, file_path: str, video: bool = False) -> None:
        """Fetch the episode file, failing loudly.

        Not the shared downloader: that one writes whatever the server answers,
        error page included, and reports a failure only to stdout, so a missing
        episode would upload as a broken file and be called a success.
        """
        with requests.get(
            track.url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True
        ) as response:
            response.raise_for_status()
            with open(file_path, "wb") as file:
                for chunk in response.iter_content(256 * 1024):
                    file.write(chunk)


__all__ = [
    "Episode",
    "Feed",
    "Podcast",
    "PodcastDirectory",
    "PodcastService",
    "episode_track",
    "parse_feed",
]
