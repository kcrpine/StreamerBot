"""The TeamTalk event thread must survive events it does not understand.

That thread is the bot's only reader of chat. On 2026-09-30 the SDK delivered
event 410 (a user account created on the server, which an admin bot is told
about), EventType had no member for it, and the ValueError from EventType(410)
ended the thread. The process kept running, so the container stayed up and
Docker never restarted it, and the bot sat in its channel unable to hear a
command until someone restarted it by hand. Three bots had hit it before.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import TeamTalkPy

from bot import Bot
from bot.TeamTalk.structs import EventType
from bot.TeamTalk.thread import TeamTalkThread


class EventTypeTests(TestCase):
    def test_account_events_that_killed_the_thread_are_known(self):
        self.assertIs(EventType(410), EventType.USER_ACCOUNT_NEW)
        self.assertIs(EventType(420), EventType.USER_ACCOUNT_REMOVE)

    def test_every_event_the_sdk_defines_has_a_member(self):
        """Fails on an SDK bump that adds events, which is when to add them here."""
        unmapped = [
            name
            for name in dir(TeamTalkPy.ClientEvent)
            if name.startswith("CLIENTEVENT_")
            and EventType(int(getattr(TeamTalkPy.ClientEvent, name))) is EventType.UNKNOWN
        ]
        self.assertEqual(unmapped, [])

    def test_an_event_no_sdk_has_defined_yet_is_unknown_rather_than_an_error(self):
        self.assertIs(EventType(987654), EventType.UNKNOWN)


def make_thread(process_next_event):
    thread = TeamTalkThread.__new__(TeamTalkThread)
    thread.config = SimpleNamespace(
        event_handling=SimpleNamespace(load_event_handlers=False)
    )
    thread.process_next_event = process_next_event
    return thread


class EventLoopTests(TestCase):
    def test_an_error_handling_one_event_does_not_end_the_loop(self):
        calls = []

        def process_next_event():
            calls.append(None)
            if len(calls) == 1:
                raise ValueError("410 is not a valid EventType")
            thread.close()

        thread = make_thread(process_next_event)
        with self.assertLogs(level="ERROR") as logs, patch("time.sleep"):
            thread.run()

        self.assertEqual(len(calls), 2)
        self.assertIn("skipping it", logs.output[0])

    def test_a_deliberate_exit_still_ends_the_thread(self):
        """The give-up paths call sys.exit; that must not be swallowed and retried."""
        thread = make_thread(Mock(side_effect=SystemExit(1)))
        with self.assertRaises(SystemExit):
            thread.run()


def make_bot(alive, closed):
    bot = Bot.__new__(Bot)
    bot.ttclient = SimpleNamespace(
        thread=SimpleNamespace(is_alive=lambda: alive, _close=closed)
    )
    return bot


class WatchdogTests(TestCase):
    def test_a_thread_that_stopped_on_its_own_counts_as_died(self):
        self.assertTrue(make_bot(alive=False, closed=False)._teamtalk_thread_died())

    def test_a_running_thread_has_not_died(self):
        self.assertFalse(make_bot(alive=True, closed=False)._teamtalk_thread_died())

    def test_a_thread_stopped_by_close_has_not_died(self):
        """Shutting the bot down stops the thread too; that is not a failure."""
        self.assertFalse(make_bot(alive=False, closed=True)._teamtalk_thread_died())

    def test_exiting_ends_the_whole_process_so_docker_restarts_it(self):
        bot = make_bot(alive=False, closed=False)
        with patch("bot.os._exit") as exit_, patch("bot.logging.shutdown"), \
                self.assertLogs(level="CRITICAL"):
            bot._exit_for_restart()
        exit_.assert_called_once_with(1)
