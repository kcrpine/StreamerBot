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
    PENDING_IMPORT_NAME,
    REJECTED_IMPORT_NAME,
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


def youtube_says(state="signed_in", **extra):
    """A stand-in for asking youtube.com through the bridge."""
    return Mock(return_value={"state": state, "signed_in": state == "signed_in", **extra})


def make_keeper(engine=None, hours=6, browser_sign_in=True, clock=None, checker=None):
    config = SimpleNamespace(browser_sign_in=browser_sign_in, session_refresh_hours=hours)
    bot = SimpleNamespace(
        config=SimpleNamespace(services=SimpleNamespace(yt=config)),
        translator=SimpleNamespace(translate=lambda s: s),
    )
    store = YouTubeSessionStore(os.path.join(tempfile.mkdtemp(), "youtube_auth"))
    return YouTubeSessionKeeper(bot, store=store, engine_getter=lambda: engine,
                                clock=clock or FakeClock(),
                                checker=checker or youtube_says("signed_in"))


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


class RestoredSessionAdoptionTests(TestCase):
    """The last step of bringing an old bot across.

    streamerbot.sh carries the old TTMediaBot's top-level cookies.txt into
    youtube_auth/imported_cookies.txt rather than deleting it, because Phase 9
    made cookies the way YouTube signs in again. This is what turns that file
    into a session the bridge can use.

    It is staged rather than written straight to cookies.txt because that file
    and the bot's Chrome profile have to agree: the keep-alive asks Chrome
    whether YouTube still considers it signed in, and a session present only in
    the file answers no. Importing loads it into the profile first.
    """

    def stage(self, keeper, text):
        os.makedirs(keeper.store.dir, mode=0o700, exist_ok=True)
        with open(keeper.pending_import_path, "w", encoding="utf-8") as f:
            f.write(text)

    def rejected_path(self, keeper):
        return os.path.join(keeper.store.dir, REJECTED_IMPORT_NAME)

    def test_nothing_staged_is_not_an_event(self):
        keeper = make_keeper(signed_in_engine())

        self.assertIsNone(keeper.adopt_pending_import())
        self.assertEqual(keeper.status(), "disconnected")

    def test_a_restored_session_becomes_a_usable_one(self):
        keeper = make_keeper(signed_in_engine())
        self.stage(keeper, cookies_txt(google_cookies()))

        self.assertIs(keeper.adopt_pending_import(), True)
        self.assertEqual(keeper.status(), "connected")
        self.assertEqual(keeper.store.meta()["source"], "import")

    def test_the_staged_file_is_removed_once_it_has_been_imported(self):
        """Left in place it would be imported again on every start, undoing a
        later sign-in with a session out of an old backup."""
        keeper = make_keeper(signed_in_engine())
        self.stage(keeper, cookies_txt(google_cookies()))

        keeper.adopt_pending_import()

        self.assertFalse(os.path.exists(keeper.pending_import_path))

    def test_it_goes_through_the_browser_like_any_other_import(self):
        """Not written to cookies.txt directly: the profile has to hold it too,
        or the first keep-alive marks a working session expired."""
        engine = signed_in_engine()
        keeper = make_keeper(engine)
        self.stage(keeper, cookies_txt(google_cookies()))

        keeper.adopt_pending_import()

        engine.import_session.assert_called_once()

    def test_a_session_youtube_has_ended_is_kept_rather_than_deleted(self):
        """A restore never destroys what it was given, and a file named for the
        reason it was refused is something a user can act on."""
        engine = Mock()
        engine.import_session.return_value = {"logged_in": False, "cookies": []}
        keeper = make_keeper(engine)
        self.stage(keeper, cookies_txt(google_cookies()))

        self.assertIs(keeper.adopt_pending_import(), False)
        self.assertEqual(keeper.status(), "disconnected")
        self.assertFalse(os.path.exists(keeper.pending_import_path))
        self.assertTrue(os.path.exists(self.rejected_path(keeper)))

    def test_a_file_that_is_not_cookies_is_refused_and_set_aside(self):
        keeper = make_keeper(signed_in_engine())
        self.stage(keeper, "this is not a cookie file")

        self.assertIs(keeper.adopt_pending_import(), False)
        self.assertTrue(os.path.exists(self.rejected_path(keeper)))

    def test_a_bot_already_signed_in_keeps_the_account_it_is_using(self):
        """The staged file came out of a backup, so it can only be older.
        Importing it would sign the bot out of the account it is playing from."""
        keeper = make_keeper(signed_in_engine())
        keeper.store.save(google_cookies(sid="live"), "account||", "browser")
        self.stage(keeper, cookies_txt(google_cookies(sid="from-the-backup")))

        self.assertIsNone(keeper.adopt_pending_import())

        self.assertEqual(keeper.status(), "connected")
        self.assertIn("live", [c["value"] for c in keeper.store.load_cookies()])
        self.assertNotIn("from-the-backup", [c["value"] for c in keeper.store.load_cookies()])
        self.assertFalse(os.path.exists(keeper.pending_import_path))

    def test_an_expired_session_is_replaced_by_the_restored_one(self):
        """Google ended the stored session, so there is nothing to protect and
        the restored file is the better of the two."""
        keeper = make_keeper(signed_in_engine())
        keeper.store.save(google_cookies(sid="dead"), "account||", "browser")
        keeper.store.mark_needs_sign_in()
        self.stage(keeper, cookies_txt(google_cookies(sid="from-the-backup")))

        self.assertIs(keeper.adopt_pending_import(), True)
        self.assertEqual(keeper.status(), "connected")

    def test_without_a_browser_the_restored_session_is_still_adopted(self):
        """arm64 has no Chrome. The file is stored as it is, which is exactly
        what the import page does there."""
        keeper = make_keeper(None)
        self.stage(keeper, cookies_txt(google_cookies()))

        self.assertIs(keeper.adopt_pending_import(), True)
        self.assertEqual(keeper.status(), "connected")

    def test_signing_out_removes_a_session_waiting_to_be_imported(self):
        """Otherwise a bot signs itself back in minutes after someone
        deliberately signed it out, with nothing on screen to explain it."""
        keeper = make_keeper(signed_in_engine())
        self.stage(keeper, cookies_txt(google_cookies()))

        keeper.sign_out()

        self.assertFalse(os.path.exists(keeper.pending_import_path))
        self.assertIsNone(keeper.adopt_pending_import())
        self.assertEqual(keeper.status(), "disconnected")

    def test_signing_out_removes_a_refused_one_too(self):
        keeper = make_keeper(signed_in_engine())
        os.makedirs(keeper.store.dir, mode=0o700, exist_ok=True)
        with open(self.rejected_path(keeper), "w", encoding="utf-8") as f:
            f.write(cookies_txt(google_cookies()))

        keeper.sign_out()

        self.assertFalse(os.path.exists(self.rejected_path(keeper)))

    def test_a_browser_failure_keeps_the_session_rather_than_losing_it(self):
        """import_text stores the cookies when the browser itself failed, since
        that is the browser failing and not the session. The staged file must
        not be the casualty of a transient Chrome error either way."""
        engine = Mock()
        engine.import_session.side_effect = RuntimeError("chrome would not start")
        keeper = make_keeper(engine)
        self.stage(keeper, cookies_txt(google_cookies()))

        self.assertIs(keeper.adopt_pending_import(), True)
        self.assertEqual(keeper.status(), "connected")

    @skipIf(sys.platform == "win32", "POSIX permissions")
    def test_the_adopted_session_is_readable_only_by_the_bot(self):
        keeper = make_keeper(signed_in_engine())
        self.stage(keeper, cookies_txt(google_cookies()))

        keeper.adopt_pending_import()

        self.assertEqual(stat.S_IMODE(os.stat(keeper.store.cookies_path).st_mode), 0o600)


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
        self.assertEqual(kind("https://accounts.google.com/v3/signin/challenge/pk"), "passkey")
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


class _FakeElement:
    def __init__(self, text="", visible=True):
        self.text, self.visible = text, visible

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.text

    def fill(self, value):
        self.filled = value

    def click(self):
        pass

    def press(self, key):
        pass


class _FakeLocator:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]

    @property
    def first(self):
        return self.elements[0]


class _FakeSignInPage:
    """Just enough of a Playwright page to stand in for one Google sign-in step."""

    def __init__(self, url="https://accounts.google.com/v3/signin/identifier", elements=None, body=""):
        self.url, self.elements, self.body = url, dict(elements or {}), body

    def locator(self, selector):
        return _FakeLocator(self.elements.get(selector, []))

    def inner_text(self, selector):
        return self.body

    def goto(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass


class GoogleSignInPageTests(TestCase):
    """Google announces each step's heading in an assertive live region, for
    screen readers. ash-kuhao's sign-in on 2026-11-22 failed with "Google did
    not accept that email address: Welcome" because that announcement on the
    password page was read as an error."""

    def password_page(self):
        return _FakeSignInPage(
            url="https://accounts.google.com/v3/signin/challenge/pwd",
            elements={
                "h1": [_FakeElement("Welcome")],
                '[aria-live="assertive"]': [_FakeElement("Welcome")],
                'input[name="Passwd"]': [_FakeElement()],
            },
        )

    def test_the_heading_announcement_is_not_an_error(self):
        self.assertEqual(YouTubeAdapter()._error_text(self.password_page()), "")

    def test_an_announcement_that_adds_to_the_heading_is_not_an_error_either(self):
        page = self.password_page()
        page.elements['[aria-live="assertive"]'] = [_FakeElement("Welcome\n  bot@example.com")]
        self.assertEqual(YouTubeAdapter()._error_text(page), "")

    def test_a_real_error_is_still_read(self):
        page = _FakeSignInPage(elements={
            "h1": [_FakeElement("Sign in")],
            '[aria-live="assertive"]': [_FakeElement("Couldn't find your Google Account")],
        })
        self.assertEqual(YouTubeAdapter()._error_text(page), "Couldn't find your Google Account")

    def test_without_a_heading_live_text_is_still_an_error(self):
        page = _FakeSignInPage(elements={'[aria-live="assertive"]': [_FakeElement("Enter a valid email")]})
        self.assertEqual(YouTubeAdapter()._error_text(page), "Enter a valid email")

    def test_the_welcome_page_goes_on_to_the_password(self):
        page = self.password_page()
        page.elements['input#identifierId'] = [_FakeElement()]
        adapter = YouTubeAdapter()
        adapter._after_password = Mock()
        job = Mock()

        adapter.login(page, "bot@example.com", "secret", job)

        job.fail.assert_not_called()
        self.assertEqual(page.elements['input[name="Passwd"]'][0].filled, "secret")
        adapter._after_password.assert_called_once()

    def test_a_visible_password_box_wins_over_any_live_text(self):
        page = self.password_page()
        page.elements["h1"] = [_FakeElement("Hi there")]
        page.elements['input#identifierId'] = [_FakeElement()]
        adapter = YouTubeAdapter()
        adapter._after_password = Mock()
        job = Mock()

        adapter.login(page, "bot@example.com", "secret", job)

        job.fail.assert_not_called()
        adapter._after_password.assert_called_once()


IDENTIFIER_URL = "https://accounts.google.com/v3/signin/identifier"
PASSWORD_URL = "https://accounts.google.com/v3/signin/challenge/pwd"


class _ScriptedSignInPage(_FakeSignInPage):
    """A sign-in page that changes while the bot waits, as Google's does.

    Google's sign-in is one page: Next updates the address and animates the
    next step in, and the old step stays in the document, hidden. Every wait
    moves this page's own clock on, and each scripted step is applied once its
    time has come, so a thirty second wait costs nothing in a test.
    """

    def __init__(self, steps, **kwargs):
        super().__init__(**kwargs)
        self.now = 0.0
        self.steps = sorted(steps, key=lambda step: step[0])

    def monotonic(self):
        return self.now

    def wait_for_timeout(self, ms):
        self.now += ms / 1000
        while self.steps and self.steps[0][0] <= self.now:
            self.steps.pop(0)[1](self)


def _welcome_arrives(page):
    """The moment after Next: the address and the announcement have moved on,
    the email step is hidden, and the password box is not showing yet."""
    page.url = PASSWORD_URL
    page.elements["h1"] = [_FakeElement("Sign in", visible=False), _FakeElement("Welcome", visible=False)]
    page.elements['[aria-live="assertive"]'] = [_FakeElement("Welcome")]
    page.elements["input#identifierId"] = [_FakeElement(visible=False)]
    page.elements["#identifierNext button"] = [_FakeElement(visible=False)]


def _password_box_shows(page):
    page.elements["h1"] = [_FakeElement("Sign in", visible=False), _FakeElement("Welcome")]
    page.elements['input[name="Passwd"]'] = [_FakeElement()]


class GoogleSignInStepTests(TestCase):
    """kuhao-bot's sign-in on 2026-11-22 still failed with "Google did not
    accept that email address: Welcome" after [097]. The fix looked only at
    the first h1, which on Google's one-page sign-in is the hidden "Sign in" of
    the step already left, and every test used a page that had finished
    changing. These follow the page through the change. See [099]."""

    def email_page(self, steps):
        return _ScriptedSignInPage(
            steps,
            url=IDENTIFIER_URL,
            elements={
                "h1": [_FakeElement("Sign in")],
                "input#identifierId": [_FakeElement()],
                # Google's page has one. Without it the bot looks for a button
                # for a second and a half before pressing Enter, which is long
                # enough to wait out the very moment these tests are about.
                "#identifierNext button": [_FakeElement()],
            },
        )

    def sign_in(self, page):
        from unittest.mock import patch

        adapter = YouTubeAdapter()
        adapter._after_password = Mock()
        job = Mock()
        with patch("bot.services.web.youtube.time", SimpleNamespace(monotonic=page.monotonic)):
            adapter.login(page, "bot@example.com", "secret", job)
        return adapter, job

    def test_the_first_heading_hidden_does_not_hide_the_one_on_screen(self):
        page = _FakeSignInPage(elements={
            "h1": [_FakeElement("Sign in", visible=False), _FakeElement("Welcome")],
            '[aria-live="assertive"]': [_FakeElement("Welcome")],
        })
        adapter = YouTubeAdapter()
        self.assertEqual(adapter._heading(page), "Welcome")
        self.assertEqual(adapter._error_text(page), "")

    def test_an_announcement_before_its_heading_shows_is_not_an_error(self):
        page = _FakeSignInPage(elements={
            "h1": [_FakeElement("Sign in"), _FakeElement("Welcome", visible=False)],
            '[aria-live="assertive"]': [_FakeElement("Welcome")],
        })
        self.assertEqual(YouTubeAdapter()._error_text(page), "")

    def test_the_moment_between_email_and_password_goes_on_to_the_password(self):
        page = self.email_page([(0.5, _welcome_arrives), (2.0, _password_box_shows)])
        adapter, job = self.sign_in(page)

        job.fail.assert_not_called()
        self.assertEqual(page.elements['input[name="Passwd"]'][0].filled, "secret")
        adapter._after_password.assert_called_once()

    def test_a_message_that_only_flashes_up_is_not_an_error(self):
        def flash(page):
            page.elements['[aria-live="assertive"]'] = [_FakeElement("Checking")]

        def gone(page):
            page.elements['[aria-live="assertive"]'] = []

        page = self.email_page([(0.5, flash), (1.0, gone), (3.0, _welcome_arrives), (4.0, _password_box_shows)])
        adapter, job = self.sign_in(page)

        job.fail.assert_not_called()
        adapter._after_password.assert_called_once()

    def test_an_address_google_cannot_find_is_still_reported(self):
        def refused(page):
            page.elements['div[jsname="B34EJ"]'] = [_FakeElement("Couldn't find your Google Account")]

        adapter, job = self.sign_in(self.email_page([(0.5, refused)]))

        job.fail.assert_called_once_with("Google did not accept that email address: Couldn't find your Google Account")
        adapter._after_password.assert_not_called()

    def test_google_asking_for_a_phone_instead_of_a_password_is_followed(self):
        def phone(page):
            page.url = "https://accounts.google.com/v3/signin/challenge/dp"
            page.elements["input#identifierId"] = [_FakeElement(visible=False)]

        adapter, job = self.sign_in(self.email_page([(0.5, phone)]))

        job.fail.assert_not_called()
        adapter._after_password.assert_called_once()

    def test_a_wrong_password_is_reported_on_the_password_step(self):
        from unittest.mock import patch

        page = _ScriptedSignInPage([], url=PASSWORD_URL, elements={
            "h1": [_FakeElement("Sign in", visible=False), _FakeElement("Welcome")],
            '[aria-live="assertive"]': [_FakeElement("Welcome")],
            'input[name="Passwd"]': [_FakeElement()],
            ".Ekjuhf": [_FakeElement("Wrong password. Try again or click Forgot password to reset it.")],
        })
        job = Mock()
        with patch("bot.services.web.youtube.time", SimpleNamespace(monotonic=page.monotonic)):
            YouTubeAdapter()._after_password(page, job)

        job.fail.assert_called_once_with(
            "Google did not accept those details: Wrong password. Try again or click Forgot password to reset it."
        )

    def test_the_password_step_announcing_itself_is_not_a_wrong_password(self):
        from unittest.mock import patch

        def signed_in(page):
            page.url = "https://www.youtube.com/"

        page = _ScriptedSignInPage([(2.0, signed_in)], url=PASSWORD_URL, elements={
            "h1": [_FakeElement("Sign in", visible=False), _FakeElement("Welcome")],
            '[aria-live="assertive"]': [_FakeElement("Welcome")],
            'input[name="Passwd"]': [_FakeElement()],
        })
        adapter = YouTubeAdapter()
        adapter.session_state = Mock(return_value={"logged_in": True, "datasync_id": "x"})
        job = Mock()
        with patch("bot.services.web.youtube.time", SimpleNamespace(monotonic=page.monotonic)):
            adapter._after_password(page, job)

        job.fail.assert_not_called()
        job.set_state.assert_called_with(job.set_state.call_args[0][0], signed_in=True)


PASSKEY_URL = "https://accounts.google.com/v3/signin/challenge/pk"
SELECTION_URL = "https://accounts.google.com/v3/signin/challenge/selection"
ANOTHER_WAY = 'button:has-text("Try another way")'
ENTER_PASSWORD = '[role="link"]:has-text("Enter your password")'


class _Clickable(_FakeElement):
    """An element whose click changes the page, as Google's links do."""

    def __init__(self, page, on_click, text=""):
        super().__init__(text)
        self.page, self.on_click, self.clicks = page, on_click, 0

    def click(self):
        self.clicks += 1
        self.on_click(self.page)


def _passkey_arrives(page):
    """What private-bot met on 2026-11-23: Google took the address and asked
    for a passkey, with no password box anywhere."""
    page.url = PASSKEY_URL
    page.elements["h1"] = [_FakeElement("Sign in", visible=False), _FakeElement("Use your passkey")]
    page.elements['[aria-live="assertive"]'] = [_FakeElement("Use your passkey")]
    page.elements["input#identifierId"] = [_FakeElement(visible=False)]
    page.elements["#identifierNext button"] = [_FakeElement(visible=False)]
    page.elements[ANOTHER_WAY] = [_Clickable(page, _list_of_ways_arrives, "Try another way")]


def _list_of_ways_arrives(page):
    page.url = SELECTION_URL
    page.elements[ANOTHER_WAY] = []
    page.elements["h1"] = [_FakeElement("Sign in", visible=False), _FakeElement("Choose how you want to sign in")]
    page.elements[ENTER_PASSWORD] = [_Clickable(page, _password_step_arrives, "Enter your password")]


def _password_step_arrives(page):
    page.url = PASSWORD_URL
    page.elements[ENTER_PASSWORD] = []
    page.elements["h1"] = [_FakeElement("Sign in", visible=False), _FakeElement("Welcome")]
    page.elements['input[name="Passwd"]'] = [_FakeElement()]


class GooglePasskeyTests(TestCase):
    """private-bot's sign-in on 2026-11-23, the first live try of [099], got
    past the email and then failed with "Google asked for a kind of
    verification the bot cannot complete": Google went to its passkey page,
    which the bot's Chrome cannot answer. The page links to the password the
    account still has, and the bot now follows that link. See [100]."""

    email_page = GoogleSignInStepTests.email_page
    sign_in = GoogleSignInStepTests.sign_in

    def test_the_bot_takes_another_way_to_the_password(self):
        page = self.email_page([(0.5, _passkey_arrives)])
        adapter, job = self.sign_in(page)

        job.fail.assert_not_called()
        self.assertEqual(page.elements['input[name="Passwd"]'][0].filled, "secret")
        adapter._after_password.assert_called_once()

    def test_a_passkey_page_that_goes_straight_to_the_password_works_too(self):
        def passkey_then_password(page):
            _passkey_arrives(page)
            page.elements[ANOTHER_WAY] = [_Clickable(page, _password_step_arrives, "Try another way")]

        page = self.email_page([(0.5, passkey_then_password)])
        adapter, job = self.sign_in(page)

        job.fail.assert_not_called()
        self.assertEqual(page.elements['input[name="Passwd"]'][0].filled, "secret")
        adapter._after_password.assert_called_once()

    def test_a_list_without_a_password_says_how_to_turn_passkeys_off(self):
        def list_without_password(page):
            _list_of_ways_arrives(page)
            page.elements[ENTER_PASSWORD] = []

        def passkey(page):
            _passkey_arrives(page)
            page.elements[ANOTHER_WAY] = [_Clickable(page, list_without_password, "Try another way")]

        adapter, job = self.sign_in(self.email_page([(0.5, passkey)]))

        message = job.fail.call_args[0][0]
        self.assertIn("passkey", message)
        self.assertIn("Skip password when possible", message)
        adapter._after_password.assert_not_called()

    def test_another_way_is_pressed_once_not_over_and_over(self):
        def passkey_that_stays(page):
            _passkey_arrives(page)
            page.elements[ANOTHER_WAY] = [_Clickable(page, lambda p: None, "Try another way")]

        page = self.email_page([(0.5, passkey_that_stays)])
        adapter, job = self.sign_in(page)

        self.assertEqual(page.elements[ANOTHER_WAY][0].clicks, 1)
        job.fail.assert_called_once()
