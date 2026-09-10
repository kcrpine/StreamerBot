"""loadfile's argument order has to match the libmpv actually loaded.

mpv 0.38 inserted an <index> argument between <flags> and <options>. The
vendored binding still passed its options string third, so against Debian 13's
mpv 0.40 every load raised MPV_ERROR_INVALID_PARAMETER and the bot answered a
request with "Invalid value for mpv parameter" instead of playing.
"""

import time
import unittest

try:
    import mpv
    from mpv import _mpv_client_api_version
except (OSError, ImportError) as error:  # pragma: no cover - host without libmpv
    raise unittest.SkipTest(f"libmpv is not loadable here: {error}")


class LoadfileArgumentTests(unittest.TestCase):
    """What gets handed to mpv_command, without starting a player."""

    class Recorder:
        """Stands in for a player. loadfile touches nothing but self.command.

        A real MPV instance is wrong here: its __getattr__ forwards unknown
        attributes to mpv properties, so a half-built one recurses instead of
        failing.
        """

        def __init__(self):
            self.calls = []

        def command(self, *args):
            self.calls.append(args)

    def setUp(self):
        self.recorder = self.Recorder()
        self.calls = self.recorder.calls

    def loadfile(self, *args, **kwargs):
        mpv.MPV.loadfile(self.recorder, *args, **kwargs)

    def test_no_options_sends_the_short_form(self):
        """The bot's own path. Every mpv ever released accepts this form.

        The trailing empty options string is what mpv 0.38+ tried to read as
        an index, so it must not be sent at all.
        """
        self.loadfile("http://example.invalid/a.mp3")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            self.calls[0], ("loadfile", b"http://example.invalid/a.mp3", "replace")
        )

    def test_options_are_positioned_after_the_index(self):
        self.loadfile("http://example.invalid/a.mp3", "replace", start=5)
        name, filename, mode, *rest = self.calls[0]
        self.assertEqual((name, mode), ("loadfile", "replace"))
        if _mpv_client_api_version() >= mpv.MPV._LOADFILE_INDEX_API_VERSION:
            self.assertEqual(rest, ["-1", "start=5"])
        else:
            self.assertEqual(rest, ["start=5"])

    def test_the_mode_is_preserved(self):
        self.loadfile("http://example.invalid/a.mp3", "append")
        self.assertEqual(self.calls[0][2], "append")


class LoadfileAgainstRealMpvTests(unittest.TestCase):
    """The test that would actually have caught this: talk to libmpv.

    The argument-order tests above only encode an assumption about what mpv
    wants. This one asks mpv.
    """

    SOURCE = "av://lavfi:sine=frequency=1000:duration=1"

    def setUp(self):
        self.player = mpv.MPV(vo="null", ao="null", video=False)
        self.addCleanup(self.player.terminate)

    def test_play_is_accepted(self):
        # A rejected load raises ValueError("Invalid value for mpv parameter").
        self.player.play(self.SOURCE)
        ended = []
        self.player.event_callback("end-file")(lambda event: ended.append(event))
        for _ in range(100):
            if ended:
                break
            time.sleep(0.05)
        self.assertTrue(ended, "mpv accepted the load but never played it")

    def test_loadfile_with_per_file_options_is_accepted(self):
        self.player.loadfile(self.SOURCE, "replace", start=0)

    def test_append_with_per_file_options_is_accepted(self):
        self.player.loadfile(self.SOURCE, "replace", start=0)
        self.player.loadfile(self.SOURCE, "append", start=0)


if __name__ == "__main__":
    unittest.main()
