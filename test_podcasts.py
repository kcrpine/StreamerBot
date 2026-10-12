"""Podcasts (search, feeds, episodes, subscriptions) and the Paramount Plus and Tubi services.

Nothing here reaches the network: the directory gets a stand-in session, and
feeds are literal XML. The browser adapters cannot be exercised without the live
sites, so what is pinned for them is wiring: they are registered, Tubi plays
without an account, and pasted links reach the right page.
"""

import json
import os
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from bot import auth, errors
from bot.commands import podcast_commands
from bot.commands.user_commands import LoginCommand
from bot.modules.podcast_subscriptions import PodcastSubscriptions
from bot.player.enums import TrackType
from bot.services.browser_service import ParamountService, TubiService
from bot.services.podcasts import (
    Episode,
    Feed,
    PodcastDirectory,
    episode_track,
    parse_feed,
)
from bot.services.web.paramount import ParamountAdapter
from bot.services.web.tubi import TubiAdapter

FEED = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>History Hour</title>
  <item><title>Older</title><guid>g1</guid>
    <pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate>
    <enclosure url="https://cdn.example/older.mp3" type="audio/mpeg"/></item>
  <item><title>Announcement</title><guid>g0</guid></item>
  <item><title>Newest</title><guid>g2</guid>
    <pubDate>Fri, 09 Oct 2026 10:00:00 GMT</pubDate>
    <enclosure url="https://cdn.example/newest?id=7" type="audio/x-m4a"/></item>
</channel>
</rss>"""


def feed_with(*guids):
    return Feed(
        title="History Hour",
        feed_url="https://feeds.example/history",
        episodes=[Episode(title=f"Episode {g}", url=f"https://cdn.example/{g}.mp3", guid=g) for g in guids],
    )


class FeedParsingTests(TestCase):
    def test_episodes_are_newest_first_and_items_without_audio_are_left_out(self):
        feed = parse_feed(FEED, "https://feeds.example/history")

        self.assertEqual(feed.title, "History Hour")
        self.assertEqual([e.title for e in feed.episodes], ["Newest", "Older"])

    def test_the_file_type_comes_from_the_url_or_else_the_mime_type(self):
        feed = parse_feed(FEED, "https://feeds.example/history")

        self.assertEqual(feed.episodes[0].extension, "m4a")
        self.assertEqual(feed.episodes[1].extension, "mp3")

    def test_something_that_is_not_a_feed_is_a_service_error(self):
        with self.assertRaises(errors.ServiceError):
            parse_feed(b"<html><body>Not found</body></html>", "https://x")

    def test_an_episode_track_names_its_show_and_plays_as_a_plain_file(self):
        feed = parse_feed(FEED, "https://feeds.example/history")

        track = episode_track(feed, feed.episodes[0])

        self.assertEqual(track.name, "History Hour - Newest")
        self.assertEqual(track.service, "pc")
        self.assertEqual(track.type, TrackType.Default)
        self.assertEqual(track.format, "m4a")


def answering(responses):
    """A session whose get() answers per URL; an Exception value is raised."""
    session = Mock()

    def get(url, **kwargs):
        value = next(v for prefix, v in responses.items() if url.startswith(prefix))
        if isinstance(value, Exception):
            raise value
        response = Mock()
        response.json.return_value = value
        return response

    session.get.side_effect = get
    return session


APPLE = {"results": [
    {"collectionName": "No Feed"},
    {"collectionName": "History Hour", "artistName": "BBC", "feedUrl": "https://feeds.example/h"},
]}
FYYD = {"data": [{"title": "History Hour", "author": "BBC", "xmlURL": "http://feeds.example/h/"},
                 {"title": "Old Times", "author": "", "xmlURL": "https://old.example/rss"}]}
GPODDER = [{"title": "Hardcore", "author": "Dan", "url": "http://feeds.example/dan?format="}]


class DirectorySearchTests(TestCase):
    def test_apple_shows_without_a_feed_are_not_offered(self):
        session = answering({"https://itunes.apple.com": APPLE})

        found = PodcastDirectory(session).search("history", source="apple")

        self.assertEqual([(p.title, p.author, p.source) for p in found], [("History Hour", "BBC", "apple")])
        self.assertEqual(session.get.call_args.kwargs["params"]["term"], "history")

    def test_fyyd_is_searched_by_title_which_answers_in_a_second_not_twenty(self):
        session = answering({"https://api.fyyd.de": FYYD})

        found = PodcastDirectory(session).search("history", source="fyyd")

        self.assertEqual(session.get.call_args.kwargs["params"]["title"], "history")
        self.assertEqual([p.source for p in found], ["fyyd", "fyyd"])

    def test_an_empty_query_parameter_is_dropped_from_a_gpodder_feed(self):
        found = PodcastDirectory(answering({"https://gpodder.net": GPODDER})).search("dan", source="gpodder")

        self.assertEqual(found[0].feed_url, "http://feeds.example/dan")

    def test_all_takes_turns_between_directories_and_lists_a_show_once(self):
        session = answering({
            "https://itunes.apple.com": APPLE,
            "https://api.fyyd.de": FYYD,
            "https://gpodder.net": GPODDER,
        })

        found = PodcastDirectory(session).search("history")

        # fyyd's History Hour is Apple's, over http and with a trailing slash.
        self.assertEqual([p.title for p in found], ["History Hour", "Hardcore", "Old Times"])

    def test_all_leaves_out_a_directory_that_fails(self):
        session = answering({
            "https://itunes.apple.com": APPLE,
            "https://api.fyyd.de": ConnectionError("down"),
            "https://gpodder.net": GPODDER,
        })

        found = PodcastDirectory(session).search("history")

        self.assertEqual([p.source for p in found], ["apple", "gpodder"])

    def test_all_fails_only_when_nothing_answered(self):
        session = answering({"https": ConnectionError("down")})

        with self.assertRaises(errors.ServiceError):
            PodcastDirectory(session).search("history")


class SpotifyShowTests(TestCase):
    def make(self, responses):
        spotify = Mock(is_enabled=True)
        spotify._get.side_effect = lambda path, **params: responses[path]
        return PodcastDirectory(Mock(), spotify=lambda: spotify, spotify_market="GB"), spotify

    def test_search_finds_shows_and_skips_the_nulls_spotify_sends(self):
        directory, spotify = self.make({"/search": {"shows": {"items": [
            None,
            {"name": "Desert Island Discs", "publisher": "BBC", "uri": "spotify:show:abc"},
        ]}}})

        found = directory.search("discs", source="spotify")

        self.assertEqual([(p.title, p.feed_url, p.source) for p in found],
                         [("Desert Island Discs", "spotify:show:abc", "spotify")])
        self.assertEqual(spotify._get.call_args.kwargs["market"], "GB")

    def test_a_show_reads_as_a_feed_of_episodes_newest_first(self):
        directory, _ = self.make({
            "/shows/abc": {"name": "Desert Island Discs"},
            "/shows/abc/episodes": {"items": [
                {"name": "Old", "uri": "spotify:episode:1", "release_date": "2019"},
                {"name": "New", "uri": "spotify:episode:2", "release_date": "2026-10-09"},
            ], "next": None},
        })

        feed = directory.feed("spotify:show:abc")

        self.assertTrue(feed.is_spotify)
        self.assertEqual([e.title for e in feed.episodes], ["New", "Old"])

    def test_a_spotify_episode_plays_through_the_spotify_engine(self):
        feed = Feed(title="Discs", feed_url="spotify:show:abc",
                    episodes=[Episode(title="New", url="spotify:episode:2", guid="spotify:episode:2")])

        track = episode_track(feed, feed.episodes[0])

        self.assertEqual((track.service, track.engine, track.url), ("sp", "librespot", "spotify:episode:2"))
        self.assertEqual(track.type, TrackType.External)

    def test_without_spotify_the_directory_says_so(self):
        directory = PodcastDirectory(Mock(), spotify=lambda: None)

        with self.assertRaises(errors.ServiceError):
            directory.search("discs", source="spotify")


class SubscriptionTests(TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.directory = Mock()
        self.subs = PodcastSubscriptions(self.dir.name, lambda s: s, directory=self.directory)

    def test_episodes_out_when_subscribing_are_not_announced(self):
        self.subs.subscribe("alice", feed_with("g2", "g1"))
        self.directory.feed.return_value = feed_with("g2", "g1")

        self.assertEqual(self.subs.check_feeds(), 0)

    def test_a_new_episode_waits_until_its_subscriber_is_online(self):
        self.subs.subscribe("alice", feed_with("g1"))
        self.directory.feed.return_value = feed_with("g2", "g1")
        self.subs.check_feeds()
        send = Mock()

        self.assertEqual(self.subs.deliver({"bob": 4}, send), 0)
        self.assertEqual(self.subs.deliver({"alice": 9}, send), 1)

        text, user_id = send.call_args.args
        self.assertEqual(user_id, 9)
        self.assertIn("History Hour", text)
        self.assertIn("Episode g2", text)
        self.assertTrue(text.endswith("ps 1"), "the command to open it goes last")
        self.assertEqual(self.subs.deliver({"alice": 9}, send), 0, "sent once, not every pass")

    def test_several_new_episodes_become_one_notice(self):
        self.subs.subscribe("alice", feed_with("g1"))
        self.directory.feed.return_value = feed_with("g5", "g4", "g3", "g1")
        self.subs.check_feeds()
        send = Mock()

        self.subs.deliver({"alice": 9}, send)

        self.assertEqual(send.call_count, 1)
        self.assertIn("3", send.call_args.args[0])

    def test_subscriptions_survive_a_restart(self):
        self.subs.subscribe("alice", feed_with("g1"))

        again = PodcastSubscriptions(self.dir.name, lambda s: s, directory=self.directory)

        self.assertEqual([s["title"] for s in again.subscriptions("alice")], ["History Hour"])
        with open(os.path.join(self.dir.name, "podcasts.json"), encoding="utf-8") as file:
            self.assertIn("alice", json.load(file)["users"])

    def test_subscribing_twice_is_refused_and_unsubscribe_takes_the_list_number(self):
        self.assertTrue(self.subs.subscribe("alice", feed_with("g1")))
        self.assertFalse(self.subs.subscribe("alice", feed_with("g1")))

        self.assertIsNone(self.subs.unsubscribe("alice", 2))
        self.assertEqual(self.subs.unsubscribe("alice", 1), "History Hour")
        self.assertEqual(self.subs.subscriptions("alice"), [])

    def test_an_unreadable_file_starts_empty_rather_than_stopping_the_bot(self):
        with open(os.path.join(self.dir.name, "podcasts.json"), "w") as file:
            file.write("{not json")

        subs = PodcastSubscriptions(self.dir.name, lambda s: s, directory=self.directory)

        self.assertEqual(subs.subscriptions("alice"), [])


def make_command(cls, processor=None):
    command = object.__new__(cls)
    feed = parse_feed(FEED, "https://feeds.example/history")
    directory = Mock()
    directory.feed.return_value = feed
    processor = processor or SimpleNamespace(
        pending_podcast_results={},
        open_podcast={},
        pending_podcast_count={},
        podcast_directory_choice={},
        podcast_subscriptions=None,
    )
    command.command_processor = processor
    command.translator = SimpleNamespace(translate=lambda s: s)
    command.ttclient = Mock()
    command.player = Mock()
    command.config = SimpleNamespace(general=SimpleNamespace(send_channel_messages=False))
    command.module_manager = Mock()
    command.service_manager = Mock()
    command.service_manager.get_service_by_name.return_value = SimpleNamespace(directory=directory)
    # Run the "async" work inline so its messages can be read back.
    command.run_async = lambda func, *args, **kwargs: func(*args, **kwargs)
    return command, feed


class PodcastCommandTests(TestCase):
    user = SimpleNamespace(id=7, username="alice", nickname="Alice")

    def sent(self, command):
        return [call.args[0] for call in command.ttclient.send_message.call_args_list]

    def open(self, command):
        command._open_feed("https://feeds.example/history", self.user)

    def test_opening_a_podcast_lists_episodes_then_says_what_to_send_next(self):
        command, _feed = make_command(podcast_commands.PodcastEpisodeCommand)

        self.open(command)

        listing, hint = self.sent(command)
        self.assertIn("1. 2026-10-09: Newest", listing)
        self.assertIn("2. 2026-10-05: Older", listing)
        self.assertTrue(hint.endswith("from episode 1, send: pe"), "the last page says pe starts again")

    def test_pe_with_a_number_plays_that_episode(self):
        command, _feed = make_command(podcast_commands.PodcastEpisodeCommand)
        self.open(command)

        reply = command("2", self.user)

        played = command.player.play.call_args.args[0]
        self.assertEqual(played[0].url, "https://cdn.example/older.mp3")
        self.assertIn("History Hour - Older", reply)

    def test_pe_past_the_last_episode_says_how_many_there_are(self):
        command, _feed = make_command(podcast_commands.PodcastEpisodeCommand)
        self.open(command)

        self.assertIn("1 to 2", command("9", self.user))

    def test_without_an_open_podcast_pe_says_how_to_find_one(self):
        command, _feed = make_command(podcast_commands.PodcastEpisodeCommand)

        self.assertIn("pc history", command("1", self.user))

    def test_pd_with_a_number_downloads_one_episode(self):
        command, _feed = make_command(podcast_commands.PodcastDownloadCommand)
        self.open(command)

        command("1", self.user)

        track = command.module_manager.uploader.call_args.args[0]
        self.assertEqual(track.name, "History Hour - Newest")

    def test_pd_on_its_own_asks_how_many_and_downloads_the_newest_that_many(self):
        command, _feed = make_command(podcast_commands.PodcastDownloadCommand)
        self.open(command)

        question = command("", self.user)
        answer = command.command_processor.pending_podcast_count.pop(self.user.id)

        self.assertIn("1 to 2", question)
        self.assertIsNone(answer("hello"), "not a number: left to run as a command")
        self.assertEqual(answer("1"), "")
        tracks, _user, name = command.module_manager.playlist_uploader.call_args.args
        self.assertEqual([t.name for t in tracks], ["History Hour - Newest"])
        self.assertEqual(name, "History Hour")

    def test_a_number_out_of_range_asks_again(self):
        command, _feed = make_command(podcast_commands.PodcastDownloadCommand)
        self.open(command)
        command("", self.user)
        answer = command.command_processor.pending_podcast_count.pop(self.user.id)

        self.assertIn("1 to 2", answer("50"))
        self.assertIn(self.user.id, command.command_processor.pending_podcast_count)

    def test_search_lists_shows_with_their_directory_and_pc_with_a_number_opens_one(self):
        command, _feed = make_command(podcast_commands.PodcastSearchCommand)
        directory = command.service_manager.get_service_by_name.return_value.directory
        directory.search.return_value = [
            SimpleNamespace(title="History Hour", author="BBC", feed_url="https://feeds.example/history",
                            source="apple")
        ]

        command("history", self.user)
        listing, hint = self.sent(command)
        self.assertIn("1. History Hour, by BBC, from Apple Podcasts", listing)
        self.assertTrue(hint.endswith("pc 1"))
        self.assertEqual(directory.search.call_args.kwargs["source"], "all", "every directory by default")

        command("1", self.user)
        self.assertIn(self.user.id, command.command_processor.open_podcast)

    def test_pcd_chooses_the_directory_and_keeps_it_with_the_account(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        pcd, _ = make_command(podcast_commands.PodcastDirectoryCommand)
        pcd.command_processor.podcast_subscriptions = PodcastSubscriptions(tempdir.name, lambda s: s, Mock())

        self.assertIn("every podcast directory", pcd("", self.user)[0])
        self.assertIn("gpodder.net only", pcd("gpodder", self.user))
        self.assertIn("no directory called", pcd("itunes", self.user))

        again = PodcastSubscriptions(tempdir.name, lambda s: s, Mock())
        self.assertEqual(again.search_directory("alice"), "gpodder")

        search, _ = make_command(podcast_commands.PodcastSearchCommand, pcd.command_processor)
        directory = search.service_manager.get_service_by_name.return_value.directory
        directory.search.side_effect = ConnectionError("down")
        search("history", self.user)
        self.assertTrue(self.sent(search)[0].endswith("send: pcd all"), "says which failed and the way round it")

    def test_pd_refuses_a_spotify_show_in_words(self):
        command, _ = make_command(podcast_commands.PodcastDownloadCommand)
        spotify_feed = Feed(title="Discs", feed_url="spotify:show:abc",
                            episodes=[Episode(title="New", url="spotify:episode:2", guid="g")])
        command.command_processor.open_podcast[self.user.id] = {"feed": spotify_feed, "cursor": 0}

        for arg in ("1", ""):
            self.assertIn("not downloaded", command(arg, self.user))
        command.module_manager.uploader.assert_not_called()
        self.assertNotIn(self.user.id, command.command_processor.pending_podcast_count)

    def test_subscribe_list_and_unsubscribe(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        subscribe, _feed = make_command(podcast_commands.PodcastSubscribeCommand)
        subscribe.command_processor.podcast_subscriptions = PodcastSubscriptions(
            tempdir.name, lambda s: s, directory=Mock()
        )
        self.open(subscribe)

        self.assertIn("Subscribed to History Hour", subscribe("", self.user))
        self.assertIn("already subscribed", subscribe("", self.user))

        listing, _ = make_command(podcast_commands.PodcastSubscriptionsCommand, subscribe.command_processor)
        reply = listing("", self.user)
        self.assertIn("1. History Hour", reply[0])

        unsubscribe, _ = make_command(podcast_commands.PodcastUnsubscribeCommand, subscribe.command_processor)
        self.assertIn("Unsubscribed from History Hour", unsubscribe("1", self.user))

    def test_a_user_without_an_account_name_cannot_subscribe(self):
        command, _feed = make_command(podcast_commands.PodcastSubscribeCommand)
        command.command_processor.podcast_subscriptions = Mock()
        guest = SimpleNamespace(id=8, username="", nickname="Guest")
        command._open_feed("https://feeds.example/history", guest)

        reply = command("", guest)

        self.assertIn("without one", reply)
        command.command_processor.podcast_subscriptions.subscribe.assert_not_called()


def make_browser_service(cls, engine):
    service = object.__new__(cls)
    service.config = SimpleNamespace(enabled=True, profile="")
    service.translator = SimpleNamespace(translate=lambda s: s)
    service.is_enabled = True
    service.error_message = ""
    service.warning_message = ""
    service._engine = engine
    return service


class NewVideoServiceTests(TestCase):
    def test_all_three_are_connectable_and_named(self):
        for code, name in (("pp", "Paramount Plus"), ("hu", "Hulu"), ("tb", "Tubi")):
            self.assertIn(code, auth.SERVICES)
            self.assertEqual(auth.service_name(code), name)

    def test_all_three_have_a_browser_adapter(self):
        from bot import WEB_ADAPTERS
        from bot.services.web.hulu import HuluAdapter

        self.assertIs(WEB_ADAPTERS["pp"], ParamountAdapter)
        self.assertIs(WEB_ADAPTERS["hu"], HuluAdapter)
        self.assertIs(WEB_ADAPTERS["tb"], TubiAdapter)

    def test_hulu_needs_an_account_and_says_which_command_connects_it(self):
        from bot.services.browser_service import HuluService

        engine = Mock()
        engine.is_logged_in.return_value = False

        with self.assertRaises(errors.NotSignedInError) as caught:
            make_browser_service(HuluService, engine).search("film")

        self.assertIn("li hu", str(caught.exception))

    def test_a_search_result_scrape_is_given_each_sites_own_paths_and_scheme(self):
        from bot.services.web.hulu import HuluAdapter

        page = Mock()
        HuluAdapter().search(page, "the bear")

        self.assertEqual(page.goto.call_args.args[0], "https://www.hulu.com/search?q=the%20bear")
        title_paths, movie_paths, scheme = page.evaluate.call_args.args[1]
        self.assertIn("/series/", title_paths)
        self.assertEqual(scheme, "hulu://")

    def test_tubi_searches_without_an_account(self):
        engine = Mock()
        engine.is_logged_in.return_value = False
        engine.search.return_value = [{"id": "/movies/1", "title": "Film", "kind": "title", "url": "tubi://movies/1"}]

        tracks = make_browser_service(TubiService, engine).search("film")

        self.assertEqual(tracks[0].url, "tubi://movies/1")
        engine.is_logged_in.assert_not_called()

    def test_paramount_still_needs_an_account(self):
        engine = Mock()
        engine.is_logged_in.return_value = False

        with self.assertRaises(errors.NotSignedInError) as caught:
            make_browser_service(ParamountService, engine).search("film")

        self.assertIn("li pp", str(caught.exception))

    def test_a_pasted_link_and_a_search_result_both_reach_the_title_page(self):
        for adapter, pasted, result, expected in (
            (TubiAdapter(), "https://tubitv.com/movies/1/film", "tubi://movies/1/film", "https://tubitv.com/movies/1/film"),
            (ParamountAdapter(), "https://www.paramountplus.com/movies/video/abc/", "paramount://movies/video/abc",
             "https://www.paramountplus.com/movies/video/abc"),
            (__import__("bot.services.web.hulu", fromlist=["HuluAdapter"]).HuluAdapter(),
             "https://www.hulu.com/movie/film-1", "hulu://movie/film-1", "https://www.hulu.com/movie/film-1"),
        ):
            for url in (pasted, result):
                page = Mock()
                adapter.first_visible = Mock(return_value=None)
                adapter.play(page, SimpleNamespace(url=url))
                self.assertTrue(page.goto.call_args.args[0].startswith(expected), url)


class SpotifyConstructionTests(TestCase):
    def test_a_real_spotify_service_can_ask_for_a_token(self):
        """[080]: these three were set after a return and never ran, so search failed."""
        from bot.services.spotify import SpotifyService

        bot = SimpleNamespace(translator=SimpleNamespace(translate=lambda s: s))
        service = SpotifyService(bot, SimpleNamespace(enabled=True, client_id=""))

        self.assertEqual((service._token, service._token_at, service._store), ("", 0.0, None))
        with self.assertRaises(errors.ServiceError):
            service._access_token()  # no application set up: a sentence, not AttributeError


class LiOptionalSignInTests(TestCase):
    def test_tubi_is_listed_without_a_link_when_not_connected(self):
        portal = Mock()
        portal.statuses.return_value = {"nf": "disconnected", "tb": "disconnected"}
        portal.mint_link.side_effect = lambda username, path="/": f"https://bot.example{path}?t=tok"
        portal.link_advice.return_value = None
        command = object.__new__(LoginCommand)
        command.command_processor = SimpleNamespace(auth_portal=portal, youtube_session=None)
        command.translator = SimpleNamespace(translate=lambda s: s)

        result = command("", SimpleNamespace(username="alice"))

        self.assertIn("Tubi: plays without an account", result[0])
        self.assertEqual(result[1:], ["https://bot.example/connect/nf?t=tok"])
