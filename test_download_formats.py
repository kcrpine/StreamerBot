"""dl, dlp, dla: the file format question, and converting gamdl's M4A into it.

The question and the routing run anywhere. The conversion tests run a real
ffmpeg on a generated second of audio, so they show each format actually comes
out playable, not only that a command line was built; they skip where ffmpeg is
missing (the image has it). No Apple Music account is involved: gamdl's own
download is not exercised here.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from bot.commands import CommandProcessor, answer_open_question
from bot.commands.format_choice import EXPIRES_AFTER_SECONDS, FormatChoice
from bot.services.gamdl_downloader import FORMAT_NAMES, FORMATS, convert_files, format_name

USER = SimpleNamespace(id=7, username="listener")
SONG = "https://music.apple.com/us/album/a-head-full-of-dreams/1053933969?i=1053934216"
ALBUM = "https://music.apple.com/us/album/a-head-full-of-dreams/1053933969"
PLAYING = {"title": "Birds", "artist": "Coldplay", "album": "A Head Full of Dreams",
           "url": SONG, "catalog_id": "1053934216", "storefront": "us"}


def translate(text):
    return text


def join_download_threads():
    for thread in threading.enumerate():
        if thread.name == "AppleMusicDownload":
            thread.join(timeout=5)


class FormatChoiceTests(TestCase):
    def make(self, clock=None):
        started = []

        def start(fmt):
            started.append(fmt)
            return f"Downloading as {fmt}"

        kwargs = {"clock": clock} if clock else {}
        return FormatChoice("dlp", translate, start, **kwargs), started

    def test_every_line_ends_with_the_number_to_send_and_names_are_in_capitals(self):
        choice, _ = self.make()
        lines = choice.question().split("\n")

        self.assertEqual(lines[0], "Which file format do you want?")
        self.assertTrue(all(line.rsplit(": ", 1)[1].isdigit() for line in lines[1:]))
        self.assertEqual(lines[-1], "To cancel, send: 0")
        for name in FORMAT_NAMES.values():
            self.assertIn(f"For {name}", choice.question())

    def test_each_number_starts_that_format(self):
        for number, fmt in FORMATS.items():
            choice, started = self.make()
            reply, still_open = choice.answer(str(number))
            self.assertEqual(started, [fmt])
            self.assertFalse(still_open)

    def test_zero_cancels_and_an_unknown_number_asks_again(self):
        choice, started = self.make()
        self.assertEqual(choice.answer("0"), ("Cancelled.", False))

        reply, still_open = choice.answer("9")
        self.assertTrue(still_open)
        self.assertTrue(reply.startswith("That is not one of the choices."))
        self.assertEqual(started, [])

    def test_words_are_not_an_answer(self):
        choice, _ = self.make()
        self.assertIsNone(choice.answer("p coldplay"))

    def test_it_expires_after_half_an_hour_and_names_the_command(self):
        now = [0.0]
        choice, _ = self.make(clock=lambda: now[0])
        now[0] = EXPIRES_AFTER_SECONDS + 1

        self.assertTrue(choice.expired())
        self.assertEqual(
            choice.expired_message(),
            "The file format question has expired. To ask again, send: dlp",
        )

    def test_an_expired_question_words_its_own_message(self):
        now = [0.0]
        choice, _ = self.make(clock=lambda: now[0])
        now[0] = EXPIRES_AFTER_SECONDS + 1
        sent = []
        ttclient = SimpleNamespace(send_message=lambda text, user=None: sent.append(text))

        handled = answer_open_question(
            ttclient, {USER.id: choice}, SimpleNamespace(user=USER, text="2"), None
        )

        self.assertTrue(handled)
        self.assertEqual(sent, ["The file format question has expired. To ask again, send: dlp"])


class FormatRoutingTests(TestCase):
    def test_a_number_goes_to_the_format_question(self):
        sent = []
        choice, started = FormatChoiceTests.make(self)
        processor = SimpleNamespace(
            pending_ad_prompt={}, pending_playlist_choice={}, pending_artist_choice={},
            pending_format_choice={USER.id: choice}, pending_config_menu={},
            pending_playlist_download={}, pending_artist_download={}, pending_ads_option={},
            pending_podcast_count={},
            ttclient=SimpleNamespace(send_message=lambda text, user=None: sent.append(text)),
            translator=SimpleNamespace(translate=translate),
            parse_command=Mock(), check_access=Mock(return_value=False),
        )

        CommandProcessor._run(processor, SimpleNamespace(user=USER, text="2"))

        self.assertEqual(started, ["mp3"])
        self.assertEqual(sent, ["Downloading as mp3"])
        processor.parse_command.assert_not_called()


class AskingTheFormatTests(TestCase):
    def make(self, now_playing=PLAYING):
        from bot.modules.apple_music_downloader import AppleMusicDownloader

        service = SimpleNamespace(now_playing=lambda: now_playing, cookies=lambda: [])
        bot = SimpleNamespace(
            translator=SimpleNamespace(translate=translate),
            ttclient=Mock(),
            service_manager=SimpleNamespace(services={"am": service}),
            config_manager=SimpleNamespace(config_dir=tempfile.mkdtemp()),
        )
        downloader = AppleMusicDownloader(bot, uploader=Mock())
        downloader._run = Mock()
        downloader._run_artist = Mock()
        return downloader

    def test_dlp_settles_the_album_first_and_starts_it_in_the_chosen_format(self):
        downloader = self.make()
        registered = []

        question = downloader.ask_format(
            SimpleNamespace(url=SONG, name="Birds"), USER, True, registered.append
        )
        self.assertTrue(question.startswith("Which file format do you want?"))
        downloader._run.assert_not_called()

        reply, _ = registered[0].answer("2")
        join_download_threads()

        downloader._run.assert_called_once_with(ALBUM, "A Head Full of Dreams", USER, "mp3")
        self.assertIn("as MP3", reply)
        self.assertIn("channel's files", reply)

    def test_a_busy_downloader_refuses_before_asking(self):
        downloader = self.make()
        downloader._busy.acquire()
        registered = []

        message = downloader.ask_format(
            SimpleNamespace(url=ALBUM, name=""), USER, False, registered.append
        )

        self.assertEqual(registered, [])
        self.assertTrue(message.endswith("send: dl"), message)

    def test_nothing_to_download_is_said_instead_of_asking(self):
        downloader = self.make(now_playing=None)
        registered = []

        message = downloader.ask_format(
            SimpleNamespace(url=ALBUM, name=""), USER, False, registered.append
        )

        self.assertEqual(registered, [])
        self.assertIn("not playing", message)

    def test_dla_asks_the_format_after_the_releases(self):
        downloader = self.make(now_playing=None)
        registered = []
        artist = "https://music.apple.com/us/artist/coldplay/471744"

        downloader.ask_artist_format(artist, "Coldplay", "main-albums", USER, registered.append)
        reply, _ = registered[0].answer("5")
        join_download_threads()

        downloader._run_artist.assert_called_once_with(artist, "Coldplay", "main-albums", USER, "flac")
        self.assertIn("as FLAC files in one zip file", reply)

    def test_tracks_left_as_m4a_are_named_in_the_finish_message(self):
        from bot.modules.apple_music_downloader import AppleMusicDownloader
        from bot.services.gamdl_downloader import GamdlDownloader
        from unittest.mock import patch

        downloader = self.make()
        del downloader._run
        downloader._busy.acquire()
        downloader._write_cookies = lambda job_dir: os.path.join(job_dir, "cookies.txt")
        downloader.uploader.upload_file.return_value = True

        def fake_download(self, url, fmt):
            self.unconverted = 2
            return "/x/A Head Full of Dreams.zip", True, 11

        with patch.object(GamdlDownloader, "download", fake_download):
            AppleMusicDownloader._run(downloader, ALBUM, "A Head Full of Dreams", USER, "opus")

        sent = downloader.ttclient.send_message.call_args[0][0]
        self.assertEqual(sent.split("\n")[1],
                         "Some tracks could not be converted to Opus, so they are M4A files instead.")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class ConversionTests(TestCase):
    # What ffprobe should report for each format's audio stream.
    CODECS = {"m4a": "aac", "mp3": "mp3", "opus": "opus", "ogg": "vorbis",
              "flac": "flac", "wav": "pcm_s16le", "aac": "aac"}

    def make_m4a(self, directory, name="01 Birds.m4a"):
        path = os.path.join(directory, name)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", "-c:a", "aac", "-metadata", "title=Birds", path],
            check=True,
        )
        return path

    def probe(self, path):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name:stream_tags=title:format_tags=title",
             "-of", "json", path],
            capture_output=True, text=True, check=True,
        ).stdout
        return json.loads(out)

    def test_every_format_comes_out_playable_and_keeps_its_title(self):
        for fmt in FORMATS.values():
            with self.subTest(fmt=fmt):
                directory = tempfile.mkdtemp()
                source = self.make_m4a(directory)

                files, failed = convert_files([source], fmt)

                self.assertEqual(failed, 0)
                self.assertEqual(len(files), 1)
                info = self.probe(files[0])
                self.assertEqual(info["streams"][0]["codec_name"], self.CODECS[fmt])
                if fmt == "m4a":
                    self.assertEqual(files, [source])
                else:
                    self.assertTrue(files[0].endswith("." + fmt))
                    self.assertFalse(os.path.exists(source), "the M4A should be replaced")
                if fmt in ("mp3", "flac", "ogg", "opus"):
                    # Ogg and Opus keep tags on the audio stream, the others on the file.
                    tags = {}
                    for holder in (info.get("format", {}), info["streams"][0]):
                        tags.update({k.lower(): v for k, v in holder.get("tags", {}).items()})
                    self.assertEqual(tags.get("title"), "Birds")

    def test_a_track_that_will_not_convert_stays_m4a_and_is_counted(self):
        directory = tempfile.mkdtemp()
        good = self.make_m4a(directory, "01 Good.m4a")
        broken = os.path.join(directory, "02 Broken.m4a")
        with open(broken, "wb") as f:
            f.write(b"not audio at all")

        files, failed = convert_files([good, broken], "mp3")

        self.assertEqual(failed, 1)
        self.assertEqual(sorted(os.path.basename(f) for f in files), ["01 Good.mp3", "02 Broken.m4a"])


class FormatNameTests(TestCase):
    def test_names_are_spoken_in_capitals(self):
        self.assertEqual(format_name("flac"), "FLAC")
        self.assertEqual(format_name("ogg"), "Ogg")


if __name__ == "__main__":
    unittest.main()
