"""Disney+, Apple Music, Amazon Music, and the gamdl download wrapper.

The adapter tests pin decision logic that is invisible in a screenshot and easy
to regress: which audio labels count as described, how search results are
ordered for someone listening rather than skimming, and that a shell is never
handed user input.
"""

import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot import errors
from bot.services.browser_service import (
    AmazonMusicService,
    AppleMusicService,
    BrowserService,
    DisneyService,
    KIND_ORDER,
)
from bot.services.gamdl_downloader import (
    GamdlDownloader,
    collection_url,
    has_media_user_token,
    is_apple_music_url,
    netscape_cookies,
    song_url,
    url_kind,
)
from bot.services.web.amazon_music import AmazonMusicAdapter
from bot.services.web.apple_music import AppleMusicAdapter
from bot.services.web.disney import DisneyAdapter


def make_service(cls, engine=None):
    service = object.__new__(cls)
    service.config = SimpleNamespace(enabled=True, profile="")
    service.translator = SimpleNamespace(translate=lambda s: s)
    service.is_enabled = True
    service.error_message = ""
    service.warning_message = ""
    # help is a property now, computed from the translator, so it is not set here.
    service._engine = engine
    return service


class DisneyAudioDescriptionTests(TestCase):
    """Disney abbreviates to AD in some regions, which is a trap."""

    def setUp(self):
        self.adapter = DisneyAdapter()

    def test_the_abbreviation_is_matched_as_a_whole_word(self):
        found = self.adapter.find_described_track(
            [{"id": "0", "label": "English"}, {"id": "1", "label": "English [AD]"}]
        )

        self.assertEqual(found["id"], "1")

    def test_a_bare_ad_substring_does_not_match(self):
        """"Standard" and "Broadcast" both contain "ad". Matching those would
        silently select the wrong audio track, which is worse than finding none."""
        tracks = [
            {"id": "0", "label": "English Standard"},
            {"id": "1", "label": "English Broadcast"},
            {"id": "2", "label": "Deutsch Standard"},
        ]

        self.assertIsNone(self.adapter.find_described_track(tracks))

    def test_the_spelled_out_form_still_matches(self):
        found = self.adapter.find_described_track(
            [{"id": "3", "label": "English - Audio Description"}]
        )

        self.assertEqual(found["id"], "3")


class MusicServicesHaveNoDescriptionTests(TestCase):
    """Music has no described track, so the capability is declared false rather
    than left to fail when someone asks for it."""

    def test_apple_music(self):
        self.assertFalse(AppleMusicService.supports_audio_description)
        self.assertEqual(AppleMusicAdapter().list_audio_tracks(Mock()), [])
        self.assertIsNone(AppleMusicAdapter().find_described_track([{"label": "Audio Description"}]))

    def test_amazon_music(self):
        self.assertFalse(AmazonMusicService.supports_audio_description)
        self.assertIsNone(AmazonMusicAdapter().find_described_track([{"label": "Described"}]))

    def test_disney_does_have_it(self):
        self.assertTrue(DisneyService.supports_audio_description)

    def test_asking_a_music_service_for_it_returns_false_rather_than_raising(self):
        service = make_service(AppleMusicService, engine=Mock())

        self.assertFalse(service.enable_audio_description())


class SearchOrderingTests(TestCase):
    def setUp(self):
        self.service = make_service(AppleMusicService)

    def test_containers_come_before_individual_tracks(self):
        """A listener hears every entry in sequence, so grouping is what makes
        the list navigable."""
        results = [
            {"title": "T1", "kind": "track"},
            {"title": "A1", "kind": "album"},
            {"title": "T2", "kind": "track"},
            {"title": "AR1", "kind": "artist"},
            {"title": "P1", "kind": "playlist"},
        ]

        kinds = [r["kind"] for r in self.service.order_results(results)]

        self.assertEqual(kinds, ["artist", "album", "playlist", "track", "track"])

    def test_the_services_own_ranking_is_preserved_within_a_kind(self):
        """The site's order is its answer to the query; re-sorting makes it worse."""
        results = [
            {"title": "second", "kind": "album"},
            {"title": "first", "kind": "album"},
            {"title": "third", "kind": "album"},
        ]

        titles = [r["title"] for r in self.service.order_results(results)]

        self.assertEqual(titles, ["second", "first", "third"])

    def test_a_top_result_leads(self):
        results = [
            {"title": "an album", "kind": "album"},
            {"title": "the best match", "kind": "top"},
        ]

        self.assertEqual(self.service.order_results(results)[0]["kind"], "top")

    def test_an_unexpected_kind_is_kept_rather_than_dropped(self):
        results = [{"title": "x", "kind": "podcast"}, {"title": "y", "kind": "album"}]

        ordered = self.service.order_results(results)

        self.assertEqual(len(ordered), 2)
        self.assertEqual(ordered[0]["kind"], "album")

    def test_each_line_names_its_kind_first(self):
        """Kind first is the word being listened for, so someone can stop
        reading once they hear it."""
        text = self.service.describe_results(
            [{"title": "Abbey Road", "kind": "album"}, {"title": "A Song", "kind": "track"}]
        )
        lines = text.split("\n")

        self.assertIn("1. Album: Abbey Road", lines)
        self.assertIn("2. Track: A Song", lines)

    def test_a_summary_of_counts_comes_first(self):
        text = self.service.describe_results(
            [{"title": "a", "kind": "album"}, {"title": "b", "kind": "album"}]
        )

        # Plural, because the summary is counting and "2 Album" read aloud is
        # wrong in a way a written list gets away with.
        self.assertTrue(text.split("\n")[0].startswith("2 Albums"))


class ServiceGuardTests(TestCase):
    def test_no_engine_names_the_architecture_problem(self):
        """On arm64 there is no Chrome; the message must say so rather than
        looking like a bug."""
        service = make_service(DisneyService, engine=None)

        with self.assertRaises(errors.EngineUnavailableError) as caught:
            service.search("anything")

        self.assertIn("Chrome", str(caught.exception))

    def test_not_signed_in_names_the_command_that_fixes_it(self):
        engine = Mock()
        engine.is_logged_in.return_value = False
        service = make_service(DisneyService, engine=engine)

        with self.assertRaises(errors.NotSignedInError) as caught:
            service.search("anything")

        self.assertIn("li dp", str(caught.exception))

    def test_downloading_a_streaming_service_is_refused_with_a_reason(self):
        service = make_service(DisneyService, engine=Mock())

        with self.assertRaises(errors.UnsupportedOperationError):
            service.download(Mock(), "/tmp/x")

    def test_an_empty_search_result_is_nothing_found_not_an_error(self):
        engine = Mock()
        engine.is_logged_in.return_value = True
        engine.search.return_value = []
        service = make_service(AmazonMusicService, engine=engine)

        with self.assertRaises(errors.NothingFoundError):
            service.search("nonsense")


class AppleUrlTests(TestCase):
    def test_recognised_links(self):
        for url in (
            "https://music.apple.com/us/album/abbey-road/1441164426",
            "https://music.apple.com/gb/playlist/todays-hits/pl.abc",
            "https://music.apple.com/us/artist/the-beatles/136975",
        ):
            self.assertTrue(is_apple_music_url(url), url)

    def test_other_links_are_rejected(self):
        for url in ("https://open.spotify.com/album/x", "https://example.com", "", "not a url"):
            self.assertFalse(is_apple_music_url(url), url)

    def test_a_song_inside_an_album_is_a_song_not_an_album(self):
        """This decides whether the result is one file or a zip."""
        self.assertEqual(
            url_kind("https://music.apple.com/us/album/abbey-road/1441164426?i=1441164427"),
            "song",
        )
        self.assertEqual(
            url_kind("https://music.apple.com/us/album/abbey-road/1441164426"),
            "album",
        )


def make_downloader(tmp):
    return GamdlDownloader(
        cookies_path=os.path.join(tmp, "cookies.txt"),
        output_dir=tmp,
        translator=SimpleNamespace(translate=lambda s: s),
    )


class GamdlTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.downloader = make_downloader(self.tmp)

    def test_the_command_is_a_list_so_no_shell_is_involved(self):
        """Track names come from user input; a shell string would be an
        injection hole."""
        command = self.downloader._command(
            'https://music.apple.com/us/album/x/1; rm -rf ~', "/tmp/w"
        )

        self.assertIsInstance(command, list)
        self.assertEqual(command[0], "gamdl")
        # The whole hostile string is one argument, not several.
        self.assertIn('https://music.apple.com/us/album/x/1; rm -rf ~', command)

    def test_missing_cookies_says_how_to_fix_it(self):
        with patch.object(GamdlDownloader, "is_available", staticmethod(lambda: True)):
            with self.assertRaises(errors.NotSignedInError) as caught:
                self.downloader.check_ready()

        self.assertIn("li am", str(caught.exception))

    def test_a_missing_binary_is_reported_clearly(self):
        with patch.object(GamdlDownloader, "is_available", staticmethod(lambda: False)):
            with self.assertRaises(errors.ServiceError):
                self.downloader.check_ready()

    def test_a_non_apple_link_is_refused_before_anything_runs(self):
        open(self.downloader.cookies_path, "w").close()
        with patch.object(GamdlDownloader, "is_available", staticmethod(lambda: True)):
            with patch("subprocess.run") as run:
                with self.assertRaises(errors.ServiceError):
                    self.downloader.download("https://open.spotify.com/album/x")
                run.assert_not_called()

    def test_failures_are_explained_without_echoing_stderr(self):
        """gamdl's stderr can carry the cookie path, which does not belong in a
        channel."""
        result = SimpleNamespace(
            returncode=1,
            stderr="Traceback: /home/streamer/data/secrets/cookies.txt not readable",
            stdout="",
        )

        message = self.downloader._explain_failure(result)

        self.assertNotIn("secrets", message)
        self.assertIn("li am", message)

    def test_a_subscription_problem_is_named(self):
        result = SimpleNamespace(returncode=1, stderr="No active subscription", stdout="")

        self.assertIn("subscription", self.downloader._explain_failure(result))

    def test_archive_names_are_safe_for_any_filesystem(self):
        files = [os.path.join(self.tmp, "AC/DC: Back?In*Black", "01.m4a")]

        name = self.downloader._archive_name(files, self.tmp, "https://music.apple.com/us/album/x/1")

        for bad in '<>:"/\\|?*':
            self.assertNotIn(bad, name)

    def test_a_single_track_is_not_zipped(self):
        """One file uploads as one file; only collections become archives."""
        work = tempfile.mkdtemp(dir=self.tmp)
        open(os.path.join(work, "only.m4a"), "w").close()

        self.assertEqual(len(self.downloader._collect_audio(work)), 1)

    def test_every_audio_file_is_collected_from_nested_folders(self):
        work = tempfile.mkdtemp(dir=self.tmp)
        os.makedirs(os.path.join(work, "Album"))
        for name in ("01.m4a", "02.mp3", "cover.jpg"):
            open(os.path.join(work, "Album", name), "w").close()

        found = self.downloader._collect_audio(work)

        self.assertEqual(len(found), 2)
        self.assertFalse(any(f.endswith(".jpg") for f in found))

    def test_the_zip_uses_relative_paths(self):
        """Absolute paths would scatter files on extract and leak the server's
        directory layout."""
        import zipfile

        work = tempfile.mkdtemp(dir=self.tmp)
        os.makedirs(os.path.join(work, "Album"))
        files = []
        for name in ("01.m4a", "02.m4a"):
            path = os.path.join(work, "Album", name)
            open(path, "w").close()
            files.append(path)

        archive = self.downloader._zip(files, work, "https://music.apple.com/us/album/x/1")

        with zipfile.ZipFile(archive) as zf:
            for entry in zf.namelist():
                self.assertFalse(os.path.isabs(entry), entry)
                self.assertNotIn("..", entry)


class DownloadingWhatIsPlayingTests(TestCase):
    """dl downloads the song MusicKit is playing; dlp the album or playlist."""

    SONG = "https://music.apple.com/us/album/a-head-full-of-dreams/1053933969?i=1053934216"
    ALBUM = "https://music.apple.com/us/album/a-head-full-of-dreams/1053933969"
    PLAYLIST = "https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb"

    def test_the_playing_song_is_its_catalog_url(self):
        self.assertEqual(song_url({"url": self.SONG, "catalog_id": "1053934216"}), self.SONG)

    def test_a_library_song_falls_back_to_its_catalog_id(self):
        """A library item's own id (i.…) is not something gamdl can fetch."""
        url = song_url({"id": "i.abc", "url": "", "catalog_id": "1053934216", "storefront": "gb"})

        self.assertEqual(url_kind(url), "song")
        self.assertTrue(url.startswith("https://music.apple.com/gb/song/"))
        self.assertTrue(url.endswith("/1053934216"))

    def test_a_song_with_no_catalog_id_cannot_be_downloaded(self):
        self.assertIsNone(song_url({"id": "i.abc", "url": "", "catalog_id": "", "storefront": "us"}))
        self.assertIsNone(song_url(None))

    def test_an_album_or_playlist_being_played_is_itself(self):
        self.assertEqual(collection_url(self.ALBUM), (self.ALBUM, "album"))
        self.assertEqual(collection_url(self.PLAYLIST), (self.PLAYLIST, "playlist"))

    def test_a_single_song_being_played_gives_the_album_it_is_on(self):
        self.assertEqual(collection_url(self.SONG), (self.ALBUM, "album"))

    def test_an_artist_falls_back_to_the_playing_song_album(self):
        artist = "https://music.apple.com/us/artist/coldplay/471744"

        self.assertEqual(collection_url(artist), (None, None))
        self.assertEqual(collection_url(artist, {"url": self.SONG}), (self.ALBUM, "album"))

    def test_cookies_load_as_gamdl_reads_them(self):
        from http.cookiejar import MozillaCookieJar

        cookies = [
            {"name": "media-user-token", "value": "tok", "domain": ".music.apple.com",
             "path": "/", "expires": 2000000000, "secure": True},
            # A session cookie: Playwright reports its expiry as -1.
            {"name": "itspod", "value": "30", "domain": ".apple.com",
             "path": "/", "expires": -1, "secure": True},
            {"name": "other", "value": "x", "domain": ".example.com", "path": "/", "expires": -1},
        ]
        path = os.path.join(tempfile.mkdtemp(), "cookies.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(netscape_cookies(cookies, now=1900000000))

        jar = MozillaCookieJar(path)
        jar.load()
        loaded = {c.name: c.value for c in jar}

        self.assertTrue(has_media_user_token(cookies))
        self.assertEqual(loaded, {"media-user-token": "tok", "itspod": "30"})

    def test_signed_out_cookies_have_no_token(self):
        self.assertFalse(has_media_user_token([{"name": "itspod", "value": "30"}]))
        self.assertFalse(has_media_user_token([{"name": "media-user-token", "value": ""}]))


class AppleMusicDownloaderTests(TestCase):
    """What dl and dlp choose while Apple Music plays, without Chrome or gamdl."""

    ALBUM = DownloadingWhatIsPlayingTests.ALBUM
    SONG = DownloadingWhatIsPlayingTests.SONG

    def make(self, now_playing):
        from bot.modules.apple_music_downloader import AppleMusicDownloader

        service = SimpleNamespace(now_playing=lambda: now_playing, cookies=lambda: [])
        bot = SimpleNamespace(
            translator=SimpleNamespace(translate=lambda s: s),
            ttclient=Mock(),
            service_manager=SimpleNamespace(services={"am": service}),
            config_manager=SimpleNamespace(config_dir=tempfile.mkdtemp()),
        )
        downloader = AppleMusicDownloader(bot, uploader=Mock())
        downloader._run = Mock()
        return downloader

    def started_url(self, downloader):
        # _run is started on a thread; wait for it so the call is recorded.
        import threading

        for thread in threading.enumerate():
            if thread.name == "AppleMusicDownload":
                thread.join(timeout=5)
        return downloader._run.call_args[0][0]

    PLAYING = {"title": "Birds", "artist": "Coldplay", "album": "A Head Full of Dreams",
               "url": SONG, "catalog_id": "1053934216", "storefront": "us"}

    def test_dl_during_an_album_downloads_the_song_playing_not_the_album(self):
        downloader = self.make(self.PLAYING)
        track = SimpleNamespace(url=self.ALBUM, name="A Head Full of Dreams")

        message = downloader.start(track, SimpleNamespace(), whole=False)

        self.assertEqual(self.started_url(downloader), self.SONG)
        self.assertIn("Birds, by Coldplay", message)
        self.assertIn("dlp", message)

    def test_dlp_during_a_song_downloads_its_album(self):
        downloader = self.make(self.PLAYING)
        track = SimpleNamespace(url=self.SONG, name="Birds")

        message = downloader.start(track, SimpleNamespace(), whole=True)

        self.assertEqual(self.started_url(downloader), self.ALBUM)
        self.assertIn("A Head Full of Dreams", message)

    def test_a_second_download_is_refused_while_one_runs(self):
        downloader = self.make(self.PLAYING)
        track = SimpleNamespace(url=self.ALBUM, name="")
        downloader._busy.acquire()

        message = downloader.start(track, SimpleNamespace(), whole=True)

        downloader._run.assert_not_called()
        self.assertIn("already running", message)

    def test_nothing_playing_in_musickit_is_said_plainly(self):
        downloader = self.make(None)

        message = downloader.start(SimpleNamespace(url=self.ALBUM, name=""), SimpleNamespace(), whole=False)

        downloader._run.assert_not_called()
        self.assertIn("not playing", message)

    def test_a_failed_download_cleans_up_and_frees_the_next_one(self):
        """The job directory holds the account's cookies, so it must go however the job ends."""
        from bot.modules.apple_music_downloader import AppleMusicDownloader

        downloader = self.make(self.PLAYING)
        del downloader._run  # the real one
        downloader._busy.acquire()
        cookies = [{"name": "media-user-token", "value": "tok", "domain": ".music.apple.com"}]
        downloader.bot.service_manager.services["am"].cookies = lambda: cookies
        user = SimpleNamespace()

        with patch.object(GamdlDownloader, "download", side_effect=errors.ServiceError("The download failed.")):
            AppleMusicDownloader._run(downloader, self.SONG, "Birds, by Coldplay", user)

        root = os.path.join(downloader.bot.config_manager.config_dir, "downloads")
        self.assertEqual(os.listdir(root), [])
        self.assertTrue(downloader._busy.acquire(blocking=False))
        sent = downloader.ttclient.send_message.call_args[0][0]
        self.assertIn("The download failed.", sent)
        downloader.uploader.upload_file.assert_not_called()

if __name__ == "__main__":
    unittest.main()
