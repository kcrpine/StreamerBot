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

    def test_li_yt_sends_the_sign_in_link_with_the_separate_account_advice_first(self):
        keeper = Mock(browser_available=True)
        command, _portal = make_li(keeper)

        result = command("yt", self.user)

        self.assertIn("separate Google account", result)
        self.assertTrue(result.endswith("https://bot.example/connect/yt?t=tok"),
                        "the link goes last, with nothing after it")
        self.assertLess(result.index("separate"), result.index("https://"))

    def test_without_a_browser_li_yt_offers_only_the_import_page(self):
        keeper = Mock(browser_available=False)
        command, _portal = make_li(keeper)

        result = command("yt", self.user)

        self.assertIn("/import/yt", result)
        self.assertNotIn("/connect/yt", result)
        self.assertEqual(result.count("https://"), 1, "one link, not two")

    def test_li_reports_a_session_google_ended(self):
        command, _portal = make_li(Mock(), statuses={"yt": "expired"})
        command.command_processor.auth_portal.mint_link.side_effect = None
        command.command_processor.auth_portal.mint_link.return_value = "https://bot.example/?t=tok"

        result = command("", self.user)

        self.assertIn("YouTube: signed out, connect again", result)
