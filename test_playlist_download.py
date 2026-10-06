"""dlp on a YouTube playlist: renewing a stale sign-in, and picking one video.

What these pin is what failed in production: kuhao answered "Failed to download
any tracks" to every playlist because YouTube refused a stale session with
LOGIN_REQUIRED, which playback commands renew and dlp did not. Also the chat
contract (one message to start, one to finish) and the numbered picker, whose
control numbers must not move as the user pages.
"""

import os
import zipfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from bot import errors
from bot.commands.playlist_choice import NEXT_PAGE, PAGE_SIZE, WHOLE_PLAYLIST, PlaylistChoice
from bot.modules.playlist_uploader import PlaylistUploader
from bot.modules.youtube_session_keeper import (
    DownloadSignIn,
    RefreshResult,
    SignInLost,
)
from bot.player.enums import State, TrackType
from bot.player.track import Track

REFUSED = "YouTube.js bridge error: Unable to resolve stream; MWEB/signed-in: no streaming data (LOGIN_REQUIRED: Sign in to confirm you're not a bot)"


def translate(text):
    return text


class FakeTrack:
    """Stands in for Track: download writes a file, or raises what it is told to."""

    def __init__(self, name, service="yt", failures=None):
        self.name = name
        self.service = service
        self.format = ""
        self.type = TrackType.Dynamic
        self.extra_info = {"videoId": name}
        self.failures = list(failures or [])
        self.downloads = 0
        self.url_reads = 0

    @property
    def url(self):
        self.url_reads += 1
        return "resolved"

    def download(self, directory, video=False):
        self.downloads += 1
        if self.failures:
            raise self.failures.pop(0)
        path = os.path.join(directory, f"{self.name}.{self.format}")
        with open(path, "w") as f:
            f.write(self.name)
        return path


def make_keeper(result=RefreshResult.Alive, has_session=True, needs_sign_in=False):
    keeper = Mock()
    keeper.is_refreshing = False
    keeper.store.has_session.return_value = has_session
    keeper.store.needs_sign_in.return_value = needs_sign_in
    keeper.on_login_required.return_value = result
    return keeper


class UploadRecorder:
    def __init__(self):
        self.zips = []

    def upload_file(self, path, user):
        with zipfile.ZipFile(path) as archive:
            self.zips.append(sorted(archive.namelist()))
        return True


def make_uploader(keeper):
    messages = []
    ttclient = SimpleNamespace(send_message=lambda text, user=None, *a: messages.append(text))
    bot = SimpleNamespace(
        config=SimpleNamespace(),
        ttclient=ttclient,
        translator=SimpleNamespace(translate=translate),
        youtube_session=keeper,
    )
    recorder = UploadRecorder()
    return PlaylistUploader(bot, recorder), recorder, messages


USER = SimpleNamespace(id=7, username="listener")


class PlaylistUploaderSignInTests(TestCase):
    def test_a_stale_session_is_renewed_once_and_every_track_downloads(self):
        keeper = make_keeper()
        tracks = [FakeTrack("a", failures=[errors.ServiceError(REFUSED)]), FakeTrack("b"), FakeTrack("c")]
        uploader, recorder, messages = make_uploader(keeper)

        uploader.run(tracks, USER, "Masses")

        keeper.on_login_required.assert_called_once()
        self.assertEqual(recorder.zips, [["Masses/a.mp3", "Masses/b.mp3", "Masses/c.mp3"]])
        self.assertEqual(len(messages), 3, messages)  # start, renewing, finish
        self.assertIn("Renewing", messages[1])
        self.assertEqual(messages[2], "Masses is in the channel as one zip file. Tracks in it: 3.")

    def test_a_session_google_ended_stops_with_the_sign_in_command(self):
        keeper = make_keeper(result=RefreshResult.Ended)
        tracks = [FakeTrack("a", failures=[errors.ServiceError(REFUSED)]), FakeTrack("b")]
        uploader, recorder, messages = make_uploader(keeper)

        uploader.run(tracks, USER, "Masses")

        self.assertEqual(recorder.zips, [])
        self.assertTrue(messages[-1].endswith("send this command: li yt"))
        self.assertEqual(tracks[1].downloads, 0)

    def test_a_refusal_that_survives_renewal_stops_the_job(self):
        keeper = make_keeper()
        refused = errors.ServiceError(REFUSED)
        tracks = [FakeTrack("a", failures=[refused, refused]), FakeTrack("b")]
        uploader, recorder, messages = make_uploader(keeper)

        uploader.run(tracks, USER, "Masses")

        self.assertEqual(recorder.zips, [])
        self.assertEqual(messages[-1], "The YouTube sign-in could not be renewed, so Masses was not downloaded.")
        self.assertEqual(tracks[1].downloads, 0)

    def test_a_signed_out_bot_says_so_before_downloading_anything(self):
        keeper = make_keeper(needs_sign_in=True)
        tracks = [FakeTrack("a")]
        uploader, recorder, messages = make_uploader(keeper)

        uploader.run(tracks, USER, "Masses")

        self.assertEqual(tracks[0].downloads, 0)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].endswith("li yt"))

    def test_youtube_tracks_are_not_resolved_before_downloading(self):
        tracks = [FakeTrack("a"), FakeTrack("b")]
        uploader, recorder, messages = make_uploader(make_keeper())

        uploader.run(tracks, USER, "Masses")

        self.assertEqual([t.url_reads for t in tracks], [0, 0])
        self.assertEqual([t.format for t in tracks], ["mp3", "mp3"])

    def test_other_failures_are_counted_in_the_finish_message(self):
        tracks = [FakeTrack("a"), FakeTrack("b", failures=[errors.ServiceError("This video is private")])]
        uploader, recorder, messages = make_uploader(make_keeper())

        uploader.run(tracks, USER, "Masses")

        self.assertEqual(recorder.zips, [["Masses/a.mp3"]])
        self.assertEqual(len(messages), 2, messages)
        self.assertEqual(
            messages[-1],
            "Masses is in the channel as one zip file. Tracks in it: 1. "
            "Tracks that could not be downloaded: 1.",
        )

    def test_nothing_downloaded_says_how_many_were_tried(self):
        tracks = [FakeTrack("a", failures=[errors.ServiceError("private")])]
        uploader, recorder, messages = make_uploader(make_keeper())

        uploader.run(tracks, USER, "Masses")

        self.assertEqual(messages[-1], "No track in Masses could be downloaded. Tracks tried: 1.")


class DownloadSignInTests(TestCase):
    def test_without_a_keeper_errors_pass_through(self):
        sign_in = DownloadSignIn(None, on_renewing=Mock())
        with self.assertRaises(errors.ServiceError):
            sign_in.run(Mock(side_effect=errors.ServiceError(REFUSED)))

    def test_a_bot_with_no_session_is_told_to_connect_one(self):
        sign_in = DownloadSignIn(make_keeper(has_session=False), on_renewing=Mock())
        with self.assertRaises(SignInLost) as caught:
            sign_in.run(Mock(side_effect=errors.ServiceError(REFUSED)))
        self.assertIs(caught.exception.result, RefreshResult.NoSession)


def make_choice(count):
    tracks = [SimpleNamespace(name=f"Video {n}", extra_info={"uploader": "Parish"}) for n in range(1, count + 1)]
    whole = Mock()
    one = Mock(side_effect=lambda track: f"Downloading {track.name}")
    return PlaylistChoice(tracks, "Masses", translate, whole, one), whole, one


class PlaylistChoiceTests(TestCase):
    def test_1_downloads_the_whole_playlist(self):
        choice, whole, one = make_choice(5)
        self.assertEqual(choice.question().split("\n"), [
            "The playlist Masses has 5 videos.",
            "For the whole playlist as one zip file, send: 1",
            "To pick one video from a list, send: 2",
            "To cancel, send: 0",
        ])

        self.assertEqual(choice.answer("1"), ("", False))
        whole.assert_called_once()

    def test_2_lists_twenty_videos_and_the_fixed_options(self):
        choice, whole, one = make_choice(45)

        reply, still_open = choice.answer("2")

        lines = reply.split("\n")
        self.assertTrue(still_open)
        self.assertEqual(lines[0], "Page 1 of 3 of Masses, 45 videos. Send a number:")
        self.assertEqual(lines[1], "1. Video 1, by Parish")
        self.assertEqual(lines[20], "20. Video 20, by Parish")
        self.assertEqual(lines[21:], [f"{NEXT_PAGE}. Next videos, 21 to 40", f"{WHOLE_PLAYLIST}. Download the whole playlist", "0. Cancel"])

    def test_21_pages_on_and_the_numbers_stay_put(self):
        choice, whole, one = make_choice(45)
        choice.answer("2")

        reply, _ = choice.answer(str(NEXT_PAGE))
        self.assertTrue(reply.startswith("Page 2 of 3 of Masses"))
        self.assertIn("1. Video 21, by Parish", reply.split("\n"))
        self.assertIn(f"{NEXT_PAGE}. Next videos, 41 to 45", reply.split("\n"))

        reply, _ = choice.answer(str(NEXT_PAGE))
        self.assertTrue(reply.startswith("Page 3 of 3 of Masses"))
        self.assertIn(f"{NEXT_PAGE}. Back to videos 1 to 20", reply.split("\n"))

        reply, _ = choice.answer(str(NEXT_PAGE))
        self.assertTrue(reply.startswith("Page 1 of 3 of Masses"))

    def test_a_number_downloads_that_video_from_the_page_shown(self):
        choice, whole, one = make_choice(45)
        choice.answer("2")
        choice.answer(str(NEXT_PAGE))

        self.assertEqual(choice.answer("3"), ("Downloading Video 23", False))

    def test_22_downloads_the_whole_playlist_from_any_page(self):
        choice, whole, one = make_choice(45)
        choice.answer("2")
        choice.answer(str(NEXT_PAGE))

        self.assertEqual(choice.answer(str(WHOLE_PLAYLIST)), ("", False))
        whole.assert_called_once()

    def test_a_short_playlist_has_no_next_option(self):
        choice, whole, one = make_choice(4)
        reply, _ = choice.answer("2")

        self.assertNotIn(f"{NEXT_PAGE}.", reply)
        self.assertEqual(
            choice.answer(str(NEXT_PAGE)),
            ("There is no 21 on this page. Send a number from 1 to 4, or to cancel, send: 0", True),
        )

    def test_out_of_range_keeps_the_question_open(self):
        choice, whole, one = make_choice(4)
        choice.answer("2")

        self.assertEqual(
            choice.answer("9"),
            ("There is no 9 on this page. Send a number from 1 to 4, or to cancel, send: 0", True),
        )
        one.assert_not_called()

    def test_0_cancels_and_words_are_not_an_answer(self):
        choice, whole, one = make_choice(4)
        self.assertIsNone(choice.answer("p something else"))
        self.assertEqual(choice.answer("0"), ("Cancelled.", False))

    def test_an_old_question_expires(self):
        now = [0.0]
        tracks = [SimpleNamespace(name="v", extra_info={})]
        choice = PlaylistChoice(tracks, "Masses", translate, Mock(), Mock(), clock=lambda: now[0])
        now[0] = 31 * 60
        self.assertTrue(choice.expired())


class CommandRoutingTests(TestCase):
    def setUp(self):
        from bot.commands import CommandProcessor

        self.sent = []
        self.processor = SimpleNamespace(
            pending_ad_prompt={},
            pending_playlist_choice={},
            pending_playlist_download={},
            pending_ads_option={},
            ttclient=SimpleNamespace(send_message=lambda text, user=None: self.sent.append(text)),
            translator=SimpleNamespace(translate=translate),
            parse_command=Mock(side_effect=errors.ParseCommandError()),
        )
        self.run = lambda text: CommandProcessor._run(
            self.processor, SimpleNamespace(user=USER, text=text)
        )

    def test_an_answer_goes_to_the_question_and_keeps_it_open(self):
        choice, whole, one = make_choice(30)
        self.processor.pending_playlist_choice[USER.id] = choice

        self.run("2")

        self.assertIs(self.processor.pending_playlist_choice[USER.id], choice)
        self.assertTrue(self.sent[0].startswith("Page 1 of 2 of Masses"))
        self.processor.parse_command.assert_not_called()

    def test_a_number_after_the_question_expired_says_so(self):
        now = [0.0]
        choice = PlaylistChoice([SimpleNamespace(name="v", extra_info={})], "Masses", translate,
                                Mock(), Mock(), clock=lambda: now[0])
        self.processor.pending_playlist_choice[USER.id] = choice
        now[0] = 31 * 60

        self.run("2")

        self.assertEqual(self.sent, ["That question has expired. To ask again, send: dlp"])
        self.assertNotIn(USER.id, self.processor.pending_playlist_choice)
        self.processor.parse_command.assert_not_called()

    def test_anything_else_drops_the_question_and_runs_as_a_command(self):
        choice, whole, one = make_choice(30)
        self.processor.pending_playlist_choice[USER.id] = choice

        self.run("v 50")

        self.assertNotIn(USER.id, self.processor.pending_playlist_choice)
        self.processor.parse_command.assert_called_once_with("v 50")


class PlayingPlaylistTests(TestCase):
    def test_no_link_dlp_takes_the_playlist_and_leaves_out_autoplay(self):
        from bot.commands.user_commands import DownloadPlaylistCommand

        def entry(video_id, playlist_title="Masses"):
            info = {"videoId": video_id, "uploader": "Parish"}
            if playlist_title:
                info["playlist_title"] = playlist_title
            return Track(service="yt", url=f"https://www.youtube.com/watch?v={video_id}",
                         name=video_id, extra_info=info, type=TrackType.Dynamic)

        track_list = [entry("aaaaaaaaaaa"), entry("bbbbbbbbbbb"), entry("ccccccccccc", playlist_title=None)]
        player = SimpleNamespace(state=State.Playing, is_playlist=True, track=track_list[0], track_list=track_list)
        playlist_uploader = Mock()
        playlist_uploader.get_status.return_value = None
        processor = SimpleNamespace(
            bot=None, cache=None, cache_manager=None, config=None, config_manager=None,
            module_manager=SimpleNamespace(playlist_uploader=playlist_uploader, uploader=Mock()),
            player=player, service_manager=None, task_processor=None,
            ttclient=Mock(), translator=SimpleNamespace(translate=translate),
            pending_playlist_choice={}, pending_playlist_download={},
        )
        command = DownloadPlaylistCommand(processor)

        reply = command("", USER)

        self.assertIn("The playlist Masses has 2 videos", reply)
        processor.pending_playlist_choice[USER.id].answer("1")
        tracks, user, name = playlist_uploader.call_args.args
        self.assertEqual([t.extra_info["videoId"] for t in tracks], ["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertTrue(all(t is not q for t in tracks for q in track_list))
        self.assertEqual(name, "Masses")


if __name__ == "__main__":
    import unittest

    unittest.main()
