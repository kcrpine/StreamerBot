"""The browser engine, the Netflix adapter, and the audio description prompt.

No real browser is launched here. What is pinned is the decision logic: that
every Playwright call goes through one thread, that a described audio track is
recognised across locales, and that an unanswered prompt cannot hang playback.
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot import errors
from bot.modules.audio_description import (
    AudioDescriptionPreference,
    PendingPrompt,
    parse_answer,
    prompt_text,
)
from bot.player.engines.browser_engine import BrowserEngine
from bot.services.web.netflix import NetflixAdapter


def make_engine():
    engine = object.__new__(BrowserEngine)
    import queue
    engine._dir = "/tmp/browser-test"
    engine._display = ":99"
    engine._queue = queue.Queue()
    engine._thread = None
    engine._closing = False
    engine._ready = threading.Event()
    engine._start_error = None
    engine._playwright = None
    engine._contexts = {}
    engine._pages = {}
    engine._adapters = {}
    engine._active_service = None
    engine._playing = False
    engine.on_end = lambda e, r: None
    return engine


class JobChannelTests(TestCase):
    """Playwright's sync API is not thread-safe, so everything is a queued job."""

    def test_submit_runs_the_work_and_returns_its_result(self):
        engine = make_engine()
        engine._thread = SimpleNamespace(is_alive=lambda: True)

        worker = threading.Thread(target=self._drain_one, args=(engine,), daemon=True)
        worker.start()

        self.assertEqual(engine.submit(lambda: 6 * 7, timeout=5), 42)

    def test_an_exception_reaches_the_caller_rather_than_being_swallowed(self):
        """A silent failure would look like a site change."""
        engine = make_engine()
        engine._thread = SimpleNamespace(is_alive=lambda: True)
        threading.Thread(target=self._drain_one, args=(engine,), daemon=True).start()

        def boom():
            raise ValueError("selector not found")

        with self.assertRaises(ValueError):
            engine.submit(boom, timeout=5)

    def test_submitting_to_a_dead_engine_is_an_error_not_a_hang(self):
        engine = make_engine()
        engine._thread = None

        with self.assertRaises(errors.ServiceError):
            engine.submit(lambda: 1, timeout=1)

    def test_a_job_that_never_finishes_times_out(self):
        """One wedged job must not block the caller forever."""
        engine = make_engine()
        engine._thread = SimpleNamespace(is_alive=lambda: True)
        # Nothing drains the queue.

        with self.assertRaises(errors.ServiceError):
            engine.submit(lambda: 1, timeout=0.3)

    @staticmethod
    def _drain_one(engine):
        job = engine._queue.get()
        if job is None:
            return
        try:
            job.result = job.fn()
        except BaseException as error:
            job.error = error
        finally:
            job.done.set()


class ArchitectureGateTests(TestCase):
    def test_no_chrome_marker_means_unsupported(self):
        """arm64 has no Google Chrome and therefore no Widevine."""
        with patch("builtins.open", side_effect=OSError):
            self.assertFalse(BrowserEngine.is_supported())

    def test_marker_zero_means_unsupported(self):
        m = Mock()
        m.__enter__ = Mock(return_value=SimpleNamespace(read=lambda: "0\n"))
        m.__exit__ = Mock(return_value=False)
        with patch("builtins.open", return_value=m):
            self.assertFalse(BrowserEngine.is_supported())

    def test_initialize_refuses_clearly_when_unsupported(self):
        engine = make_engine()
        with patch.object(BrowserEngine, "is_supported", staticmethod(lambda: False)):
            with self.assertRaises(errors.EngineUnavailableError) as caught:
                engine.initialize()
        self.assertIn("Chrome", str(caught.exception))


class AudioTrackMatchingTests(TestCase):
    """The site labels tracks in the account's language, not the bot's."""

    def setUp(self):
        self.adapter = NetflixAdapter()

    def test_english_variants_are_recognised(self):
        for label in ("English - Audio Description", "English [Descriptive Audio]",
                      "English (Described)"):
            found = self.adapter.find_described_track(
                [{"id": "0", "label": "English"}, {"id": "1", "label": label}]
            )
            self.assertEqual(found["id"], "1", label)

    def test_other_locales_are_recognised(self):
        for label in ("Español - Audiodescripción", "Deutsch (Hörfilm)",
                      "Italiano - Audiodescrizione", "Português - Audiodescrição"):
            found = self.adapter.find_described_track([{"id": "9", "label": label}])
            self.assertIsNotNone(found, label)

    def test_an_ordinary_track_is_not_mistaken_for_a_described_one(self):
        tracks = [{"id": "0", "label": "English"}, {"id": "1", "label": "Español"}]

        self.assertIsNone(self.adapter.find_described_track(tracks))

    def test_no_tracks_at_all_is_not_an_error(self):
        self.assertIsNone(self.adapter.find_described_track([]))


class NetflixIdTests(TestCase):
    def test_a_watch_id_is_extracted_from_both_forms(self):
        adapter = NetflixAdapter()

        self.assertEqual(adapter._watch_id(SimpleNamespace(url="netflix://watch/80100172")), "80100172")
        self.assertEqual(
            adapter._watch_id(SimpleNamespace(url="https://www.netflix.com/watch/70143836")),
            "70143836",
        )

    def test_something_that_is_not_a_netflix_title_yields_nothing(self):
        adapter = NetflixAdapter()

        self.assertIsNone(adapter._watch_id(SimpleNamespace(url="spotify:track:abc")))


class PreferenceTests(TestCase):
    def test_the_configured_default_applies_until_a_user_chooses(self):
        preference = AudioDescriptionPreference("always")

        self.assertEqual(preference.get(1), "always")
        preference.set(1, "never")
        self.assertEqual(preference.get(1), "never")

    def test_one_users_choice_does_not_affect_another(self):
        preference = AudioDescriptionPreference("ask")
        preference.set(1, "always")

        self.assertEqual(preference.get(2), "ask")

    def test_an_invalid_configured_default_falls_back_to_asking(self):
        self.assertEqual(AudioDescriptionPreference("nonsense").default, "ask")


class AnswerParsingTests(TestCase):
    def test_the_numbered_options(self):
        self.assertEqual(parse_answer("1"), (True, False))
        self.assertEqual(parse_answer("2"), (False, False))
        self.assertEqual(parse_answer("3"), (True, True))
        self.assertEqual(parse_answer("4"), (False, True))

    def test_words_are_accepted_too(self):
        """Someone who hears "1. Yes" will sometimes type "yes"."""
        self.assertEqual(parse_answer("yes"), (True, False))
        self.assertEqual(parse_answer("No"), (False, False))
        self.assertEqual(parse_answer(" Y "), (True, False))

    def test_anything_else_is_not_an_answer(self):
        for text in ("", "p something", "5", "maybe"):
            self.assertIsNone(parse_answer(text), text)


class PromptTests(TestCase):
    def test_an_answer_is_delivered_once(self):
        answers = []
        prompt = PendingPrompt(1, "nf", lambda e, r: answers.append((e, r)))

        self.assertTrue(prompt.answer(True, False))
        self.assertFalse(prompt.answer(False, True))  # second answer ignored
        self.assertEqual(answers, [(True, False)])

    def test_an_unanswered_prompt_falls_back_rather_than_hanging(self):
        """Playback must never wait on a question nobody answered."""
        answers = []
        PendingPrompt(1, "nf", lambda e, r: answers.append((e, r)), timeout=0.2)

        time.sleep(0.5)

        self.assertEqual(answers, [(False, False)])

    def test_a_timeout_is_not_remembered_as_a_choice(self):
        answers = []
        PendingPrompt(1, "nf", lambda e, r: answers.append((e, r)), timeout=0.2)
        time.sleep(0.5)

        self.assertFalse(answers[0][1])

    def test_cancelling_stops_the_fallback_firing(self):
        answers = []
        prompt = PendingPrompt(1, "nf", lambda e, r: answers.append((e, r)), timeout=0.2)
        prompt.cancel()

        time.sleep(0.5)

        self.assertEqual(answers, [])

    def test_the_question_is_one_message_with_all_four_options(self):
        """Five sends would be five separate screen reader announcements."""
        text = prompt_text(SimpleNamespace(translate=lambda s: s), "Some Film")

        self.assertEqual(text.count("\n"), 0)
        for option in ("1.", "2.", "3.", "4."):
            self.assertIn(option, text)


if __name__ == "__main__":
    unittest.main()
