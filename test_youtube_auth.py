"""YouTube sign-in as the commands see it, after Phase 9 retired the device code.

YouTube stopped serving playback to the TV device-code sign-in, so connecting
happens through the portal: in the bot's own Chrome, or by importing a session.
yl keeps the two things an administrator wants from the channel, the state and
signing out; li yt hands out the portal link.
"""

import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot import errors
from bot.commands.admin_commands import YouTubeLoginCommand
from bot.commands.user_commands import LoginCommand
from bot.services.youtube_bridge import YouTubeBridge


class BridgeStatusTests(TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"TTBOT_INSTANCE": "bot-one"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge = YouTubeBridge()

    def test_the_device_code_calls_are_gone(self):
        """A sign-in that succeeds and then plays nothing is worse than none."""
        self.assertFalse(hasattr(self.bridge, "auth_start"))
        self.assertFalse(hasattr(self.bridge, "auth_signout"))

    def test_status_carries_this_bots_id(self):
        """bot_id is what keeps one bot out of another bot's session."""
        with patch.object(self.bridge, "_post", return_value={}) as post:
            self.bridge.auth_status()

        self.assertEqual(post.call_args.args[0], "/auth/status")
        self.assertEqual(self.bridge.bot_id, "bot-one")

    def test_is_signed_in_survives_an_unreachable_bridge(self):
        """Startup must not fail just because sign-in state is unknown."""
        with patch.object(self.bridge, "_post", side_effect=errors.ServiceError("down")):
            self.assertFalse(self.bridge.is_signed_in())


def make_yl(keeper):
    command = object.__new__(YouTubeLoginCommand)
    command.command_processor = SimpleNamespace(youtube_session=keeper)
    command.translator = SimpleNamespace(translate=lambda s: s)
    return command


class YouTubeLoginCommandTests(TestCase):
    def test_status_reports_signed_in(self):
        keeper = Mock()
        keeper.status.return_value = "connected"

        self.assertIn("is signed in", make_yl(keeper)("", user=None))

    def test_a_session_google_ended_says_how_to_fix_it(self):
        """Not "service unavailable", which tells nobody anything they can act on."""
        keeper = Mock()
        keeper.status.return_value = "expired"

        result = make_yl(keeper)("", user=None)

        self.assertIn("signed this bot out", result)
        self.assertTrue(result.endswith("li yt"), "the command goes last")

    def test_start_points_at_the_portal_instead_of_a_device_code(self):
        keeper = Mock()

        result = make_yl(keeper)("start", user=None)

        self.assertIn("li yt", result)
        self.assertNotIn("google.com/device", result)

    def test_out_signs_the_bot_out(self):
        keeper = Mock()

        result = make_yl(keeper)("out", user=None)

        keeper.sign_out.assert_called_once_with()
        self.assertIn("signed out", result)

    def test_an_unknown_argument_is_rejected(self):
        with self.assertRaises(errors.InvalidArgumentError):
            make_yl(Mock())("wipe", user=None)

    def test_a_disabled_youtube_service_is_reported_not_crashed(self):
        with self.assertRaises(errors.ServiceError):
            make_yl(None)("", user=None)


def make_li(keeper, statuses=None):
    portal = Mock()
    portal.statuses.return_value = statuses or {"yt": "disconnected"}
    portal.mint_link.side_effect = lambda username, path="/": f"https://bot.example{path}?t=tok"
    portal.link_advice.return_value = None
    command = object.__new__(LoginCommand)
    command.command_processor = SimpleNamespace(auth_portal=portal, youtube_session=keeper)
    command.translator = SimpleNamespace(translate=lambda s: s)
    return command, portal


class LiYouTubeTests(TestCase):
    user = SimpleNamespace(username="alice")

    def test_li_yt_sends_the_advice_then_the_link_in_a_message_of_its_own(self):
        keeper = Mock(browser_available=True)
        command, _portal = make_li(keeper)

        result = command("yt", self.user)

        self.assertEqual(len(result), 2)
        self.assertIn("separate Google account", result[0])
        self.assertNotIn("https://", result[0], "the link is not repeated in the advice")
        self.assertEqual(result[1], "https://bot.example/connect/yt?t=tok",
                         "the link message holds the link and nothing else")

    def test_without_a_browser_li_yt_offers_only_the_import_page(self):
        keeper = Mock(browser_available=False)
        command, _portal = make_li(keeper)

        result = command("yt", self.user)

        self.assertEqual(result[-1], "https://bot.example/import/yt?t=tok")
        self.assertNotIn("/connect/yt", "".join(result))
        self.assertEqual("".join(result).count("https://"), 1, "one link, not two")

    def test_li_reports_a_session_google_ended(self):
        command, _portal = make_li(Mock(), statuses={"yt": "expired"})

        result = command("", self.user)

        self.assertIn("YouTube: signed out, connect again", result[0])
        self.assertEqual(result[1:], ["https://bot.example/connect/yt?t=tok"])


class LiLinkMessageTests(TestCase):
    """Every link and code goes out alone, so copying the message copies only it."""

    user = SimpleNamespace(username="alice")

    def test_li_for_a_portal_service_sends_instructions_then_the_bare_link(self):
        command, _portal = make_li(Mock(), statuses={"nf": "disconnected"})

        result = command("nf", self.user)

        self.assertEqual(len(result), 2)
        self.assertIn("Netflix", result[0])
        self.assertNotIn("https://", result[0])
        self.assertEqual(result[1], "https://bot.example/connect/nf?t=tok")

    def test_link_advice_stays_with_the_instructions(self):
        command, portal = make_li(Mock(), statuses={"nf": "disconnected"})
        portal.link_advice.return_value = "Set a public address."

        result = command("nf", self.user)

        self.assertIn("Set a public address.", result[0])
        self.assertEqual(result[1], "https://bot.example/connect/nf?t=tok")

    def test_li_sp_sends_the_url_and_the_code_each_alone(self):
        command, portal = make_li(Mock(), statuses={"sp": "disconnected"})
        portal.spotify_start.return_value = {
            "user_code": "BCDF-GHJK",
            "verification_url": "https://www.google.com/device",
        }

        result = command("sp", self.user)

        self.assertEqual(result[1:], ["https://www.google.com/device", "BCDF-GHJK"])
        self.assertIn("B C D F G H J K", result[0])
        self.assertNotIn("https://", result[0])

    def test_an_already_connected_service_gets_the_portal_link_alone(self):
        command, _portal = make_li(Mock(), statuses={"nf": "connected"})

        result = command("nf", self.user)

        self.assertIn("already connected", result[0])
        self.assertEqual(result[1], "https://bot.example/?t=tok")

    def test_the_overview_sends_one_bare_link_per_unconnected_service_in_the_order_named(self):
        keeper = Mock(browser_available=True)
        command, _portal = make_li(
            keeper,
            statuses={"yt": "connected", "nf": "disconnected", "am": "expired"},
        )

        result = command("", self.user)

        self.assertEqual(result[1:], [
            "https://bot.example/connect/yt?t=tok",
            "https://bot.example/connect/nf?t=tok",
            "https://bot.example/connect/am?t=tok",
        ])
        self.assertNotIn("https://", result[0])
        self.assertIn("this order: reconnect YouTube, connect Netflix, connect Apple Music", result[0])

    def test_youtube_gets_a_link_even_when_connected_and_so_does_the_account_page(self):
        command, _portal = make_li(Mock(browser_available=True), statuses={"yt": "connected"})

        result = command("", self.user)

        self.assertEqual(result[1:], ["https://bot.example/connect/yt?t=tok", "https://bot.example/?t=tok"])
        self.assertIn("reconnect YouTube, your account page", result[0])

    def test_li_yt_when_connected_offers_signing_in_again(self):
        command, _portal = make_li(Mock(browser_available=True), statuses={"yt": "connected"})

        result = command("yt", self.user)

        self.assertEqual(result[-1], "https://bot.example/connect/yt?t=tok")


class SeveralMessageReplyTests(TestCase):
    def test_a_list_reply_is_sent_as_separate_messages_in_order(self):
        from bot.commands import CommandProcessor

        processor = object.__new__(CommandProcessor)
        processor.pending_ad_prompt = {}
        processor.pending_playlist_choice = {}
        processor.pending_playlist_download = {}
        processor.pending_ads_option = {}
        processor.pending_podcast_count = {}
        processor.ttclient = Mock()
        processor.parse_command = Mock(return_value=("li", "nf"))
        processor.check_access = Mock(return_value=True)
        processor.get_command = Mock(
            return_value=lambda _processor: (lambda arg, user: ["Open the link.", "https://x", ""])
        )
        user = SimpleNamespace(id=7, username="alice")

        processor._run(SimpleNamespace(user=user, text="li nf"))

        sent = [call.args[0] for call in processor.ttclient.send_message.call_args_list]
        self.assertEqual(sent, ["Open the link.", "https://x"], "empty items are not sent")
