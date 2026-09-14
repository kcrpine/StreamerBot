"""Apple Music search and playback, pinned to what Apple's live site did.

Measured on 14 September 2026 in the bot's own Chrome, after sign-in started
working and playback did not:

- play() called MusicKit's play() with nothing queued. The queue stayed at 0 and
  playback never started, for a song and an album alike: the 30-second
  "did not start playing" timeout every time. setQueue({url}) then play() started
  both at once.
- The search URL had no storefront. Apple redirected /search?term=adventure%20of%20a%20lifetime
  to /us/search?term=adventure%2Bof%2Ba%2Blifetime, turning the spaces into
  literal plus signs, so it searched for "adventure+of+a+lifetime" and ranked
  Chubb+Bits first. With /us/ in the URL, the Coldplay song was first.
- The scraper took every album, playlist and artist link on the page. Signed in,
  that includes the sidebar's own library, which is how a search for a Coldplay
  song played "Favorite Songs".
"""

import unittest
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from bot.services.web.apple_music import AppleMusicAdapter


class SearchUrlTests(TestCase):
    def test_the_storefront_is_always_in_the_url(self):
        """Without it Apple redirects and turns spaces into plus signs."""
        url = AppleMusicAdapter.search_url("us", "adventure of a lifetime")
        self.assertTrue(url.startswith("https://music.apple.com/us/search?term="))

    def test_spaces_are_percent_encoded_never_plus(self):
        url = AppleMusicAdapter.search_url("us", "adventure of a lifetime")
        self.assertIn("adventure%20of%20a%20lifetime", url)
        self.assertNotIn("+", url)

    def test_a_real_plus_sign_survives_as_a_plus(self):
        """"C++" must not arrive as "C  "."""
        self.assertIn("C%2B%2B", AppleMusicAdapter.search_url("us", "C++"))

    def test_a_slash_does_not_become_a_path(self):
        """"AC/DC" would otherwise break the URL into two path segments."""
        url = AppleMusicAdapter.search_url("us", "AC/DC")
        self.assertIn("AC%2FDC", url)
        self.assertEqual(url.count("/search"), 1)

    def test_other_storefronts_are_used_as_given(self):
        self.assertIn("/gb/search", AppleMusicAdapter.search_url("gb", "x"))


class StorefrontTests(TestCase):
    def test_read_from_an_apple_music_address(self):
        f = AppleMusicAdapter.storefront_from_url
        self.assertEqual(f("https://music.apple.com/us/browse"), "us")
        self.assertEqual(f("https://music.apple.com/gb"), "gb")
        self.assertEqual(f("https://music.apple.com/de?l=en"), "de")

    def test_not_invented_from_other_addresses(self):
        f = AppleMusicAdapter.storefront_from_url
        self.assertIsNone(f("https://music.apple.com/search?term=x"))
        self.assertIsNone(f("https://music.apple.com/includes/commerce/authenticate"))
        self.assertIsNone(f("https://example.com/us/browse"))
        self.assertIsNone(f(""))

    def test_musickit_wins_because_it_is_the_accounts_own_country(self):
        adapter = AppleMusicAdapter()
        page = MagicMock()
        page.url = "https://music.apple.com/us/browse"
        page.evaluate.return_value = "GB"
        with patch.object(adapter, "_musickit_ready", return_value=True):
            self.assertEqual(adapter._storefront(page), "gb")

    def test_falls_back_to_us_rather_than_failing_the_search(self):
        adapter = AppleMusicAdapter()
        page = MagicMock()
        page.url = "about:blank"
        page.goto.side_effect = Exception("offline")
        with patch.object(adapter, "_musickit_ready", return_value=False):
            self.assertEqual(adapter._storefront(page), "us")


class ScrapeRulesTests(TestCase):
    """The rules live in JavaScript, so the ones that went wrong are pinned by
    reading it; the live behaviour was verified separately."""

    def setUp(self):
        self.js = AppleMusicAdapter.SEARCH_SCRAPE_JS

    def test_only_links_inside_main_are_taken(self):
        self.assertIn("document.querySelector('main')", self.js)

    def test_navigation_and_the_library_are_never_results(self):
        """The sidebar library is how "Favorite Songs" answered a song search."""
        self.assertIn("/library/", self.js)
        self.assertIn("nav, aside", self.js)

    def test_music_videos_are_not_offered_on_a_music_bot(self):
        self.assertIn("/music-video/", self.js)

    def test_top_results_read_their_kind_from_apples_label(self):
        # The JavaScript splits Apple's label on the middle-dot escape, as in
        # "Adventure of a Lifetime, dot, Song, dot, Coldplay".
        self.assertIn("\\u00b7", self.js)
        self.assertIn("top_kind", self.js)


class SpokenTitleTests(TestCase):
    def test_a_top_result_says_what_it_is_and_who_by(self):
        """Its group label is only "Top result", so the kind has to be in the
        title or a listener cannot tell a song from an album."""
        title = AppleMusicAdapter.spoken_title(
            {"kind": "top", "top_kind": "track", "title": "Adventure of a Lifetime", "artist": "Coldplay"}
        )
        self.assertEqual(title, "Song: Adventure of a Lifetime, by Coldplay")

    def test_an_ordinary_result_adds_only_the_artist(self):
        title = AppleMusicAdapter.spoken_title(
            {"kind": "album", "title": "A Head Full of Dreams", "artist": "Coldplay"}
        )
        self.assertEqual(title, "A Head Full of Dreams, by Coldplay")

    def test_no_dangling_by_without_an_artist(self):
        self.assertEqual(AppleMusicAdapter.spoken_title({"kind": "track", "title": "X"}), "X")

    def test_an_artist_is_not_said_to_be_by_itself(self):
        self.assertEqual(
            AppleMusicAdapter.spoken_title({"kind": "artist", "title": "Coldplay", "artist": "Coldplay"}),
            "Coldplay",
        )


class ArtistsNeverLeadTests(TestCase):
    """`p <query>` plays the first result, and an artist cannot be queued."""

    def test_a_top_artist_is_moved_to_the_artist_group(self):
        adapter = AppleMusicAdapter()
        page = MagicMock()
        scraped = [
            {"kind": "top", "top_kind": "artist", "title": "Coldplay", "url": "https://music.apple.com/us/artist/coldplay/1"},
            {"kind": "top", "top_kind": "track", "title": "Yellow", "artist": "Coldplay", "url": "https://music.apple.com/us/album/x/2?i=3"},
        ]
        with patch.object(adapter, "_storefront", return_value="us"), \
             patch.object(adapter, "_settled_results", return_value=scraped):
            results = adapter.search(page, "coldplay")

        kinds = {r["url"]: r["kind"] for r in results}
        self.assertEqual(kinds["https://music.apple.com/us/artist/coldplay/1"], "artist")
        self.assertEqual(kinds["https://music.apple.com/us/album/x/2?i=3"], "top")


class SettleTests(TestCase):
    def test_returns_once_two_readings_agree(self):
        adapter = AppleMusicAdapter()
        page = MagicMock()
        first = [{"url": "a"}]
        settled = [{"url": "b"}, {"url": "c"}]
        page.evaluate.side_effect = [first, settled, settled]

        self.assertEqual(adapter._settled_results(page), settled)
        self.assertEqual(page.evaluate.call_count, 3)

    def test_gives_up_waiting_and_returns_what_it_has(self):
        adapter = AppleMusicAdapter()
        adapter.SETTLE_MAX_MS = 2000
        page = MagicMock()
        page.evaluate.side_effect = [[{"url": str(n)}] for n in range(10)]

        self.assertTrue(adapter._settled_results(page))


def playing_page(queued=1, becomes_playing=True, state=None):
    page = MagicMock()
    page.url = "https://music.apple.com/us/browse"
    calls = {"n": 0}

    def evaluate(script, *args):
        calls["n"] += 1
        if "setQueue" in script:
            return queued
        if "isPlaying: !!k.isPlaying" in script:
            return state
        return True

    page.evaluate.side_effect = evaluate
    if not becomes_playing:
        page.wait_for_function.side_effect = Exception("Timeout 30000ms exceeded")
    return page


class PlayTests(TestCase):
    def setUp(self):
        self.adapter = AppleMusicAdapter()
        patcher = patch.object(self.adapter, "_musickit_ready", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_item_is_queued_before_play_is_called(self):
        """The bug: play() with an empty queue does nothing, forever."""
        page = playing_page()
        self.adapter.play(page, SimpleNamespace(url="https://music.apple.com/us/album/x/1?i=2"))

        script = next(c.args[0] for c in page.evaluate.call_args_list if "setQueue" in c.args[0])
        self.assertLess(script.index("setQueue"), script.index("k.play()"))
        self.assertEqual(
            next(c.args[1] for c in page.evaluate.call_args_list if "setQueue" in c.args[0]),
            "https://music.apple.com/us/album/x/1?i=2",
        )

    def test_the_items_own_page_is_not_loaded_when_musickit_is_already_here(self):
        page = playing_page()
        self.adapter.play(page, SimpleNamespace(url="https://music.apple.com/us/album/x/1"))
        page.goto.assert_not_called()

    def test_an_empty_queue_fails_at_once_without_the_30_second_wait(self):
        page = playing_page(queued=0)
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.play(page, SimpleNamespace(url="https://music.apple.com/us/album/x/1"))

        page.wait_for_function.assert_not_called()
        self.assertIn("nothing to play", str(caught.exception))

    def test_an_artist_explains_what_to_choose_instead(self):
        page = playing_page(queued=0)
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.play(page, SimpleNamespace(url="https://music.apple.com/us/artist/coldplay/1"))
        self.assertIn("cannot play an artist directly", str(caught.exception))

    def test_a_timeout_names_the_likely_cause_not_a_playwright_error(self):
        """"Page.wait_for_function: Timeout 30000ms exceeded" is what reached the
        channel, and tells a listener nothing they can act on."""
        page = playing_page(becomes_playing=False, state={"queue": 1, "authorized": True})
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.play(page, SimpleNamespace(url="https://music.apple.com/us/album/x/1"))

        message = str(caught.exception)
        self.assertNotIn("wait_for_function", message)
        self.assertIn("subscription", message)

    def test_a_signed_out_session_says_to_reconnect(self):
        page = playing_page(becomes_playing=False, state={"queue": 1, "authorized": False})
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.play(page, SimpleNamespace(url="https://music.apple.com/us/album/x/1"))
        self.assertIn("li am", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
