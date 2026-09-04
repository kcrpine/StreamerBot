"""YouTube OAuth device-code sign-in, replacing the cookies.txt file."""

import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot import errors
from bot.commands.admin_commands import YouTubeLoginCommand
from bot.services.youtube_bridge import YouTubeBridge


class BridgeAuthEndpointTests(TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"TTBOT_INSTANCE": "bot-one"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge = YouTubeBridge()

    def test_every_auth_call_carries_this_bots_id(self):
        """bot_id is what keeps one bot out of another bot's tokens."""
        with patch.object(self.bridge, "_post", return_value={}) as post:
            self.bridge.auth_start()
            self.bridge.auth_status()
            self.bridge.auth_signout()

        endpoints = [call.args[0] for call in post.call_args_list]
        self.assertEqual(endpoints, ["/auth/start", "/auth/status", "/auth/signout"])
        self.assertEqual(self.bridge.bot_id, "bot-one")

    def test_is_signed_in_reads_the_flag(self):
        with patch.object(self.bridge, "_post", return_value={"signed_in": True}):
            self.assertTrue(self.bridge.is_signed_in())
        with patch.object(self.bridge, "_post", return_value={"signed_in": False}):
            self.assertFalse(self.bridge.is_signed_in())

    def test_is_signed_in_survives_an_unreachable_bridge(self):
        """Startup must not fail just because sign-in state is unknown."""
        with patch.object(self.bridge, "_post", side_effect=errors.ServiceError("down")):
            self.assertFalse(self.bridge.is_signed_in())


def make_command(bridge):
    command = object.__new__(YouTubeLoginCommand)
    command.service_manager = SimpleNamespace(
        services={"yt": SimpleNamespace(_bridge=bridge)}
    )
    command.translator = SimpleNamespace(translate=lambda s: s)
    return command


class YouTubeLoginCommandTests(TestCase):
    def test_status_reports_signed_in(self):
        bridge = Mock()
        bridge.is_signed_in.return_value = True

        result = make_command(bridge)("", user=None)

        self.assertIn("signed in", result)

    def test_status_reports_signed_out_without_alarming_the_user(self):
        """Not being signed in is normal: public videos still play."""
        bridge = Mock()
        bridge.is_signed_in.return_value = False

        result = make_command(bridge)("", user=None)

        self.assertIn("not signed in", result)
        self.assertIn("Public videos still play", result)

    def test_start_returns_the_code_spelled_out(self):
        """Codes mix letters and digits; a screen reader must not read a word."""
        bridge = Mock()
        bridge.is_signed_in.return_value = False
        bridge.auth_start.return_value = {
            "verification_url": "https://www.google.com/device",
            "user_code": "XGHQ-VDGJ",
            "expires_in": 1800
        }

        result = make_command(bridge)("start", user=None)

        self.assertIn("https://www.google.com/device", result)
        self.assertIn("XGHQ-VDGJ", result)
        self.assertIn("X G H Q V D G J", result)
        self.assertIn("30 minutes", result)

    def test_start_refuses_to_silently_replace_a_working_account(self):
        bridge = Mock()
        bridge.is_signed_in.return_value = True

        result = make_command(bridge)("start", user=None)

        self.assertIn("already signed in", result)
        bridge.auth_start.assert_not_called()

    def test_out_signs_the_bot_out(self):
        bridge = Mock()

        result = make_command(bridge)("out", user=None)

        bridge.auth_signout.assert_called_once_with()
        self.assertIn("signed out", result)

    def test_an_unknown_argument_is_rejected(self):
        bridge = Mock()
        bridge.is_signed_in.return_value = False

        with self.assertRaises(errors.InvalidArgumentError):
            make_command(bridge)("wipe", user=None)

    def test_a_disabled_youtube_service_is_reported_not_crashed(self):
        command = object.__new__(YouTubeLoginCommand)
        command.service_manager = SimpleNamespace(services={})
        command.translator = SimpleNamespace(translate=lambda s: s)

        with self.assertRaises(errors.ServiceError):
            command("", user=None)
