"""Podcast commands: pc, pcd, pe, pd, pa, ps and pu.

They all work on one thing per user, the open podcast, much as `sl` works on the
last search list. `pc` with words searches, and `pc` or `ps` with a number opens
a show; `pe`, `pd` and `pa` then act on whichever show is open. `pcd` picks the
directory `pc` searches, or all of them. Each step's reply names the command
that comes next, last in the message, so a screen reader's review cursor lands
on it.

Feeds are read on the task thread, never on the main loop: a slow publisher can
take seconds to answer, and nothing else should wait for it.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from bot import errors
from bot.commands.command import Command
from bot.services.podcasts import DIRECTORIES, DIRECTORY_NAMES, Feed, Podcast, episode_track

if TYPE_CHECKING:
    from bot.TeamTalk.structs import User

PAGE = 10
SEARCH_LIMIT = 10


def _date(published: float) -> str:
    # ISO dates: the same in every language. How each synth speaks them (eSpeak
    # may say "2026, 10, 09") has not been checked in NVDA.
    return time.strftime("%Y-%m-%d", time.gmtime(published)) if published else ""


class PodcastCommand(Command):
    """State shared by the podcast commands, kept on the CommandProcessor per user."""

    @property
    def _results(self) -> Dict[int, List[Podcast]]:
        return self.command_processor.pending_podcast_results

    @property
    def _open(self) -> Dict[int, Dict]:
        return self.command_processor.open_podcast

    @property
    def subscriptions(self):
        return getattr(self.command_processor, "podcast_subscriptions", None)

    def _directory(self):
        return self.service_manager.get_service_by_name("pc").directory

    def _send(self, text, user: User) -> None:
        messages = [m for m in (text if isinstance(text, list) else [text]) if m]
        for index, message in enumerate(messages):
            if index:
                # As CommandProcessor does: flood protection drops back-to-back messages.
                time.sleep(0.15)
            self.ttclient.send_message(message, user)

    def _open_feed(self, feed_url: str, user: User) -> None:
        """Read a feed, make it the user's open podcast, and send its newest episodes."""
        try:
            feed = self._directory().feed(feed_url)
        except Exception as error:
            logging.warning(f"[podcasts] could not read {feed_url}: {error}")
            self._send(self.translator.translate("That podcast could not be read. Try again later."), user)
            return
        if not feed.episodes:
            self._send(
                self.translator.translate("{podcast} has no episodes to play.").format(podcast=feed.title),
                user,
            )
            return
        self._open[user.id] = {"feed": feed, "cursor": 0}
        self._send(self._page(user), user)

    def _page(self, user: User) -> List[str]:
        """The next page of the open podcast's episodes, then what to send next."""
        state = self._open[user.id]
        feed: Feed = state["feed"]
        start = state["cursor"]
        if start >= len(feed.episodes):
            start = 0
        episodes = feed.episodes[start : start + PAGE]
        state["cursor"] = start + len(episodes)
        lines = [
            self.translator.translate("{podcast}, episodes {first} to {last} of {total}:").format(
                podcast=feed.title, first=start + 1, last=start + len(episodes), total=len(feed.episodes)
            )
        ]
        for number, episode in enumerate(episodes, start + 1):
            # The date before the title: titles carry commas and their own
            # numbers, and a long one would push a trailing date past the
            # 256-character message limit.
            date = _date(episode.published)
            lines.append(f"{number}. {date}: {episode.title}" if date else f"{number}. {episode.title}")
        if state["cursor"] < len(feed.episodes):
            next_step = self.translator.translate("For the next ten episodes, send: pe")
        else:
            next_step = self.translator.translate("To start the list again from episode 1, send: pe")
        if feed.is_spotify:
            options = self.translator.translate(
                "pe and a number plays an episode. pa subscribes to this podcast. Spotify "
                "episodes cannot be downloaded."
            )
        else:
            options = self.translator.translate(
                "pe and a number plays an episode. pd and a number downloads one. pd on its "
                "own downloads several as one zip file. pa subscribes to this podcast."
            )
        # Its own message, so it is not buried at the end of a long list.
        return ["\n".join(lines), options + " " + next_step]

    def _out_of_list(self, number: int, total: int) -> str:
        return self.translator.translate(
            "There is no number {number}. The list goes from 1 to {total}."
        ).format(number=number, total=total)

    def _require_open(self, user: User) -> Optional[Feed]:
        state = self._open.get(user.id)
        return state["feed"] if state else None

    def _chosen_directory(self, user: User) -> str:
        """Saved with the user's subscriptions when there is an account name, else for this session."""
        if self.subscriptions is not None and user.username:
            return self.subscriptions.search_directory(user.username)
        return self.command_processor.podcast_directory_choice.get(user.id, "all")

    def _no_open_podcast(self) -> str:
        return self.translator.translate(
            "No podcast is open. To find one, send pc and a few words, for example: pc history"
        )

    def _episode(self, user: User, arg: str):
        """(feed, episode) for pe N or pd N, or a reply saying what was wrong."""
        feed = self._require_open(user)
        if feed is None:
            return None, self._no_open_podcast()
        try:
            number = int(arg.strip())
        except ValueError:
            raise errors.InvalidArgumentError()
        if not 1 <= number <= len(feed.episodes):
            return None, self.translator.translate(
                "{podcast} has episodes 1 to {total}."
            ).format(podcast=feed.title, total=len(feed.episodes))
        return (feed, feed.episodes[number - 1]), None


class PodcastSearchCommand(PodcastCommand):
    @property
    def help(self) -> str:
        return self.translator.translate(
            "WORDS Searches for podcasts. pc and a number opens one from the list"
        )

    def __call__(self, arg: str, user: User) -> Optional[str]:
        arg = (arg or "").strip()
        if not arg:
            raise errors.InvalidArgumentError()
        results = self._results.get(user.id)
        if arg.isdigit() and results:
            number = int(arg)
            if not 1 <= number <= len(results):
                return self._out_of_list(number, len(results))
            self.run_async(self._open_feed, results[number - 1].feed_url, user)
            return self.translator.translate("Opening {podcast}.").format(podcast=results[number - 1].title)
        self.run_async(self._search, arg, user)
        return self.translator.translate("Searching...")

    def _search(self, query: str, user: User) -> None:
        source = self._chosen_directory(user)
        try:
            podcasts = self._directory().search(query, SEARCH_LIMIT, source=source)
        except Exception as error:
            logging.warning(f"[podcasts] search in {source} for {query!r} failed: {error}")
            if source == "all":
                text = self.translator.translate("No podcast directory answered. Try again later.")
            else:
                # Says which one failed, and the way round it last.
                text = self.translator.translate(
                    "{directory} did not answer. To search every directory instead, send: pcd all"
                ).format(directory=DIRECTORY_NAMES[source])
            self._send(text, user)
            return
        if not podcasts:
            self._send(self.translator.translate("Nothing is found for your query"), user)
            return
        self._results[user.id] = podcasts
        lines = [self.translator.translate("Podcasts found: {count}").format(count=len(podcasts))]
        for number, podcast in enumerate(podcasts, 1):
            # The directory is named on every line: a Spotify show plays but
            # cannot be downloaded, and the same title can appear in two places.
            directory = DIRECTORY_NAMES.get(podcast.source, podcast.source)
            if podcast.author:
                line = self.translator.translate("{title}, by {author}, from {directory}").format(
                    title=podcast.title, author=podcast.author, directory=directory
                )
            else:
                line = self.translator.translate("{title}, from {directory}").format(
                    title=podcast.title, directory=directory
                )
            lines.append(f"{number}. {line}")
        self._send(
            ["\n".join(lines), self.translator.translate("To open one, send pc and its number, for example: pc 1")],
            user,
        )


class PodcastDirectoryCommand(PodcastCommand):
    """pcd: which directory pc searches, kept per account name."""

    @property
    def help(self) -> str:
        return self.translator.translate(
            "[DIRECTORY] Chooses where pc searches: apple, fyyd, gpodder, spotify, or all of "
            "them. On its own, says which one is chosen"
        )

    def __call__(self, arg: str, user: User) -> Optional[str]:
        choice = (arg or "").strip().lower()
        current = self._chosen_directory(user)
        if not choice:
            return [
                self._describe(current),
                self.translator.translate(
                    "Directories: apple for Apple Podcasts, fyyd, gpodder for gpodder.net, and "
                    "spotify, whose episodes play but cannot be downloaded. Send pcd and a name "
                    "to choose one. To search them all, send: pcd all"
                ),
            ]
        if choice != "all" and choice not in DIRECTORIES:
            return self.translator.translate(
                "There is no directory called {name}. Send pcd on its own to hear the list."
            ).format(name=choice)
        if self.subscriptions is not None and user.username:
            self.subscriptions.set_search_directory(user.username, choice)
        else:
            self.command_processor.podcast_directory_choice[user.id] = choice
        return self._describe(choice)

    def _describe(self, source: str) -> str:
        if source == "all":
            return self.translator.translate("pc searches every podcast directory.")
        return self.translator.translate("pc searches {directory} only.").format(
            directory=DIRECTORY_NAMES[source]
        )


class PodcastEpisodeCommand(PodcastCommand):
    @property
    def help(self) -> str:
        return self.translator.translate(
            "[NUMBER] Plays episode NUMBER of the open podcast. On its own, lists the next ten episodes"
        )

    def __call__(self, arg: str, user: User):
        if not (arg or "").strip():
            if self._require_open(user) is None:
                return self._no_open_podcast()
            return self._page(user)
        found, problem = self._episode(user, arg)
        if problem:
            return problem
        feed, episode = found
        track = episode_track(feed, episode)
        if self.config.general.send_channel_messages:
            self.run_async(
                self.ttclient.send_message,
                self.translator.translate("{nickname} selected: {track}").format(
                    nickname=user.nickname, track=track.name
                ),
                type=2,
            )
        self.run_async(self.player.play, [track])
        return self.translator.translate("Playing {}").format(track.name)


class PodcastDownloadCommand(PodcastCommand):
    @property
    def help(self) -> str:
        return self.translator.translate(
            "[NUMBER] Downloads episode NUMBER of the open podcast to the channel. On its own, "
            "asks how many of the newest episodes to download as one zip file"
        )

    def _spotify_refusal(self, feed: Feed) -> str:
        # translators: pcd is a command, not a word to translate, and it stays last.
        return self.translator.translate(
            "{podcast} is a Spotify show, so its episodes can be played but not downloaded. "
            "Many shows also publish a public feed. To look for one, search every directory "
            "again. To switch to every directory, send: pcd all"
        ).format(podcast=feed.title)

    def __call__(self, arg: str, user: User) -> Optional[str]:
        open_feed = self._require_open(user)
        if open_feed is not None and open_feed.is_spotify:
            # Refused here, in words: Spotify's audio is DRM protected and
            # SpotifyService.download would raise on the uploader's thread.
            return self._spotify_refusal(open_feed)
        if (arg or "").strip():
            found, problem = self._episode(user, arg)
            if problem:
                return problem
            feed, episode = found
            track = episode_track(feed, episode)
            self.module_manager.uploader(track, user)
            return self.translator.translate(
                "Downloading {name}. It will be uploaded to the channel when it is ready."
            ).format(name=track.name)

        feed = self._require_open(user)
        if feed is None:
            return self._no_open_podcast()

        def answer(text: str) -> Optional[str]:
            """The reply to the question below, or None when the message is not one.

            An empty string is an answer with nothing to add: the playlist
            uploader announces the download itself.
            """
            text = text.strip()
            if not text.isdigit():
                return None
            count = int(text)
            if not 1 <= count <= len(feed.episodes):
                self.command_processor.pending_podcast_count[user.id] = answer
                return self.translator.translate(
                    "Send a number from 1 to {total}."
                ).format(total=len(feed.episodes))
            tracks = [episode_track(feed, episode) for episode in feed.episodes[:count]]
            self.module_manager.playlist_uploader(tracks, user, feed.title)
            return ""

        self.command_processor.pending_podcast_count[user.id] = answer
        # The count after a colon, so no language needs a plural form here.
        return self.translator.translate(
            "Episodes of {podcast}: {total}. To download the newest ones as one zip file, "
            "send how many, from 1 to {total}"
        ).format(podcast=feed.title, total=len(feed.episodes))


class PodcastSubscribeCommand(PodcastCommand):
    @property
    def help(self) -> str:
        return self.translator.translate(
            "Subscribes you to the open podcast. The bot tells you privately when a new episode is out"
        )

    def __call__(self, arg: str, user: User) -> Optional[str]:
        if self.subscriptions is None:
            return self.translator.translate("Podcast subscriptions are not available in this bot.")
        feed = self._require_open(user)
        if feed is None:
            return self._no_open_podcast()
        if not user.username:
            return self.translator.translate(
                "Subscriptions are kept by TeamTalk account name, and you are logged in without one."
            )
        if not self.subscriptions.subscribe(user.username, feed):
            return self.translator.translate("You are already subscribed to {podcast}.").format(podcast=feed.title)
        return self.translator.translate(
            "Subscribed to {podcast}. You will get a private message when a new episode is "
            "out, or the next time you are online. To see your subscriptions, send: ps"
        ).format(podcast=feed.title)


class PodcastSubscriptionsCommand(PodcastCommand):
    @property
    def help(self) -> str:
        return self.translator.translate(
            "[NUMBER] Lists your podcast subscriptions. With a number, opens that podcast"
        )

    def __call__(self, arg: str, user: User):
        if self.subscriptions is None:
            return self.translator.translate("Podcast subscriptions are not available in this bot.")
        subscriptions = self.subscriptions.subscriptions(user.username) if user.username else []
        if not subscriptions:
            return self.translator.translate(
                "You have no podcast subscriptions. To add one, open a podcast and send: pa"
            )
        arg = (arg or "").strip()
        if arg:
            try:
                number = int(arg)
            except ValueError:
                raise errors.InvalidArgumentError()
            if not 1 <= number <= len(subscriptions):
                return self._out_of_list(number, len(subscriptions))
            chosen = subscriptions[number - 1]
            self.run_async(self._open_feed, chosen["feed"], user)
            return self.translator.translate("Opening {podcast}.").format(podcast=chosen["title"])
        lines = [self.translator.translate("Your podcast subscriptions:")]
        lines += [f"{number}. {s['title']}" for number, s in enumerate(subscriptions, 1)]
        return [
            "\n".join(lines),
            self.translator.translate(
                "ps and a number opens one, pu and a number unsubscribes. For example: ps 1"
            ),
        ]


class PodcastUnsubscribeCommand(PodcastCommand):
    @property
    def help(self) -> str:
        return self.translator.translate("NUMBER Unsubscribes from podcast NUMBER in your ps list")

    def __call__(self, arg: str, user: User) -> Optional[str]:
        if self.subscriptions is None:
            return self.translator.translate("Podcast subscriptions are not available in this bot.")
        try:
            number = int((arg or "").strip())
        except ValueError:
            raise errors.InvalidArgumentError()
        title = self.subscriptions.unsubscribe(user.username, number) if user.username else None
        if title is None:
            total = len(self.subscriptions.subscriptions(user.username)) if user.username else 0
            if not total:
                return self.translator.translate(
                    "You have no podcast subscriptions. To add one, open a podcast and send: pa"
                )
            return self._out_of_list(number, total)
        return self.translator.translate("Unsubscribed from {podcast}.").format(podcast=title)


__all__ = [
    "PodcastDirectoryCommand",
    "PodcastDownloadCommand",
    "PodcastEpisodeCommand",
    "PodcastSearchCommand",
    "PodcastSubscribeCommand",
    "PodcastSubscriptionsCommand",
    "PodcastUnsubscribeCommand",
]
