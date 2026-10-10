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


def make_connecting_thread(connect_results, reconnection_attempts=-1):
    """A thread whose client refuses or accepts connect() in the order given."""
    thread = TeamTalkThread.__new__(TeamTalkThread)
    thread.config = SimpleNamespace(
        hostname="chrisduffley.com",
        tcp_port=1982,
        reconnection_attempts=reconnection_attempts,
        reconnection_timeout=10,
        event_handling=SimpleNamespace(load_event_handlers=False),
    )
    thread.ttclient = Mock(reconnect=False, reconnect_attempt=0)
    thread.ttclient.connect.side_effect = list(connect_results)
    thread.ttclient.tt.getMessage.return_value = SimpleNamespace(
        nClientEvent=EventType.NONE.value
    )
    thread._connect_at = None
    return thread


class RefusedConnectTests(TestCase):
    """TT_Connect refusing to start must be retried, not waited on forever.

    On 2026-10-10 chrisduffley.com dropped three bots at once. Each logged
    "Lost the connection ... Attempt 1 of unlimited" and then nothing for more
    than sixteen hours: the reconnect's TT_Connect returned FALSE, which the
    SDK does without raising any event (measured: an unresolvable hostname
    returns 0 and stays silent), and the thread sat in getMessage(-1) waiting
    for one. Every earlier drop on those bots ended the same way.
    """

    def test_a_refused_attempt_is_logged_and_retried_after_the_timeout(self):
        thread = make_connecting_thread([False, True])
        with patch("bot.TeamTalk.thread.time.monotonic", return_value=100.0), \
                self.assertLogs(level="WARNING") as logs:
            thread.start_connecting()
        self.assertIn("Could not start connecting to chrisduffley.com:1982", logs.output[0])
        self.assertIn("Attempt 1 of unlimited", logs.output[0])
        self.assertEqual(thread._connect_at, 110.0)

        with patch("bot.TeamTalk.thread.time.monotonic", return_value=105.0):
            thread.process_next_event()
        self.assertEqual(thread.ttclient.connect.call_count, 1)

        with patch("bot.TeamTalk.thread.time.monotonic", return_value=110.0):
            thread.process_next_event()
        self.assertEqual(thread.ttclient.connect.call_count, 2)
        self.assertIsNone(thread._connect_at)

    def test_refusals_keep_being_retried_without_limit(self):
        thread = make_connecting_thread([False] * 5 + [True])
        clock = [0.0]
        with patch("bot.TeamTalk.thread.time.monotonic", side_effect=lambda: clock[0]), \
                self.assertLogs(level="WARNING"):
            thread.start_connecting()
            for _ in range(5):
                clock[0] += 10
                thread.process_next_event()
        self.assertEqual(thread.ttclient.connect.call_count, 6)
        self.assertIsNone(thread._connect_at)

    def test_a_lost_connection_reconnects_through_the_same_retry(self):
        thread = make_connecting_thread([False, True])
        thread.ttclient.get_event.return_value = SimpleNamespace(
            event_type=EventType.CON_LOST
        )
        thread.ttclient.tt.getMessage.return_value = SimpleNamespace(
            nClientEvent=EventType.CON_LOST.value
        )
        with patch("bot.TeamTalk.thread.time.monotonic", return_value=0.0), \
                self.assertLogs(level="WARNING"):
            thread.process_next_event()
        thread.ttclient.disconnect.assert_called()
        thread.ttclient.connect.assert_not_called()

        thread.ttclient.tt.getMessage.return_value = SimpleNamespace(
            nClientEvent=EventType.NONE.value
        )
        with patch("bot.TeamTalk.thread.time.monotonic", side_effect=[10.0, 10.0, 20.0, 20.0]), \
                self.assertLogs(level="WARNING") as logs:
            thread.process_next_event()
            thread.process_next_event()
        self.assertIn("Attempt 2 of unlimited", logs.output[0])
        self.assertEqual(thread.ttclient.connect.call_count, 2)

    def test_a_limited_bot_still_gives_up(self):
        thread = make_connecting_thread([False], reconnection_attempts=0)
        with self.assertRaises(SystemExit), self.assertLogs(level="WARNING"):
            thread.start_connecting()

    def test_the_wait_for_an_event_is_finite(self):
        thread = make_connecting_thread([])
        thread.process_next_event()
        (wait,), _ = thread.ttclient.tt.getMessage.call_args
        self.assertGreater(wait, 0)
        thread.ttclient.get_event.assert_not_called()
