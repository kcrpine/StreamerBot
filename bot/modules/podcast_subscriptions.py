"""Per-user podcast subscriptions, and telling people when a new episode is out.

Subscriptions are keyed by TeamTalk account name, not user id: an id is handed
out per connection, so the same person has a different one tomorrow.

The checker reads every subscribed feed once per `services.pc.check_hours` and
queues a notice for each subscriber. Notices wait for their user: one queued
while someone is away is sent the next time the bot sees them online, rather
than being lost or broadcast to the channel.

Saved in the bot's own data directory, like everything else that is per bot.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from bot.services.podcasts import Feed, PodcastDirectory

logger = logging.getLogger(__name__)

FILE_NAME = "podcasts.json"
# How many episode ids to remember per subscription. Enough to cover a feed's
# visible window many times over, so an episode a publisher moves down the list
# is not announced twice, while a show with daily episodes stays a small file.
SEEN_LIMIT = 300
# Only the newest episodes are compared. A publisher re-tagging its whole back
# catalogue must not produce hundreds of notices.
COMPARE_NEWEST = 20
# Several new episodes of one show become a single notice from this many up.
SUMMARISE_FROM = 3
# A person away for months gets at most this many notices on their return.
PENDING_LIMIT = 30
WAKE_SECONDS = 300


class PodcastSubscriptions:
    def __init__(
        self,
        data_dir: str,
        translate: Callable[[str], str],
        directory: Optional[PodcastDirectory] = None,
        check_hours: float = 6,
    ) -> None:
        self.path = os.path.join(data_dir, FILE_NAME)
        self.translate = translate
        self.directory = directory or PodcastDirectory()
        self.check_hours = check_hours
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = self._load()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_check = 0.0

    # -- storage -----------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict) and isinstance(data.get("users"), dict):
                return data
            logger.warning(f"[podcasts] {self.path} is not a subscription file; starting empty")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            logger.warning(f"[podcasts] could not read {self.path}: {error}; starting empty")
        return {"version": 1, "users": {}}

    def _save(self) -> None:
        # Written whole and renamed into place, so a crash mid-write leaves the
        # previous file rather than half of one.
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(self._data, file, ensure_ascii=False, indent=1)
        os.replace(temp, self.path)

    def _user(self, username: str) -> Dict[str, Any]:
        return self._data["users"].setdefault(username, {"subscriptions": [], "pending": []})

    # -- what the commands use ---------------------------------------------

    def subscriptions(self, username: str) -> List[Dict[str, Any]]:
        with self._lock:
            user = self._data["users"].get(username)
            return [dict(s) for s in user["subscriptions"]] if user else []

    def subscribe(self, username: str, feed: Feed) -> bool:
        """False when already subscribed. Episodes out now are not announced."""
        with self._lock:
            user = self._user(username)
            if any(s["feed"] == feed.feed_url for s in user["subscriptions"]):
                return False
            user["subscriptions"].append(
                {
                    "title": feed.title,
                    "feed": feed.feed_url,
                    "seen": [episode.guid for episode in feed.episodes[:SEEN_LIMIT]],
                }
            )
            self._save()
            return True

    def unsubscribe(self, username: str, number: int) -> Optional[str]:
        """The title removed, or None when there is no subscription with that number."""
        with self._lock:
            user = self._data["users"].get(username)
            if not user or not 1 <= number <= len(user["subscriptions"]):
                return None
            removed = user["subscriptions"].pop(number - 1)
            user["pending"] = [p for p in user["pending"] if p.get("feed") != removed["feed"]]
            self._save()
            return removed["title"]

    def search_directory(self, username: str) -> str:
        """Which directory pc searches for this user: a code, or "all"."""
        with self._lock:
            user = self._data["users"].get(username)
            return (user or {}).get("directory") or "all"

    def set_search_directory(self, username: str, source: str) -> None:
        with self._lock:
            self._user(username)["directory"] = source
            self._save()

    def number_of(self, username: str, feed_url: str) -> Optional[int]:
        with self._lock:
            for index, subscription in enumerate(self.subscriptions(username), 1):
                if subscription["feed"] == feed_url:
                    return index
        return None

    # -- checking ----------------------------------------------------------

    def check_feeds(self) -> int:
        """Read each subscribed feed once and queue notices. Returns how many were queued."""
        with self._lock:
            feeds = sorted({
                s["feed"]
                for user in self._data["users"].values()
                for s in user["subscriptions"]
            })
        queued = 0
        for feed_url in feeds:
            # Fetched outside the lock: a slow publisher must not hold up a
            # command that only wants to list someone's subscriptions.
            try:
                feed = self.directory.feed(feed_url)
            except Exception as error:
                logger.warning(f"[podcasts] could not read {feed_url}: {error}")
                continue
            queued += self._record(feed)
        self._last_check = time.time()
        return queued

    def _record(self, feed: Feed) -> int:
        queued = 0
        newest = feed.episodes[:COMPARE_NEWEST]
        with self._lock:
            for user in self._data["users"].values():
                for subscription in user["subscriptions"]:
                    if subscription["feed"] != feed.feed_url:
                        continue
                    seen = set(subscription["seen"])
                    new = [episode for episode in newest if episode.guid not in seen]
                    if not new:
                        continue
                    user["pending"].append(
                        {
                            "feed": feed.feed_url,
                            "podcast": feed.title,
                            "episode": new[0].title,
                            "count": len(new),
                        }
                    )
                    del user["pending"][:-PENDING_LIMIT]
                    subscription["seen"] = (
                        [episode.guid for episode in new] + subscription["seen"]
                    )[:SEEN_LIMIT]
                    queued += 1
            if queued:
                self._save()
        return queued

    def notice(self, username: str, item: Dict[str, Any]) -> str:
        number = self.number_of(username, item["feed"])
        # It may arrive days later, so it says why it was sent. No full stop
        # after the title, which often ends in one of its own.
        if item.get("count", 1) >= SUMMARISE_FROM:
            text = self.translate(
                "New episodes of {podcast}, which you subscribed to: {count}. The newest is: {episode}"
            ).format(podcast=item["podcast"], count=item["count"], episode=item["episode"])
        else:
            text = self.translate("New episode of {podcast}, which you subscribed to: {episode}").format(
                podcast=item["podcast"], episode=item["episode"]
            )
        if number is None:
            return text
        # The command on its own line and last, so a screen reader's review cursor lands on it.
        return text + "\n" + self.translate("To open the podcast, send: ps {number}").format(number=number)

    def deliver(self, online: Dict[str, int], send: Callable[[str, int], None]) -> int:
        """Send waiting notices to whoever in online (username to user id) has any."""
        sent = 0
        with self._lock:
            ready = {
                username: list(user["pending"])
                for username, user in self._data["users"].items()
                if user["pending"] and username in online
            }
        for username, items in ready.items():
            for item in items:
                try:
                    send(self.notice(username, item), online[username])
                    sent += 1
                except Exception as error:
                    logger.warning(f"[podcasts] could not tell {username} about a new episode: {error}")
                    break
            with self._lock:
                pending = self._data["users"][username]["pending"]
                del pending[: len(items)]
                self._save()
        return sent

    # -- background thread -------------------------------------------------

    def start(self, online: Callable[[], Dict[str, int]], send: Callable[[str, int], None]) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(WAKE_SECONDS):
                try:
                    if self.check_hours > 0 and time.time() - self._last_check >= self.check_hours * 3600:
                        self.check_feeds()
                    self.deliver(online(), send)
                except Exception as error:
                    logger.error(f"[podcasts] the subscription check failed: {error}", exc_info=True)

        self._thread = threading.Thread(target=loop, daemon=True, name="PodcastSubscriptions")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


__all__ = ["PodcastSubscriptions"]
