"""AudioVault: search, sign-in, downloads, unpacking, and the av questions.

The listing fixtures are the real pages audiovault.net served on 2026-10-31
(signed out). Sign-in and downloads run against a local stand-in server that
behaves the way the live site was seen to: a CSRF token on /login, a redirect
back to /login for wrong details, and /download redirecting to /login when
signed out. What the real site sends for a download when signed in has not been
measured, so both shapes it might take (one audio file, a zip) are covered.
"""

import io
import os
import shutil
import stat
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from urllib.parse import parse_qs

from bot import errors
from bot.commands.audiovault_commands import MovieQuestion, ResultsQuestion, ShowQuestion
from bot.services.audiovault import (
    AudioVaultClient,
    Entry,
    Title,
    as_zip,
    group_titles,
    parse_listing,
    speakable,
    unpack,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures", "audiovault")
USER = SimpleNamespace(id=7, username="listener")
BIG = 10 * 1024 * 1024


def translate(text):
    return text


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buffer.getvalue()


class ListingTests(TestCase):
    def test_a_movie_search_reads_each_rows_id_and_name(self):
        entries = parse_listing(fixture("movies_spaceballs.html"))

        self.assertEqual(
            [(e.id, e.name) for e in entries],
            [(26836, "Spaceballs (1987) [AudioVault Original]"), (26423, "Spaceballs (1987) [TTS]")],
        )

    def test_a_shows_seasons_are_gathered_under_one_title_in_order(self):
        shows = parse_listing(fixture("shows_family_guy.html"))
        titles = group_titles([], shows, translate)

        # The live page lists 25 seasons (one as "season 24", in lower case, and
        # season 9 twice) and two single episodes, all of which belong to the show.
        family_guy = [t for t in titles if t.name.startswith("Family Guy")]
        self.assertEqual([t.name for t in family_guy], ["Family Guy"])
        seasons = family_guy[0].seasons
        self.assertEqual(len(seasons), len([e for e in shows if e.name.startswith("Family Guy")]))
        self.assertEqual(seasons[0].label, "Season 1 (1999)")
        self.assertIn("Season 24 (2026)", [s.label for s in seasons])
        self.assertIn("Season 9, episode 9: Road to the North Pole Pt 2", [s.label for s in seasons])
        self.assertIn("Season 9 (2010), Not Described", [s.label for s in seasons])
        self.assertEqual([s.number for s in seasons], sorted(s.number for s in seasons))

    def test_tags_are_spoken_without_brackets_and_tts_is_explained(self):
        self.assertEqual(speakable("Spaceballs (1987) [TTS]", translate),
                         "Spaceballs (1987), text-to-speech narration")
        self.assertEqual(speakable("Halloween (1978) [UK]", translate), "Halloween (1978), UK")


class UnpackTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_a_season_zip_comes_out_in_episode_order_without_its_folders(self):
        path = self.write("s1.zip", make_zip([
            ("Show/Episode 10.mp3", b"ten"), ("Show/Episode 2.mp3", b"two"),
            ("Show/cover.jpg", b"img"), ("Show/Episode 1.mp3", b"one"),
        ]))

        files = unpack(path, os.path.join(self.tmp, "out"), BIG, translate)

        self.assertEqual([os.path.basename(f) for f in files],
                         ["Episode 1.mp3", "Episode 2.mp3", "Episode 10.mp3"])

    def test_a_single_audio_file_is_played_as_it_is(self):
        path = self.write("film.mp3", b"audio")
        self.assertEqual(unpack(path, os.path.join(self.tmp, "out"), BIG, translate), [path])

    def test_a_zip_reaching_outside_its_folder_is_refused_before_anything_is_written(self):
        path = self.write("bad.zip", make_zip([("ok.mp3", b"a"), ("../../evil.mp3", b"b")]))

        with self.assertRaises(errors.ServiceError) as caught:
            unpack(path, os.path.join(self.tmp, "out"), BIG, translate)
        self.assertIn("not safe", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")))

    def test_a_zip_larger_than_allowed_is_refused(self):
        path = self.write("big.zip", make_zip([("a.mp3", b"x" * 2000)]))
        with self.assertRaises(errors.ServiceError) as caught:
            unpack(path, os.path.join(self.tmp, "out"), 1000, translate)
        self.assertIn("larger than this bot allows", str(caught.exception))

    def test_something_that_is_neither_audio_nor_a_zip_is_named(self):
        path = self.write("page.html", b"<html>")
        with self.assertRaises(errors.ServiceError) as caught:
            unpack(path, os.path.join(self.tmp, "out"), BIG, translate)
        self.assertIn("not audio or a zip file", str(caught.exception))

    def test_checking_only_writes_nothing(self):
        path = self.write("s1.zip", make_zip([("e1.mp3", b"a")]))
        self.assertEqual(unpack(path, None, BIG, translate), ["e1.mp3"])

    def test_uploads_are_always_zip_files(self):
        mp3 = self.write("film.mp3", b"audio")
        archive = as_zip(mp3, "Spaceballs (1987) [TTS]", self.tmp)
        self.assertTrue(archive.endswith("Spaceballs (1987) [TTS].zip"))
        self.assertEqual(zipfile.ZipFile(archive).namelist(), ["film.mp3"])

        season = self.write("dl.zip", make_zip([("e1.mp3", b"a")]))
        archive = as_zip(season, "Family Guy - Season 01 (1999)", self.tmp)
        self.assertEqual(zipfile.ZipFile(archive).namelist(), ["e1.mp3"])


class StandIn(BaseHTTPRequestHandler):
    """Behaves like audiovault.net as far as it was seen to."""

    def log_message(self, *args):
        pass

    def _signed_in(self):
        return "remember_web_test=yes" in (self.headers.get("Cookie") or "")

    def _redirect(self, where, cookie=None):
        self.send_response(302)
        self.send_header("Location", where)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _send(self, body, content_type, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/login":
            return self._send(b'<form><input type="hidden" name="_token" value="tok123"></form>',
                              "text/html")
        if path == "/":
            return self._send(b"home", "text/html")
        if path == "/movies":
            return self._send(fixture("movies_spaceballs.html").encode(), "text/html")
        if path == "/shows":
            return self._send(fixture("shows_family_guy.html").encode(), "text/html")
        if path.startswith("/download/"):
            if not self._signed_in():
                return self._redirect("/login")
            entry = path.rsplit("/", 1)[1]
            if entry == "1":
                return self._send(make_zip([("e2.mp3", b"b"), ("e1.mp3", b"a")]), "application/zip",
                                  {"Content-Disposition": 'attachment; filename="Show - Season 01.zip"'})
            if entry == "2":
                return self._send(b"ID3audio", "audio/mpeg")
            return self._send(b"<html>error</html>", "text/html")
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        if form.get("_token") == ["tok123"] and form.get("password") == ["right"]:
            return self._redirect("/", cookie="remember_web_test=yes; Path=/")
        return self._redirect("/login")


class ClientTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.creds = ("me@example.com", "right")

    def client(self):
        return AudioVaultClient(os.path.join(self.tmp, "av", "session.json"),
                                lambda: self.creds, translate, base_url=self.base)

    def test_signing_in_keeps_the_session_in_a_private_file(self):
        client = self.client()
        client.sign_in("me@example.com", "right")

        path = os.path.join(self.tmp, "av", "session.json")
        self.assertTrue(os.path.isfile(path))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # A fresh client, as after a restart, is still signed in.
        self.assertTrue(self.client().fetch(Entry(2, "Film"), self.tmp, BIG).endswith(".mp3"))

    def test_a_wrong_password_is_said_plainly(self):
        with self.assertRaises(errors.ServiceError) as caught:
            self.client().sign_in("me@example.com", "wrong")
        self.assertIn("did not accept", str(caught.exception))

    def test_an_ended_session_signs_in_again_with_the_stored_details(self):
        path = self.client().fetch(Entry(1, "Show - Season 01 (1999)"), self.tmp, BIG)

        self.assertTrue(path.endswith("Show - Season 01.zip"))
        self.assertTrue(zipfile.is_zipfile(path))

    def test_no_stored_details_points_to_li_av(self):
        self.creds = ("", "")
        with self.assertRaises(errors.NotSignedInError) as caught:
            self.client().fetch(Entry(1, "x"), self.tmp, BIG)
        self.assertTrue(str(caught.exception).endswith("send: li av"))

    def test_a_web_page_instead_of_a_file_is_refused(self):
        client = self.client()
        client.sign_in(*self.creds)
        with self.assertRaises(errors.ServiceError) as caught:
            client.fetch(Entry(3, "x"), self.tmp, BIG)
        self.assertIn("not audio or a zip file", str(caught.exception))

    def test_search_reads_both_films_and_shows(self):
        titles = self.client().search("anything")
        kinds = {t.kind for t in titles}
        self.assertEqual(kinds, {"movie", "show"})


class QuestionTests(TestCase):
    def setUp(self):
        self.movie = Title("movie", "Spaceballs (1987), text-to-speech narration", entry=Entry(1, "m"))
        shows = [Entry(10 + n, f"Family Guy - Season {n:02d} ({1998 + n})") for n in (1, 2, 3)]
        self.show = group_titles([], shows, translate)[0]

    def test_results_end_each_line_with_a_number_or_command(self):
        question = ResultsQuestion("family guy", [self.show, self.movie], translate, lambda t: t.name)

        self.assertEqual(question.question().split("\n"), [
            "AudioVault results for family guy: 2",
            "1: Family Guy, TV show, seasons: 3",
            "2: Spaceballs (1987), text-to-speech narration, movie",
            "To choose one, send its number.",
            "To cancel, send: 0",
        ])
        self.assertEqual(question.answer("2"), (self.movie.name, False))
        self.assertEqual(question.answer("0"), ("Cancelled.", False))
        self.assertTrue(question.answer("9")[1])
        self.assertIsNone(question.answer("p something"))

    def test_a_film_streams_on_1_and_downloads_on_2(self):
        question = MovieQuestion(self.movie, translate, stream=lambda: "streaming",
                                 download=lambda: "downloading")
        self.assertTrue(question.question().endswith("To cancel, send: 0"))
        self.assertEqual(question.answer("1"), ("streaming", False))
        self.assertEqual(question.answer("2"), ("downloading", False))

    def test_a_show_streams_a_number_downloads_d_and_a_number_or_all(self):
        calls = []
        question = ShowQuestion(
            self.show, translate,
            stream=lambda i: calls.append(("stream", i)) or "s",
            download=lambda i: calls.append(("download", i)) or "d",
            download_all=lambda: calls.append(("all",)) or "a",
        )

        lines = question.question().split("\n")
        self.assertEqual(lines[0], "Family Guy. Seasons: 3")
        self.assertEqual(lines[1], "1: Season 1 (1999)")
        question.answer("3")
        question.answer("d 2")
        question.answer("all")
        self.assertEqual(calls, [("stream", 2), ("download", 1), ("all",)])
        self.assertTrue(question.answer("d 9")[1])
        self.assertIsNone(question.answer("hello"))


class JobTests(TestCase):
    def make(self, connected=True):
        from bot.modules.audiovault_jobs import AudioVaultJobs

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = Mock()
        service = SimpleNamespace(
            is_enabled=True, is_connected=lambda: connected, client=self.client, max_bytes=BIG,
        )
        self.player = Mock()
        bot = SimpleNamespace(
            translator=SimpleNamespace(translate=translate), ttclient=Mock(), player=self.player,
            service_manager=SimpleNamespace(services={"av": service}),
            config_manager=SimpleNamespace(config_dir=self.tmp),
        )
        self.uploader = Mock()
        self.uploader.upload_file.return_value = True
        return AudioVaultJobs(bot, self.uploader)

    def wait(self):
        for thread in threading.enumerate():
            if thread.name == "AudioVaultJob":
                thread.join(timeout=10)

    def fetch_zip(self, names):
        def fetch(entry, dest, max_bytes):
            path = os.path.join(dest, f"{entry.id}.zip")
            with open(path, "wb") as f:
                f.write(make_zip([(n, b"x") for n in names]))
            return path
        return fetch

    def test_not_connected_points_to_li_av(self):
        jobs = self.make(connected=False)
        self.assertTrue(jobs.stream("Film", Entry(1, "x"), USER, False).endswith("send: li av"))
        self.client.fetch.assert_not_called()

    def test_a_season_is_unpacked_and_its_episodes_queued_in_order(self):
        jobs = self.make()
        self.client.fetch.side_effect = self.fetch_zip(["Ep 2.mp3", "Ep 1.mp3"])

        started = jobs.stream("Family Guy, Season 1", Entry(5, "x"), USER, True)
        self.wait()

        self.assertIn("ready to stream", started)
        tracks = self.player.play.call_args[0][0]
        self.assertEqual([t.name for t in tracks],
                         ["Family Guy, Season 1, Ep 1", "Family Guy, Season 1, Ep 2"])
        self.assertTrue(all(os.path.isfile(t.url) for t in tracks))
        jobs.ttclient.send_message.assert_called_with(
            "Family Guy, Season 1 is playing.\nEpisodes: 2", USER
        )
        self.assertEqual(os.listdir(os.path.join(self.tmp, "downloads")), [])

    def test_a_second_job_waits_for_the_first(self):
        jobs = self.make()
        jobs._busy.acquire()
        message = jobs.download("Film", Entry(1, "x"), USER)
        self.assertTrue(message.endswith("for example: av spaceballs"))

    def test_every_season_uploads_its_own_zip_and_names_the_ones_that_failed(self):
        jobs = self.make()
        ok = self.fetch_zip(["e.mp3"])

        def fetch(entry, dest, max_bytes):
            if entry.id == 2:
                raise errors.ServiceError("AudioVault did not have that download.")
            return ok(entry, dest, max_bytes)

        self.client.fetch.side_effect = fetch
        seasons = [("Season 1", Entry(1, "Show - Season 01")), ("Season 2", Entry(2, "Show - Season 02")),
                   ("Season 3", Entry(3, "Show - Season 03"))]

        jobs.download_all("Show", seasons, USER)
        self.wait()

        self.assertEqual(self.uploader.upload_file.call_count, 2)
        uploaded = [os.path.basename(c[0][0]) for c in self.uploader.upload_file.call_args_list]
        self.assertEqual(uploaded, ["Show - Season 01.zip", "Show - Season 03.zip"])
        self.assertEqual(jobs.ttclient.send_message.call_args[0][0].split("\n"), [
            "The seasons of Show are in the channel's files.",
            "Zip files: 2",
            "Seasons that could not be downloaded: Season 2",
        ])

    def test_no_season_downloaded_says_so_with_the_reason(self):
        jobs = self.make()
        self.client.fetch.side_effect = errors.NotSignedInError(
            "av", "AudioVault did not accept the sign-in. To connect again, send: li av")

        jobs.download_all("Show", [("Season 1", Entry(1, "a")), ("Season 2", Entry(2, "b"))], USER)
        self.wait()

        self.assertEqual(jobs.ttclient.send_message.call_args[0][0].split("\n"), [
            "Could not download any season of Show.",
            "AudioVault did not accept the sign-in. To connect again, send: li av",
        ])


class PortalFormTests(TestCase):
    def test_the_sign_in_form_asks_for_an_audiovault_email_address(self):
        from bot.modules.portal_pages import PageBuilder
        from test_auth_portal import FakeTranslator

        html = PageBuilder(FakeTranslator(), "en").credentials_page("tok", "av")

        self.assertIn("AudioVault email address", html)
        self.assertIn('id="username" name="username" type="email"', html)
        self.assertIn('autocomplete="username"', html)
        self.assertNotIn("maxlength", html)
        # Every other service keeps the plain text field it had.
        netflix = PageBuilder(FakeTranslator(), "en").credentials_page("tok", "nf")
        self.assertIn('id="username" name="username" type="text"', netflix)


class RoutingTests(TestCase):
    def test_a_number_answers_the_open_audiovault_question(self):
        from bot.commands import CommandProcessor

        sent = []
        picked = []
        question = ResultsQuestion("q", [Title("movie", "Film", entry=Entry(1, "f"))], translate,
                                   lambda t: picked.append(t) or "picked")
        processor = SimpleNamespace(
            pending_ad_prompt={}, pending_playlist_choice={}, pending_artist_choice={},
            pending_format_choice={}, pending_audiovault={USER.id: question}, pending_config_menu={},
            pending_playlist_download={}, pending_artist_download={}, pending_ads_option={},
            pending_podcast_count={},
            ttclient=SimpleNamespace(send_message=lambda text, user=None: sent.append(text)),
            translator=SimpleNamespace(translate=translate),
            parse_command=Mock(), check_access=Mock(return_value=False),
        )

        CommandProcessor._run(processor, SimpleNamespace(user=USER, text="1"))

        self.assertEqual(sent, ["picked"])
        self.assertEqual(len(picked), 1)


if __name__ == "__main__":
    unittest.main()
