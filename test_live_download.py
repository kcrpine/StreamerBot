"""ldd on a live broadcast: refuse it in words instead of recording forever.

On 2026-10-11 `ldd` on a live church service answered "Searching and
downloading..." and then nothing at all: ffmpeg was recording the broadcast to
a temporary file, 127 MB half an hour in, and would have gone on until the
service ended. A broadcast on air has no end to wait for.
"""

import subprocess
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from bot import errors
from bot.modules.uploader import Uploader
from bot.services.youtube_bridge import YouTubeBridge

LIVE_URL = "https://rr1---sn-x.googlevideo.com/videoplayback?expire=1&live=1&noclen=1&itag=140"
FILE_URL = "https://rr1---sn-x.googlevideo.com/videoplayback?expire=1&clen=3400000&itag=140"


def bridge_with_plan(plan):
    bridge = YouTubeBridge()
    bridge._post = lambda endpoint, **payload: plan
    return bridge


class BridgeDownloadTests(TestCase):
    def test_a_live_audio_url_is_refused_before_ffmpeg_starts(self):
        bridge = bridge_with_plan({"audio": {"url": LIVE_URL}})
        with patch.object(subprocess, "run") as run:
            with self.assertRaises(errors.LiveBroadcastError):
                bridge.download("https://www.youtube.com/live/abc", "/tmp/x.mp3")
        run.assert_not_called()

    def test_a_live_video_download_is_refused_too(self):
        bridge = bridge_with_plan({"video": {"url": LIVE_URL}, "audio": {"url": LIVE_URL}})
        with patch.object(subprocess, "run") as run:
            with self.assertRaises(errors.LiveBroadcastError):
                bridge.download("https://www.youtube.com/live/abc", "/tmp/x.mp4", video=True)
        run.assert_not_called()

    def test_a_live_broadcast_is_still_a_service_error(self):
        # Every caller that already catches ServiceError keeps reporting it.
        self.assertTrue(issubclass(errors.LiveBroadcastError, errors.ServiceError))

    def test_an_ordinary_file_downloads_with_a_stall_timeout(self):
        bridge = bridge_with_plan({"audio": {"url": FILE_URL}})
        with patch.object(subprocess, "run") as run:
            bridge.download("https://www.youtube.com/watch?v=abc", "/tmp/x.mp3")
        cmd = run.call_args.args[0]
        self.assertIn("-rw_timeout", cmd)
        # A protocol option only applies to the input it precedes.
        self.assertLess(cmd.index("-rw_timeout"), cmd.index("-i"))


class BridgeDownloadRelayTests(TestCase):
    """ffmpeg reading one open-ended request was throttled to about real time:
    an 8-hour 23-minute finished stream took hours, while the same URL in 10 MB
    ranges came at 14-21 MB/s. Downloads now read through the windowing relay."""

    def relayed(self, plan, video=False):
        bridge = bridge_with_plan(plan)
        seen = []

        def relay(url, headers):
            seen.append((url, headers))
            return f"http://127.0.0.1:4420/token{len(seen)}"

        with patch.object(subprocess, "run") as run:
            bridge.download("https://www.youtube.com/watch?v=abc", "/tmp/x", video=video, relay=relay)
        return run.call_args.args[0], seen

    def test_ffmpeg_reads_the_relay_not_googlevideo(self):
        cmd, seen = self.relayed({"audio": {"url": FILE_URL}, "http_headers": {"User-Agent": "UA"}})
        self.assertEqual(seen, [(FILE_URL, {"User-Agent": "UA"})])
        self.assertIn("http://127.0.0.1:4420/token1", cmd)
        self.assertFalse(any("googlevideo" in part for part in cmd))

    def test_a_video_download_relays_both_streams(self):
        video_url = FILE_URL.replace("itag=140", "itag=137")
        cmd, seen = self.relayed({"video": {"url": video_url}, "audio": {"url": FILE_URL}}, video=True)
        self.assertEqual([url for url, _ in seen], [video_url, FILE_URL])
        self.assertFalse(any("googlevideo" in part for part in cmd))

    def test_a_live_broadcast_is_refused_before_anything_is_relayed(self):
        bridge = bridge_with_plan({"audio": {"url": LIVE_URL}})
        seen = []
        with patch.object(subprocess, "run"):
            with self.assertRaises(errors.LiveBroadcastError):
                bridge.download("x", "/tmp/x", relay=lambda url, headers: seen.append(url) or url)
        self.assertEqual(seen, [])

    def test_without_a_relay_ffmpeg_reads_the_stream_directly(self):
        bridge = bridge_with_plan({"audio": {"url": FILE_URL}})
        with patch.object(subprocess, "run") as run:
            bridge.download("x", "/tmp/x")
        self.assertIn(FILE_URL, run.call_args.args[0])


class UploaderLiveMessageTests(TestCase):
    def test_the_user_is_told_the_link_is_live_and_what_to_do_instead(self):
        messages = []
        bot = SimpleNamespace(
            config=SimpleNamespace(),
            ttclient=SimpleNamespace(send_message=lambda text, user=None: messages.append(text)),
            translator=SimpleNamespace(translate=lambda text: text),
            youtube_session=None,
        )
        track = SimpleNamespace(name="[LIVE] Sunday service", service="yt", format="mp3")

        def refuse(directory, video=False):
            raise errors.LiveBroadcastError("live")

        track.download = refuse
        result = Uploader(bot)._download(track, "/tmp", False, user=None)

        self.assertIsNone(result)
        self.assertEqual(len(messages), 1)
        self.assertIn("[LIVE] Sunday service is a live broadcast", messages[0])
        self.assertIn("u command", messages[0])
