"""Phase 11 — searching while playing, and telling the truth about a service.

Three defects that all read to a user as "the bot is broken": a search stopped
the music, search results mode offered a choice of one, and `sv` described a
service that was playing as "not ready yet".
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from bot import app_vars
from bot.commands.user_commands import ServiceCommand
from bot.player.engines.browser_engine import AUX, PLAYER, BrowserEngine
from bot.services import Service
from bot.services.browser_service import AppleMusicService
from bot.services.netflix import NetflixService


def make_service(cls, engine=None):
    """A service without its constructor, which wants a whole Bot."""
    service = object.__new__(cls)
    service.config = SimpleNamespace(enabled=True, profile="")
    service.translator = SimpleNamespace(translate=lambda s: s)
    service.is_enabled = True
    service.error_message = ""
    service.warning_message = ""
    service._engine = engine
    return service


def make_command(portal=None, services=None, current=None):
    """A ServiceCommand without Command.__init__, which wants a whole bot."""
    command = object.__new__(ServiceCommand)
    command.translator = SimpleNamespace(translate=lambda s: s)
    command.command_processor = SimpleNamespace(auth_portal=portal)
    command.service_manager = SimpleNamespace(
        services=services or {}, service=current
    )
    return command


def track(name, kind=None):
    return SimpleNamespace(
        name=name, extra_info=({"kind": kind} if kind else {})
    )


class PageRoleTests(TestCase):
    """A tab producing audio is a playback device, not a browser tab.

    Apple Music's search and its is_logged_in both goto() the page they are
    given. Handed the page MusicKit is playing on, they end the music with no
    exception, no event and nothing in Player.state to say so.
    """

    def setUp(self):
        self.engine = object.__new__(BrowserEngine)
        self.engine._pages = {}
        self.pages = []

        def new_page():
            page = SimpleNamespace(closed=False)
            page.is_closed = lambda p=page: p.closed
            self.pages.append(page)
            return page

        # A persistent context opens with one blank tab already in it.
        self.context = SimpleNamespace(pages=[new_page()], new_page=new_page)
        self.engine._context_for = lambda service: self.context

    def test_searching_does_not_get_the_page_the_audio_is_on(self):
        player = self.engine._page_for("am", PLAYER)
        aux = self.engine._page_for("am", AUX)

        self.assertIsNot(player, aux)

    def test_each_role_keeps_its_own_page_across_calls(self):
        first = self.engine._page_for("am", PLAYER)
        self.engine._page_for("am", AUX)

        self.assertIs(self.engine._page_for("am", PLAYER), first)

    def test_the_contexts_existing_tab_is_adopted_rather_than_left_blank(self):
        self.engine._page_for("am", PLAYER)

        self.assertEqual(len(self.pages), 1)

    def test_playback_is_the_default_role(self):
        self.assertIs(
            self.engine._page_for("am"), self.engine._page_for("am", PLAYER)
        )

    def test_a_closed_page_is_replaced(self):
        first = self.engine._page_for("am", PLAYER)
        first.closed = True

        self.assertIsNot(self.engine._page_for("am", PLAYER), first)

    def test_services_do_not_share_a_page(self):
        self.engine._context_for = lambda service: SimpleNamespace(
            pages=[], new_page=lambda: SimpleNamespace(is_closed=lambda: False)
        )

        self.assertIsNot(
            self.engine._page_for("am", PLAYER), self.engine._page_for("az", PLAYER)
        )

    def test_signing_out_drops_every_role(self):
        """A page left behind belongs to a context that has just been closed."""
        self.engine._page_for("am", PLAYER)
        self.engine._page_for("am", AUX)
        self.engine._page_for("az", PLAYER)
        self.engine._contexts = {"am": SimpleNamespace(close=lambda: None)}
        self.engine.submit = lambda fn, timeout=0, name="": fn()
        self.engine._dir = "/nonexistent"

        self.engine.sign_out("am")

        self.assertEqual(list(self.engine._pages), [("az", PLAYER)])


class SearchListingTests(TestCase):
    """An album, an artist and the song on that album are three identical lines
    unless the list says which is which."""

    def setUp(self):
        self.service = make_service(AppleMusicService)

    def test_each_entry_names_its_kind(self):
        text = self.service.describe_tracks(
            [track("Abbey Road", "album"), track("A Song", "track")]
        )

        self.assertIn("1. Album: Abbey Road", text)
        self.assertIn("2. Track: A Song", text)

    def test_a_track_with_no_kind_is_not_dropped(self):
        text = self.service.describe_tracks([track("Something")])

        self.assertIn("Something", text)

    def test_counts_are_plural_when_there_is_more_than_one(self):
        text = self.service.describe_tracks(
            [track("a", "album"), track("b", "album")]
        )

        self.assertTrue(text.split("\n")[0].startswith("2 Albums"))

    def test_a_service_with_no_kinds_lists_plain_names(self):
        """YouTube returns videos and nothing else, so labelling every line
        identically would be noise rather than information."""
        text = Service.describe_tracks(
            make_service(AppleMusicService), [track("one"), track("two")]
        )

        self.assertEqual(text, "1. one\n2. two")


class NetflixListingTests(TestCase):
    """Netflix is its own class rather than a BrowserService, and its results
    carry the same `kind` the other four do. It must not be the one service
    whose list reads back as bare titles."""

    def test_netflix_entries_are_named_by_kind_like_the_others(self):
        service = make_service(NetflixService)

        text = service.describe_tracks(
            [track("The Diplomat", "series"), track("Glass Onion", "title")]
        )

        self.assertIn("1. Series: The Diplomat", text)
        self.assertIn("2. Title: Glass Onion", text)


class SearchResultsCountTests(TestCase):
    def test_the_list_default_is_not_one(self):
        """A numbered list with one entry in it is not a choice. This was the
        defect: `p QUERY` in search results mode asked the service for a single
        result and read it back as a list."""
        self.assertGreater(app_vars.search_results_mode_count, 1)

    def test_the_bare_play_limit_is_left_alone(self):
        """services.*.search_results is the `p` that plays the best match and
        genuinely wants one; the two limits are not the same setting."""
        from bot.config.models import YtModel

        self.assertEqual(YtModel().search_results, 1)


class ServiceReadinessTests(TestCase):
    """`sv am` called Apple Music "not ready yet" while Apple Music was playing.

    The warning was written during initialize(), when no engine was attached
    yet, and nothing ever cleared it.
    """

    def setUp(self):
        self.service = make_service(AppleMusicService)
        self.portal = Mock()

    def test_attaching_the_engine_clears_the_startup_warning(self):
        self.service.initialize()
        self.assertTrue(self.service.warning_message)

        self.service.attach_engine(object())

        self.assertFalse(self.service.warning_message)

    def test_a_connected_service_is_reported_ready(self):
        self.portal.statuses.return_value = {"am": "connected"}
        command = make_command(portal=self.portal)

        self.assertIn("ready", command.status_line(self.service))

    def test_a_service_with_no_account_names_the_command_that_connects_one(self):
        self.portal.statuses.return_value = {"am": "disconnected"}
        command = make_command(portal=self.portal)

        line = command.status_line(self.service)

        self.assertIn("not connected", line)
        self.assertTrue(line.rstrip().endswith("li am"))

    def test_a_disabled_service_reports_why_rather_than_its_sign_in_state(self):
        self.service.is_enabled = False
        self.service.error_message = "no Chrome on this architecture"
        command = make_command(portal=self.portal)

        self.assertEqual(
            command.status_line(self.service), "no Chrome on this architecture"
        )

    def test_youtube_music_is_asked_about_youtubes_account(self):
        """ytm has no account of its own: it plays through YouTube's session."""
        service = make_service(AppleMusicService)
        service.name = "ytm"
        self.portal.statuses.return_value = {"yt": "connected"}
        command = make_command(portal=self.portal)

        self.assertIn("ready", command.status_line(service))
        self.portal.statuses.assert_called()

    def test_no_portal_does_not_become_a_claim_that_nothing_is_connected(self):
        """The portal failing to bind is not the same as a signed-out account,
        and saying so sends people to reconnect something that is connected."""
        command = make_command(portal=None)

        self.assertIn("ready", command.status_line(self.service))


class ServiceListTests(TestCase):
    """The list of every service is a different job from the report on one.

    Seven full sentences in a row is a paragraph a listener sits through to
    reach the one they asked about.
    """

    def setUp(self):
        self.portal = Mock()
        self.portal.statuses.return_value = {"am": "connected", "az": "disconnected"}

    def test_a_ready_service_is_one_word(self):
        command = make_command(portal=self.portal)

        self.assertEqual(command.short_status(make_service(AppleMusicService)), "ready")

    def test_an_unconnected_service_names_its_command_without_a_sentence(self):
        service = make_service(AppleMusicService)
        service.name = "az"
        command = make_command(portal=self.portal)

        self.assertEqual(command.short_status(service), "not connected, send li az")

    def test_the_list_names_every_service_with_its_state(self):
        ready = make_service(AppleMusicService)
        missing = make_service(AppleMusicService)
        missing.name = "az"
        command = make_command(
            portal=self.portal,
            services={"am": ready, "az": missing},
            current=ready,
        )

        text = command.service_help

        self.assertIn("am (ready)", text)
        self.assertIn("az (not connected, send li az)", text)


class ServiceHelpTests(TestCase):
    def test_a_service_has_something_to_say_for_itself(self):
        """`sv` tells users to send `sv SERVICE h`, which answered "This service
        has no additional help" for every service the bot has."""
        self.assertTrue(make_service(AppleMusicService).help.strip())

    def test_the_help_ends_with_the_command_that_connects_it(self):
        """Command last, after a colon and with no full stop, so a screen
        reader's review cursor lands on it."""
        help_text = make_service(AppleMusicService).help

        self.assertTrue(help_text.rstrip().endswith("li am"))
