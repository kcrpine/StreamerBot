"""dla: every album by an Apple Music artist, as one zip with a folder per album.

Runs without Chrome, gamdl or a TeamTalk server: gamdl's subprocess is replaced
by a function that writes the files gamdl would, so the zip layout the user asked
for (Artist/Album/track) is pinned exactly.
"""

import os
import tempfile
import threading
import unittest
import zipfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot import errors
from bot.commands.artist_choice import ArtistChoice
from bot.services.gamdl_downloader import (
    ARTIST_SELECTIONS,
    ARTIST_TIMEOUT_SECONDS,
    GamdlDownloader,
    artist_url,
    url_kind,
)

ARTIST = "https://music.apple.com/us/artist/coldplay/471744"
USER = SimpleNamespace(id=7, username="listener")


def translate(text):
    return text


def make_downloader(tmp):
    cookies = os.path.join(tmp, "cookies.txt")
    open(cookies, "w").close()
    return GamdlDownloader(cookies, tmp, SimpleNamespace(translate=translate))


def fake_gamdl(files, returncode=0, stderr=""):
    """A subprocess.run that writes files under gamdl's --output-path."""
    def run(command, **kwargs):
        work_dir = command[command.index("--output-path") + 1]
        for relative in files:
            path = os.path.join(work_dir, *relative.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"audio")
        run.command = command
        run.kwargs = kwargs
        return SimpleNamespace(returncode=returncode, stderr=stderr, stdout="")
    return run


class ArtistLinkTests(TestCase):
    def test_an_artist_link_is_recognised_and_its_query_dropped(self):
        self.assertEqual(url_kind(ARTIST), "artist")
        self.assertEqual(artist_url(ARTIST + "?l=en-GB"), ARTIST)

    def test_albums_songs_and_other_sites_are_not_artists(self):
        for url in (
            "https://music.apple.com/us/album/parachutes/1122782080",
            "https://music.apple.com/us/album/parachutes/1122782080?i=1122782283",
            "https://open.spotify.com/artist/4gzpq5DPGxSnKTe4SA8HAU",
            "Coldplay",
        ):
            self.assertIsNone(artist_url(url), url)


class DownloadArtistTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.downloader = make_downloader(self.tmp)
        available = patch.object(GamdlDownloader, "is_available", staticmethod(lambda: True))
        available.start()
        self.addCleanup(available.stop)

    def test_the_command_selects_without_a_prompt_and_puts_each_album_in_its_own_folder(self):
        command = self.downloader._artist_command(ARTIST, "/tmp/w", "main-albums")

        self.assertIsInstance(command, list)
        self.assertEqual(command[command.index("--artist-auto-select") + 1], "main-albums")
        self.assertEqual(command[command.index("--album-folder-template") + 1], "{album}")
        self.assertEqual(command[command.index("--compilation-folder-template") + 1], "{album}")
        self.assertEqual(command[-1], ARTIST)
        self.assertIn("--cookies-path", command)

    def test_the_zip_holds_the_artist_folder_with_a_folder_per_album(self):
        run = fake_gamdl(["Parachutes/01 Don't Panic.m4a", "Parachutes/02 Shiver.m4a",
                          "X&Y/01 Square One.m4a"])
        with patch("subprocess.run", run):
            path, albums, tracks, partial = self.downloader.download_artist(
                ARTIST, "main-albums", "Coldplay")

        self.assertEqual(os.path.basename(path), "Coldplay.zip")
        with zipfile.ZipFile(path) as zf:
            self.assertEqual(sorted(zf.namelist()), [
                "Coldplay/Parachutes/01 Don't Panic.m4a",
                "Coldplay/Parachutes/02 Shiver.m4a",
                "Coldplay/X&Y/01 Square One.m4a",
            ])
        self.assertEqual((albums, tracks, partial), (2, 3, False))
        self.assertFalse(run.kwargs["shell"])
        self.assertEqual(run.kwargs["timeout"], ARTIST_TIMEOUT_SECONDS)

    def test_a_single_track_is_still_a_zip_in_the_artist_folder(self):
        with patch("subprocess.run", fake_gamdl(["Yellow - Single/01 Yellow.m4a"])):
            path, albums, tracks, _ = self.downloader.download_artist(
                ARTIST, "singles-eps", "Coldplay")

        self.assertTrue(path.endswith(".zip"))
        self.assertEqual((albums, tracks), (1, 1))

    def test_an_unsafe_artist_name_makes_a_safe_folder(self):
        with patch("subprocess.run", fake_gamdl(["Back in Black/01 Hells Bells.m4a"])):
            path, *_ = self.downloader.download_artist(ARTIST, "main-albums", "AC/DC")

        with zipfile.ZipFile(path) as zf:
            self.assertEqual(zf.namelist(), ["AC_DC/Back in Black/01 Hells Bells.m4a"])

    def test_errors_with_some_tracks_still_upload_and_say_so(self):
        run = fake_gamdl(["Parachutes/01 Don't Panic.m4a"], returncode=1, stderr="Error downloading")
        with patch("subprocess.run", run):
            _, _, tracks, partial = self.downloader.download_artist(
                ARTIST, "all-albums", "Coldplay")

        self.assertEqual(tracks, 1)
        self.assertTrue(partial)

    def test_an_artist_with_none_of_that_kind_is_said_plainly(self):
        stderr = "Artist has no media of type (media ID: 471744): Live Albums"
        with patch("subprocess.run", fake_gamdl([], stderr=stderr)):
            with self.assertRaises(errors.ServiceError) as caught:
                self.downloader.download_artist(ARTIST, "main-albums", "Coldplay")

        self.assertEqual(str(caught.exception), "Apple Music lists none of those for this artist.")
        self.assertEqual(os.listdir(self.tmp), ["cookies.txt"])

    def test_a_non_artist_link_never_runs_gamdl(self):
        with patch("subprocess.run") as run:
            with self.assertRaises(errors.ServiceError):
                self.downloader.download_artist(
                    "https://music.apple.com/us/album/x/1", "main-albums", "X")
        run.assert_not_called()


class FindArtistTests(TestCase):
    def make(self, results):
        from bot.modules.apple_music_downloader import AppleMusicDownloader

        def search(query):
            if results is None:
                raise errors.NothingFoundError("")
            return results

        service = SimpleNamespace(now_playing=lambda: None, cookies=lambda: [], search=search)
        bot = SimpleNamespace(
            translator=SimpleNamespace(translate=translate),
            ttclient=Mock(),
            service_manager=SimpleNamespace(services={"am": service}),
            config_manager=SimpleNamespace(config_dir=tempfile.mkdtemp()),
        )
        return AppleMusicDownloader(bot, uploader=Mock())

    @staticmethod
    def result(kind, url, name):
        return SimpleNamespace(url=url, name=name, extra_info={"kind": kind})

    def test_a_name_takes_the_first_artist_result_over_songs_and_albums(self):
        downloader = self.make([
            self.result("track", "https://music.apple.com/us/album/a/1?i=2", "Yellow, by Coldplay"),
            self.result("album", "https://music.apple.com/us/album/parachutes/1", "Parachutes"),
            self.result("artist", ARTIST, "Coldplay"),
            self.result("artist", "https://music.apple.com/us/artist/other/9", "Other"),
        ])

        self.assertEqual(downloader.find_artist("coldplay"), (ARTIST, "Coldplay"))

    def test_a_link_is_used_as_given_without_searching(self):
        downloader = self.make(None)

        self.assertEqual(downloader.find_artist(ARTIST + "?l=en"), (ARTIST, "Coldplay"))

    def test_a_percent_encoded_link_name_is_decoded_for_speech(self):
        downloader = self.make(None)
        link = "https://music.apple.com/jp/artist/%E5%AE%87%E5%A4%9A%E7%94%B0/258535972"

        self.assertEqual(downloader.find_artist(link)[1], "宇多田")

    def test_a_busy_downloader_says_so_before_asking(self):
        downloader = self.make(None)
        self.assertIsNone(downloader.busy_message())
        downloader._busy.acquire()
        self.assertTrue(downloader.busy_message().endswith("send: dla"))

    def test_no_artist_found_names_what_was_searched(self):
        downloader = self.make(None)

        with self.assertRaises(errors.ServiceError) as caught:
            downloader.find_artist("zzqx")
        self.assertIn("zzqx", str(caught.exception))

    def test_an_album_link_points_to_dlp(self):
        downloader = self.make(None)

        with self.assertRaises(errors.ServiceError) as caught:
            downloader.find_artist("https://music.apple.com/us/album/parachutes/1")
        self.assertIn("dlp", str(caught.exception))

    def test_starting_runs_one_job_and_says_what_it_downloads(self):
        downloader = self.make(None)
        downloader._run_artist = Mock()

        message = downloader.start_artist(ARTIST, "Coldplay", "main-albums", USER)

        for thread in threading.enumerate():
            if thread.name == "AppleMusicDownload":
                thread.join(timeout=5)
        downloader._run_artist.assert_called_once_with(ARTIST, "Coldplay", "main-albums", USER)
        self.assertIn("main albums by Coldplay", message)
        self.assertIn("one zip file", message)

    def test_the_finish_message_counts_albums_and_tracks(self):
        downloader = self.make(None)
        downloader._write_cookies = lambda job_dir: os.path.join(job_dir, "cookies.txt")
        downloader.uploader.upload_file.return_value = True
        downloader._busy.acquire()

        with patch.object(GamdlDownloader, "download_artist",
                          return_value=("/x/Coldplay.zip", 9, 112, True)):
            downloader._run_artist(ARTIST, "Coldplay", "main-albums", USER)

        sent = downloader.ttclient.send_message.call_args[0][0]
        self.assertEqual(sent.split("\n"), [
            "The Coldplay download is in the channel's files, as one zip file.",
            "Albums: 9",
            "Tracks: 112",
            "Some tracks could not be downloaded.",
        ])
        self.assertTrue(downloader._busy.acquire(blocking=False))


class ArtistChoiceTests(TestCase):
    def make(self, clock=None):
        started = []
        choice = ArtistChoice("Coldplay", translate,
                              lambda selection: started.append(selection) or "Downloading",
                              **({"clock": clock} if clock else {}))
        return choice, started

    def test_the_question_names_the_artist_and_ends_each_line_with_its_number(self):
        choice, _ = self.make()
        lines = choice.question().split("\n")

        self.assertEqual(lines[0], "What do you want to download by Coldplay?")
        self.assertEqual([line[-1] for line in lines[1:]], ["1", "2", "3", "0"])

    def test_each_number_starts_its_selection(self):
        for number, selection in ARTIST_SELECTIONS.items():
            choice, started = self.make()
            self.assertEqual(choice.answer(str(number)), ("Downloading", False))
            self.assertEqual(started, [selection])

    def test_zero_cancels_and_an_unknown_number_asks_again(self):
        choice, started = self.make()
        self.assertEqual(choice.answer("0"), ("Cancelled.", False))
        reply, still_open = choice.answer("7")
        self.assertTrue(still_open)
        self.assertTrue(reply.startswith("That is not one of the choices.\n"))
        self.assertIn("Coldplay", reply)
        self.assertEqual(started, [])

    def test_words_are_not_an_answer(self):
        choice, _ = self.make()
        self.assertIsNone(choice.answer("p yellow"))

    def test_it_expires_after_half_an_hour(self):
        now = [0.0]
        choice, _ = self.make(clock=lambda: now[0])
        now[0] = 31 * 60
        self.assertTrue(choice.expired())


class ArtistRoutingTests(TestCase):
    def setUp(self):
        from bot.commands import CommandProcessor

        self.sent = []
        self.processor = SimpleNamespace(
            pending_ad_prompt={},
            pending_playlist_choice={},
            pending_artist_choice={},
            pending_playlist_download={},
            pending_artist_download={},
            pending_ads_option={},
            pending_podcast_count={},
            ttclient=SimpleNamespace(send_message=lambda text, user=None: self.sent.append(text)),
            translator=SimpleNamespace(translate=translate),
            parse_command=Mock(side_effect=errors.ParseCommandError()),
            check_access=Mock(return_value=False),
        )
        self.run = lambda text: CommandProcessor._run(
            self.processor, SimpleNamespace(user=USER, text=text)
        )

    def test_an_answer_goes_to_the_artist_question(self):
        choice, started = ArtistChoiceTests.make(self)
        self.processor.pending_artist_choice[USER.id] = choice

        self.run("1")

        self.assertEqual(started, ["main-albums"])
        self.assertEqual(self.sent, ["Downloading"])
        self.assertNotIn(USER.id, self.processor.pending_artist_choice)
        self.processor.parse_command.assert_not_called()

    def test_an_expired_question_points_back_to_dla(self):
        now = [0.0]
        choice, _ = ArtistChoiceTests.make(self, clock=lambda: now[0])
        self.processor.pending_artist_choice[USER.id] = choice
        now[0] = 31 * 60

        self.run("1")

        self.assertEqual(self.sent, ["That question has expired. To ask again, send: dla"])

    def test_dla_alone_waits_for_the_artist_in_the_next_message(self):
        from bot.commands.user_commands import DownloadArtistCommand

        command = object.__new__(DownloadArtistCommand)
        command.command_processor = self.processor
        command.translator = SimpleNamespace(translate=translate)

        self.assertTrue(command("", USER).endswith("To cancel, send: 0"))
        self.assertIn(USER.id, self.processor.pending_artist_download)

        self.run("Coldplay")

        self.processor.check_access.assert_called_once_with(USER, "dla")
        self.assertNotIn(USER.id, self.processor.pending_artist_download)
        self.processor.parse_command.assert_not_called()

    def test_zero_cancels_the_wait_for_an_artist(self):
        self.processor.pending_artist_download[USER.id] = True

        self.run("0")

        self.assertEqual(self.sent, ["Cancelled."])
        self.assertNotIn(USER.id, self.processor.pending_artist_download)
        self.processor.check_access.assert_not_called()


if __name__ == "__main__":
    unittest.main()
