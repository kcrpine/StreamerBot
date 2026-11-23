"""Whether the bot is signed in to YouTube is YouTube's answer, not a file's.

The case that led here, captianbubblebot on 2026-11-22: Google ended the
session on the 19th and the bot logged "signed in to YouTube with a browser
session" at its next start-up, because a cookie file existed. An 84-minute video
was then resolved while it was really signed out, stopped at 69.5s, and went on
stopping at 69.5s after the session was imported, because the bot kept handing
back the address it had resolved before.
"""

import os
import tempfile
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot import errors
from bot.modules.youtube_session_keeper import (
    VERIFY_MAX_AGE_SECONDS,
    SessionCheck,
)
from bot.player.enums import TrackType
from bot.player.track import Track
from bot.services.youtube_bridge import YouTubeBridge
from bot.services.youtube_session import session_generation
from test_youtube_session import FakeClock, google_cookies, make_keeper, youtube_says


def signed_in_keeper(**kwargs):
    keeper = make_keeper(**kwargs)
    keeper.store.save(google_cookies(), "account||", "import", new_sign_in=True)
    return keeper


class SessionGenerationTests(TestCase):
    """The number that says which sign-in a stream address belongs to."""

    def setUp(self):
        self.keeper = make_keeper()
        self.store = self.keeper.store

    def test_an_import_is_a_new_sign_in_even_over_a_stored_session(self):
        self.store.save(google_cookies(), "account||", "import", new_sign_in=True)
        before = session_generation()
        self.store.save(google_cookies(sid="other"), "account||", "import", new_sign_in=True)
        self.assertNotEqual(session_generation(), before)

    def test_routine_rotation_is_the_same_sign_in(self):
        # Every refresh rotates the cookies; re-resolving every queued track
        # four times a day would be work for nothing.
        self.store.save(google_cookies(), "account||", "browser", new_sign_in=True)
        before = session_generation()
        self.assertTrue(self.store.save(google_cookies(psidts="rotated"), "account||", "browser"))
        self.assertEqual(session_generation(), before)

    def test_youtube_ending_the_session_changes_it_once(self):
        self.store.save(google_cookies(), "account||", "browser", new_sign_in=True)
        before = session_generation()
        self.store.mark_needs_sign_in()
        after = session_generation()
        self.store.mark_needs_sign_in()
        self.assertNotEqual(after, before)
        self.assertEqual(session_generation(), after)

    def test_a_session_stored_after_one_ended_is_a_new_sign_in(self):
        self.store.save(google_cookies(), "account||", "browser", new_sign_in=True)
        self.store.mark_needs_sign_in()
        before = session_generation()
        self.store.save(google_cookies(psidts="rotated"), "account||", "browser")
        self.assertNotEqual(session_generation(), before)

    def test_signing_out_changes_it(self):
        self.store.save(google_cookies(), "account||", "browser", new_sign_in=True)
        before = session_generation()
        self.store.clear()
        self.assertNotEqual(session_generation(), before)


class KeeperVerifyTests(TestCase):
    def test_no_session_is_disconnected_without_asking_youtube(self):
        checker = youtube_says("signed_in")
        keeper = make_keeper(checker=checker)

        self.assertEqual(keeper.status(), "disconnected")
        checker.assert_not_called()

    def test_connected_only_once_youtube_says_so(self):
        checker = youtube_says("signed_in")
        keeper = signed_in_keeper(checker=checker)

        self.assertEqual(keeper.status(), "connected")
        checker.assert_called_once_with()

    def test_a_session_youtube_has_ended_is_expired_and_marked(self):
        # private-bot, 2026-11-23: its files said connected; youtube.com sent
        # its cookies to Google's sign-in page.
        keeper = signed_in_keeper(checker=youtube_says("signed_out"))

        with self.assertLogs("bot.modules.youtube_session_keeper", level="WARNING") as logged:
            self.assertEqual(keeper.status(), "expired")

        self.assertTrue(keeper.store.needs_sign_in())
        self.assertIn("has ended", "\n".join(logged.output))
        self.assertIn("li yt", "\n".join(logged.output))

    def test_with_chrome_an_ended_session_is_rechecked_in_the_background(self):
        # The profile can hold a newer session than the file did; a refresh
        # stores it and clears the mark.
        exported = threading.Event()
        engine = Mock()

        def export_session(service):
            exported.set()
            return {"logged_in": True, "datasync_id": "account||", "cookies": google_cookies(psidts="new")}

        engine.export_session.side_effect = export_session
        keeper = signed_in_keeper(engine=engine, checker=youtube_says("signed_out"))

        with self.assertLogs("bot.modules.youtube_session_keeper", level="INFO"):
            self.assertIs(keeper.verify("test"), SessionCheck.Ended)
            self.assertTrue(exported.wait(5))
            keeper.wait_until_idle(5)

        self.assertFalse(keeper.store.needs_sign_in())

    def test_unknown_leaves_the_stored_state_and_asks_again_next_time(self):
        # An outage or a consent page says nothing about the session; calling
        # it ended would send a user to sign in again for nothing.
        checker = youtube_says("unknown", reason="youtube.com answered 503")
        keeper = signed_in_keeper(checker=checker)

        with self.assertLogs("bot.modules.youtube_session_keeper", level="INFO"):
            self.assertEqual(keeper.status(), "connected")
            keeper.status()

        self.assertFalse(keeper.store.needs_sign_in())
        self.assertEqual(checker.call_count, 2)

    def test_an_unreachable_bridge_is_unknown(self):
        keeper = signed_in_keeper(checker=Mock(side_effect=errors.ServiceError("bridge unavailable")))

        with self.assertLogs("bot.modules.youtube_session_keeper", level="WARNING"):
            self.assertIs(keeper.verify("test"), SessionCheck.Unknown)
        self.assertFalse(keeper.store.needs_sign_in())

    def test_one_answer_stands_for_a_few_minutes(self):
        clock = FakeClock()
        checker = youtube_says("signed_in")
        keeper = signed_in_keeper(checker=checker, clock=clock)

        keeper.status()
        clock.now += VERIFY_MAX_AGE_SECONDS - 1
        keeper.status()
        self.assertEqual(checker.call_count, 1)

        clock.now += 2
        keeper.status()
        self.assertEqual(checker.call_count, 2)

    def test_a_new_sign_in_is_asked_about_at_once(self):
        checker = youtube_says("signed_in")
        keeper = signed_in_keeper(checker=checker)

        keeper.status()
        keeper.store.save(google_cookies(sid="new"), "account||", "import", new_sign_in=True)
        keeper.status()

        self.assertEqual(checker.call_count, 2)

    def test_a_different_datasync_id_is_reported(self):
        keeper = signed_in_keeper(checker=youtube_says("signed_in", datasync_matches=False))

        with self.assertLogs("bot.modules.youtube_session_keeper", level="WARNING") as logged:
            keeper.verify("test")
        self.assertIn("DataSync ID", "\n".join(logged.output))

    def test_start_up_reports_what_youtube_said_not_that_a_file_exists(self):
        keeper = signed_in_keeper(checker=youtube_says("signed_out"))

        with self.assertLogs("bot.modules.youtube_session_keeper", level="INFO") as logged:
            self.assertIs(keeper.log_start_up_state(), SessionCheck.Ended)

        text = "\n".join(logged.output)
        self.assertIn("has ended", text)
        self.assertNotIn("signed in to YouTube with a browser session", text)
        # Said once, not once by verify and again by the start-up report.
        self.assertEqual(text.count("li yt"), 1)

    def test_start_up_with_a_session_already_marked_ended_says_so(self):
        checker = youtube_says("signed_in")
        keeper = signed_in_keeper(checker=checker)
        keeper.store.mark_needs_sign_in()

        with self.assertLogs("bot.modules.youtube_session_keeper", level="WARNING") as logged:
            self.assertIs(keeper.log_start_up_state(), SessionCheck.Ended)
        self.assertIn("has ended", "\n".join(logged.output))
        checker.assert_not_called()


class BridgeStatusTests(TestCase):
    def setUp(self):
        self.bridge = YouTubeBridge()

    def test_signed_in_means_youtube_said_signed_in(self):
        # The old bridge answered signed_in whenever it could read a cookie file.
        for answer, expected in (
            ({"state": "signed_in", "signed_in": True}, True),
            ({"state": "signed_out", "signed_in": False}, False),
            ({"state": "unknown", "signed_in": False}, False),
            ({"status": "authenticated", "signed_in": True}, False),
        ):
            with patch.object(self.bridge, "_post", return_value=answer):
                self.assertIs(self.bridge.is_signed_in(), expected, answer)

    def test_fresh_asks_the_bridge_not_to_use_its_cached_answer(self):
        with patch.object(self.bridge, "_post", return_value={}) as post:
            self.bridge.auth_status(fresh=True)
        self.assertEqual(post.call_args.args[0], "/auth/status")
        self.assertIs(post.call_args.kwargs.get("fresh"), True)


class BridgeResolveCacheTests(TestCase):
    """The bot-side cache that handed back the signed-out address in 0.04 ms."""

    def test_an_address_from_before_the_sign_in_changed_is_not_reused(self):
        bridge = YouTubeBridge()
        answers = [
            {"url": "resolved-signed-out", "cache_expires_at_ms": 9_000_000_000_000},
            {"url": "resolved-signed-in", "cache_expires_at_ms": 9_000_000_000_000},
        ]
        store = make_keeper().store
        with patch.object(bridge, "_post", side_effect=answers) as post:
            first = bridge.resolve(video_id="fmdnKlDMnXk")
            again = bridge.resolve(video_id="fmdnKlDMnXk")
            store.save(google_cookies(), "account||", "import", new_sign_in=True)
            after_import = bridge.resolve(video_id="fmdnKlDMnXk")

        self.assertEqual(first["url"], "resolved-signed-out")
        self.assertEqual(again["url"], "resolved-signed-out")
        self.assertEqual(after_import["url"], "resolved-signed-in")
        self.assertEqual(post.call_count, 2)


class TrackStaleSignInTests(TestCase):
    def _service(self, urls):
        service = SimpleNamespace(
            engine="mpv",
            _bridge=Mock(),
            get=Mock(side_effect=[
                [Track(service="yt", url=url, name="Coin Flip", type=TrackType.Default)] for url in urls
            ]),
        )
        return service

    def _track(self, service_name="yt"):
        return Track(
            service=service_name,
            url="https://www.youtube.com/watch?v=fmdnKlDMnXk",
            name="Coin Flip",
            extra_info={"videoId": "fmdnKlDMnXk"},
            type=TrackType.Dynamic,
        )

    def test_a_track_resolved_before_a_new_sign_in_resolves_again(self):
        service = self._service(["http://relay/old", "http://relay/new"])
        store = make_keeper().store
        track = self._track()
        with patch("bot.player.track.get_service_by_name", return_value=service, create=True):
            self.assertEqual(track.url, "http://relay/old")
            self.assertEqual(track.url, "http://relay/old")
            self.assertFalse(track.resolved_under_older_sign_in())

            store.save(google_cookies(), "account||", "import", new_sign_in=True)
            self.assertTrue(track.resolved_under_older_sign_in())
            with self.assertLogs("root", level="INFO"):
                self.assertEqual(track.url, "http://relay/new")
            self.assertFalse(track.resolved_under_older_sign_in())

        self.assertEqual(service.get.call_count, 2)
        service._bridge.invalidate.assert_called_once_with(video_id="fmdnKlDMnXk", url="")

    def test_other_services_never_count_as_stale(self):
        service = self._service(["http://relay/podcast"])
        store = make_keeper().store
        track = self._track(service_name="pc")
        with patch("bot.player.track.get_service_by_name", return_value=service, create=True):
            track.url
            store.save(google_cookies(), "account||", "import", new_sign_in=True)
            self.assertFalse(track.resolved_under_older_sign_in())
            self.assertEqual(track.url, "http://relay/podcast")
        self.assertEqual(service.get.call_count, 1)

    def test_truthiness_never_resolves(self):
        service = self._service([])
        track = self._track()
        with patch("bot.player.track.get_service_by_name", return_value=service, create=True):
            self.assertTrue(track)
        service.get.assert_not_called()
