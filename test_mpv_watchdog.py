import logging
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot.player import MPV_UNRESPONSIVE_EXIT_CODE, Player
from bot.player.mpv_watchdog import MpvWatchdog, format_all_thread_stacks


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class MpvWatchdogTests(TestCase):
    def make(self, probe=lambda: True, timeout=60.0):
        clock = FakeClock()
        fired = Mock()
        dog = MpvWatchdog(probe, fired, interval=15.0, timeout=timeout, clock=clock)
        return dog, clock, fired

    def test_answered_question_never_fires(self):
        dog, clock, fired = self.make()
        dog.ask()
        clock.now += 3600
        self.assertFalse(dog.check())
        fired.assert_not_called()

    def test_probe_error_counts_as_an_answer(self):
        # A terminated mpv raises rather than blocking; that is not a stopped core.
        dog, clock, fired = self.make(probe=Mock(side_effect=RuntimeError("gone")))
        dog.ask()
        clock.now += 3600
        self.assertFalse(dog.check())
        fired.assert_not_called()

    def test_unanswered_question_fires_once_past_the_timeout(self):
        release = threading.Event()
        dog, clock, fired = self.make(probe=release.wait)
        asker = threading.Thread(target=dog.ask, daemon=True)
        asker.start()
        try:
            for _ in range(200):
                if dog._asked_at is not None:
                    break
                threading.Event().wait(0.01)
            clock.now += 59
            self.assertFalse(dog.check())
            clock.now += 2
            self.assertTrue(dog.check())
            fired.assert_called_once_with(61)
            clock.now += 60
            self.assertFalse(dog.check())
            fired.assert_called_once()
        finally:
            release.set()
            asker.join(2)

    def test_stack_dump_names_threads(self):
        self.assertIn(threading.current_thread().name, format_all_thread_stacks())


class PlayerMpvLogTests(TestCase):
    def make_player(self):
        player = object.__new__(Player)
        player._log_level = 5
        return player

    def test_mpv_errors_and_warnings_reach_the_log(self):
        player = self.make_player()
        for mpv_level, expected in (
            ("fatal", logging.CRITICAL),
            ("error", logging.ERROR),
            ("warn", logging.WARNING),
            ("info", logging.DEBUG),
            ("debug", 5),
            ("trace", 5),
        ):
            with patch("bot.player.logging.log") as log:
                player.log_handler(mpv_level, "ao/pulse", "message\n")
            level, text = log.call_args.args
            self.assertEqual(level, expected, mpv_level)
            self.assertEqual(text, f"mpv {mpv_level}: ao/pulse: message")

    def test_unresponsive_mpv_logs_stacks_and_exits(self):
        player = self.make_player()
        player.track = SimpleNamespace(name="General Conference")
        with patch("bot.player.os._exit") as exit_, patch(
            "bot.player.logging.critical"
        ) as critical:
            player._on_mpv_unresponsive(61.0)
        exit_.assert_called_once_with(MPV_UNRESPONSIVE_EXIT_CODE)
        message = critical.call_args.args[0]
        self.assertIn("61s", message)
        self.assertIn("General Conference", message)
        self.assertIn("Thread", message)
