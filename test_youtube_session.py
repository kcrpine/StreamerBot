"""Phase 9: a YouTube browser session, kept alive per bot.

What these pin is the part that fails silently in production: a refresh that
overwrites a working session with a dead one, a rewrite that throws away the
bridge's warm session every six hours, a dead session reported as "service
unavailable", and a play request that is dropped rather than held.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time
from http.cookiejar import MozillaCookieJar
from types import SimpleNamespace
from unittest import TestCase, skipIf
from unittest.mock import Mock

from bot import errors
from bot.auth.cookies import CookieFileError, has_google_session, parse_netscape, to_netscape
from bot.modules.youtube_session_keeper import (
    ImportProblem,
    ON_DEMAND_MIN_INTERVAL_SECONDS,
    RefreshResult,
    YouTubeSessionKeeper,
    is_login_required,
)
from bot.services.web.youtube import YouTubeAdapter
from bot.services.youtube_session import YouTubeSessionStore, fingerprint

TAB = chr(9)
NL = chr(10)


def google_cookies(sid="sid-1", psidts="ts-1"):
    return [
        {"name": "SAPISID", "value": "sapisid", "domain": ".youtube.com", "path": "/", "expires": 4102444800, "secure": True},
        {"name": "SID", "value": sid, "domain": ".youtube.com", "path": "/", "expires": 4102444800, "secure": False},
        {"name": "__Secure-1PSIDTS", "value": psidts, "domain": ".youtube.com", "path": "/", "expires": -1, "secure": True},
        {"name": "SID", "value": sid, "domain": ".google.com", "path": "/", "expires": 4102444800, "secure": False},
        {"name": "unrelated", "value": "x", "domain": ".example.com", "path": "/", "expires": -1},
    ]


def cookies_txt(cookies):
    return to_netscape(cookies, ("youtube.com", "google.com"))


class CookieFileTests(TestCase):
    def test_written_cookies_load_in_pythons_own_reader(self):
        path = os.path.join(tempfile.mkdtemp(), "cookies.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(cookies_txt(google_cookies()))

        jar = MozillaCookieJar(path)
        jar.load()

        self.assertEqual({c.name for c in jar}, {"SAPISID", "SID", "__Secure-1PSIDTS"})

    def test_what_browser_extensions_export_parses(self):
        text = chr(0xFEFF) + NL.join([
            "# Netscape HTTP Cookie File",
            "#HttpOnly_" + TAB.join([".youtube.com", "TRUE", "/", "TRUE", "4102444800", "SID", "a"]),
            TAB.join([".youtube.com", "TRUE", "/", "TRUE", "0", "SAPISID", "b"]),
        ]).replace(NL, chr(13) + NL)

        cookies = parse_netscape(text)

        self.assertEqual([c["name"] for c in cookies], ["SID", "SAPISID"])
        self.assertTrue(cookies[0]["httpOnly"])
        self.assertEqual(cookies[1]["expires"], -1)

    def test_a_paste_that_lost_its_tabs_is_refused(self):
        with self.assertRaises(CookieFileError):
            parse_netscape(".youtube.com TRUE / TRUE 4102444800 SID a")

    def test_a_signed_out_export_is_not_a_google_session(self):
        self.assertTrue(has_google_session(google_cookies()))
        self.assertFalse(has_google_session([c for c in google_cookies() if c["name"] != "SAPISID"]))
        # Signed in on google.com only: nothing a youtube.com request would carry.
        self.assertFalse(has_google_session([dict(c, domain=".google.com") for c in google_cookies()]))


class SessionStoreTests(TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.mkdtemp(), "youtube_auth")
        self.store = YouTubeSessionStore(self.dir)

    def test_a_saved_session_is_connected_with_its_datasync_id(self):
        self.assertTrue(self.store.save(google_cookies(), "account||", "browser"))

        self.assertEqual(self.store.status(), "connected")
        self.assertEqual(self.store.meta()["datasync_id"], "account||")
        self.assertEqual({c["name"] for c in self.store.load_cookies()},
                         {"SAPISID", "SID", "__Secure-1PSIDTS"})

    def test_unchanged_cookies_are_not_rewritten(self):
        """The bridge rebuilds a bot's session whenever cookies.txt changes, so a
        needless rewrite every refresh would throw its warm session away."""
        self.store.save(google_cookies(), "account||", "browser")
        before = os.stat(self.store.cookies_path).st_mtime_ns
        time.sleep(0.02)

        changed = self.store.save(google_cookies(), "account||", "browser")

        self.assertFalse(changed)
        self.assertEqual(os.stat(self.store.cookies_path).st_mtime_ns, before)

    def test_rotated_cookies_are_rewritten(self):
        self.store.save(google_cookies(psidts="ts-1"), "account||", "browser")

        self.assertTrue(self.store.save(google_cookies(psidts="ts-2"), "account||", "browser"))

    def test_expiry_alone_is_not_a_change(self):
        a = google_cookies()
        b = [dict(c, expires=c["expires"] + 100 if c["expires"] > 0 else -1) for c in a]

        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_cookies_that_cannot_sign_in_never_overwrite_a_working_file(self):
        self.store.save(google_cookies(), "account||", "browser")

        with self.assertRaises(ValueError):
            self.store.save([{"name": "PREF", "value": "x", "domain": ".youtube.com"}], "", "browser")

        self.assertEqual(self.store.status(), "connected")

    def test_a_session_google_ended_is_expired_until_saved_again(self):
        self.store.save(google_cookies(), "account||", "browser")
        self.store.mark_needs_sign_in()
        self.assertEqual(self.store.status(), "expired")

        self.store.save(google_cookies(sid="new"), "account||", "browser")
        self.assertEqual(self.store.status(), "connected")

    def test_clearing_removes_the_old_device_code_tokens_too(self):
        self.store.save(google_cookies(), "account||", "browser")
        with open(os.path.join(self.dir, "credentials.json"), "w") as f:
            f.write("{}")

        self.store.clear()

        self.assertEqual(self.store.status(), "disconnected")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "credentials.json")))

    @skipIf(sys.platform == "win32", "POSIX permissions")
    def test_files_are_readable_only_by_the_bot(self):
        """A cookie file is a full login to someone's Google account."""
        self.store.save(google_cookies(), "account||", "browser")

        for path in (self.store.cookies_path, self.store.meta_path):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)


class FakeClock:
    def __init__(self, now=1_900_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_keeper(engine=None, hours=6, browser_sign_in=True, clock=None):
    config = SimpleNamespace(browser_sign_in=browser_sign_in, session_refresh_hours=hours)
    bot = SimpleNamespace(
        config=SimpleNamespace(services=SimpleNamespace(yt=config)),
        translator=SimpleNamespace(translate=lambda s: s),
    )
    store = YouTubeSessionStore(os.path.join(tempfile.mkdtemp(), "youtube_auth"))
    return YouTubeSessionKeeper(bot, store=store, engine_getter=lambda: engine,
                                clock=clock or FakeClock())


def signed_in_engine(cookies=None, datasync="account||"):
    engine = Mock()
    engine.export_session.return_value = {
        "logged_in": True, "datasync_id": datasync, "cookies": cookies or google_cookies(psidts="rotated"),
    }
    engine.import_session.return_value = engine.export_session.return_value
    return engine


class KeeperRefreshTests(TestCase):
    def test_a_live_session_stores_the_rotated_cookies(self):
        keeper = make_keeper(signed_in_engine())
        keeper.store.save(google_cookies(), "account||", "browser")

        self.assertIs(keeper.refresh("test"), RefreshResult.Alive)
        self.assertIn("rotated", [c["value"] for c in keeper.store.load_cookies()])

    def test_a_signed_out_page_marks_the_session_ended_and_keeps_the_file(self):
        engine = Mock()
        engine.export_session.return_value = {"logged_in": False, "cookies": [], "datasync_id": ""}
        keeper = make_keeper(engine)
        keeper.store.save(google_cookies(), "account||", "browser")

        self.assertIs(keeper.refresh("test"), RefreshResult.Ended)
        self.assertEqual(keeper.status(), "expired")
        self.assertTrue(keeper.store.has_session())

    def test_an_ended_session_is_not_retried_on_schedule(self):
        """Retrying forever against a dead session helps nobody."""
        clock = FakeClock()
        keeper = make_keeper(signed_in_engine(), clock=clock)
        keeper.store.save(google_cookies(), "account||", "browser")
        keeper.store.mark_needs_sign_in()
        clock.now += 7 * 3600

        self.assertFalse(keeper.is_due())

    def test_the_schedule_counts_from_the_last_check_on_disk(self):
        """Bots restart on every update; a timer from start-up might never fire."""
        clock = FakeClock(now=time.time())
        keeper = make_keeper(signed_in_engine(), hours=6, clock=clock)
        keeper.store.save(google_cookies(), "account||", "browser")

        self.assertFalse(keeper.is_due())
        clock.now += 6 * 3600 + 1
        self.assertTrue(keeper.is_due())

    def test_zero_hours_turns_the_schedule_off(self):
        clock = FakeClock(now=time.time() + 10 * 86400)
        keeper = make_keeper(signed_in_engine(), hours=0, clock=clock)
        keeper.store.save(google_cookies(), "account||", "browser")

        self.assertFalse(keeper.is_due())

    def test_no_browser_means_unavailable_not_ended(self):
        """arm64 cannot check, and must not claim the session is dead."""
        keeper = make_keeper(None)
        keeper.store.save(google_cookies(), "", "import")

        self.assertIs(keeper.refresh("test"), RefreshResult.Unavailable)
        self.assertEqual(keeper.status(), "connected")

    def test_a_refused_request_refreshes_at_most_once_per_interval(self):
        clock = FakeClock()
        engine = signed_in_engine()
        keeper = make_keeper(engine, clock=clock)
        keeper.store.save(google_cookies(), "account||", "browser")

        keeper.on_login_required()
        self.assertFalse(keeper.can_refresh_now())
        keeper.on_login_required()
        self.assertEqual(engine.export_session.call_count, 1)

        clock.now += ON_DEMAND_MIN_INTERVAL_SECONDS
        self.assertTrue(keeper.can_refresh_now())

    def test_concurrent_refreshes_load_the_page_once(self):
        gate = threading.Event()
        engine = signed_in_engine()
        result = engine.export_session.return_value

        def slow(_service):
            gate.wait(5)
            return result

        engine.export_session.side_effect = slow
        keeper = make_keeper(engine)
        keeper.store.save(google_cookies(), "account||", "browser")

        results = []
        threads = [threading.Thread(target=lambda: results.append(keeper.refresh("t"))) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        self.assertTrue(keeper.is_refreshing)
        gate.set()
        for t in threads:
            t.join(5)

        self.assertEqual(engine.export_session.call_count, 1)
        self.assertEqual(results, [RefreshResult.Alive] * 3)

    def test_login_required_is_recognised_in_the_bridge_error(self):
        error = errors.ServiceError(
            "Unable to resolve stream for x; MWEB: no streaming data "
            "(LOGIN_REQUIRED: Sign in to confirm you're not a bot)"
        )
        self.assertTrue(is_login_required(error))
        self.assertFalse(is_login_required(errors.ServiceError("Video unavailable")))


class KeeperSignInAndImportTests(TestCase):
    def test_sign_in_is_finished_only_once_the_session_is_stored(self):
        """The portal must not say "connected" before the bridge can use it."""
        keeper = make_keeper(signed_in_engine())
        job = Mock()

        keeper.finish_browser_sign_in(job)

        job.succeed.assert_called_once_with()
        self.assertEqual(keeper.status(), "connected")

    def test_sign_in_that_youtube_does_not_recognise_fails(self):
        engine = Mock()
        engine.export_session.return_value = {"logged_in": False, "cookies": []}
        keeper = make_keeper(engine)
        job = Mock()

        keeper.finish_browser_sign_in(job)

        job.fail.assert_called_once()
        job.succeed.assert_not_called()

    def test_import_problems_are_named(self):
        keeper = make_keeper(None)

        self.assertEqual(keeper.import_text("  "), (False, ImportProblem.Empty))
        self.assertEqual(keeper.import_text("hello"), (False, ImportProblem.NotCookies))
        signed_out = cookies_txt([c for c in google_cookies() if c["name"] != "SAPISID"])
        self.assertEqual(keeper.import_text(signed_out), (False, ImportProblem.NoGoogleSession))
        self.assertEqual(keeper.import_text("x" * (600 * 1024)), (False, ImportProblem.TooLarge))

    def test_without_a_browser_an_import_is_stored_as_it_is(self):
        keeper = make_keeper(None)

        self.assertEqual(keeper.import_text(cookies_txt(google_cookies())), (True, None))
        self.assertEqual(keeper.store.meta()["source"], "import")

    def test_with_a_browser_youtube_is_asked_before_an_import_is_accepted(self):
        engine = Mock()
        engine.import_session.return_value = {"logged_in": False, "cookies": []}
        keeper = make_keeper(engine)

        self.assertEqual(keeper.import_text(cookies_txt(google_cookies())),
                         (False, ImportProblem.SessionEnded))
        self.assertEqual(keeper.status(), "disconnected")

    def test_a_confirmed_import_keeps_the_datasync_id_the_browser_read(self):
        keeper = make_keeper(signed_in_engine(datasync="acct||"))

        self.assertEqual(keeper.import_text(cookies_txt(google_cookies())), (True, None))
        self.assertEqual(keeper.store.meta()["datasync_id"], "acct||")

    def test_browser_sign_in_switched_off_leaves_import_only(self):
        keeper = make_keeper(signed_in_engine(), browser_sign_in=False)

        self.assertFalse(keeper.browser_available)


class HeldRequestTests(TestCase):
    def test_a_request_during_a_refresh_is_played_when_it_finishes(self):
        keeper = make_keeper(None)
        keeper._idle.clear()
        sent = []
        done = threading.Event()

        keeper.hold(lambda: "Playing x", lambda m: (sent.append(m), done.set()), "failed")
        time.sleep(0.1)
        self.assertEqual(sent, [], "nothing is said until the refresh is over")
        keeper._last_result = RefreshResult.Alive
        keeper._idle.set()
        done.wait(5)

        self.assertEqual(sent, ["Playing x"])

    def test_a_held_request_whose_session_ended_says_so_once(self):
        keeper = make_keeper(None)
        keeper._idle.clear()
        sent = []
        done = threading.Event()
        retry = Mock()

        keeper.hold(retry, lambda m: (sent.append(m), done.set()), "failed", "signed out")
        # What a refresh that found the session ended leaves behind.
        keeper.store.save(google_cookies(), "account||", "browser")
        keeper.store.mark_needs_sign_in()
        keeper._last_result = RefreshResult.Ended
        keeper._idle.set()
        done.wait(5)

        retry.assert_not_called()
        self.assertEqual(sent, ["signed out"])


class ChallengeKindTests(TestCase):
    def test_google_second_steps_are_mapped(self):
        kind = YouTubeAdapter.challenge_kind
        self.assertEqual(kind("https://accounts.google.com/v3/signin/challenge/dp?TL=x"), "approve")
        self.assertEqual(kind("https://accounts.google.com/signin/v2/challenge/totp"), "code")
        self.assertEqual(kind("https://accounts.google.com/v3/signin/challenge/ipp"), "code")
        self.assertEqual(kind("https://accounts.google.com/v3/signin/challenge/recaptcha"), "captcha")
        self.assertEqual(kind("https://accounts.google.com/v3/signin/challenge/selection"), "selection")
        self.assertEqual(kind("https://accounts.google.com/v3/signin/challenge/wat"), "unknown")
        self.assertEqual(kind("https://www.youtube.com/"), "")

    def test_imported_cookies_are_shaped_for_chrome(self):
        entries = YouTubeAdapter.playwright_cookies(google_cookies())

        session_cookie = next(e for e in entries if e["name"] == "__Secure-1PSIDTS")
        self.assertNotIn("expires", session_cookie, "a session cookie has no expiry")
        self.assertTrue(all(e["sameSite"] in ("None", "Lax") for e in entries))


class CommandHelperTests(TestCase):
    """Command.youtube_hold_or_run, without TeamTalk."""

    def make(self, keeper):
        from bot.commands.command import Command

        command = object.__new__(Command)
        command.command_processor = SimpleNamespace(youtube_session=keeper)
        command.translator = SimpleNamespace(translate=lambda s: s)
        command.ttclient = Mock()
        return command

    def refused(self):
        raise errors.ServiceError("MWEB: no streaming data (LOGIN_REQUIRED: Sign in to confirm)")

    def test_no_session_says_to_connect_one_instead_of_service_unavailable(self):
        command = self.make(make_keeper(None))

        reply = command.youtube_hold_or_run("song", SimpleNamespace(), self.refused)

        self.assertTrue(reply.endswith("li yt"))

    def test_an_ended_session_says_to_sign_in_again(self):
        keeper = make_keeper(None)
        keeper.store.save(google_cookies(), "", "import")
        keeper.store.mark_needs_sign_in()

        reply = self.make(keeper).youtube_hold_or_run("song", SimpleNamespace(), self.refused)

        self.assertIn("signed StreamerBot out", reply)

    def test_a_refusal_with_a_live_session_renews_then_plays(self):
        keeper = make_keeper(signed_in_engine())
        keeper.store.save(google_cookies(), "account||", "browser")
        command = self.make(keeper)
        attempts = []

        def run():
            attempts.append(1)
            if len(attempts) == 1:
                self.refused()
            return "Playing song"

        reply = command.youtube_hold_or_run("song", SimpleNamespace(), run)
        for _ in range(50):
            if command.ttclient.send_message.called:
                break
            time.sleep(0.05)

        self.assertIn("Renewing the YouTube sign-in", reply)
        self.assertEqual(command.ttclient.send_message.call_args.args[0], "Playing song")

    def test_other_errors_keep_their_existing_handling(self):
        command = self.make(make_keeper(None))

        def unavailable():
            raise errors.ServiceError("Video unavailable")

        with self.assertRaises(errors.ServiceError):
            command.youtube_hold_or_run("song", SimpleNamespace(), unavailable)
