"""Working out a portal address a user can actually open.

The bug this replaces: the portal handed out http://127.0.0.1:4419, which on the
remote Linux box these bots run on is a link to the user's own computer, where
nothing is listening. Every account connection failed with a browser error and no
explanation.
"""

import os
import unittest
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from bot.modules import public_address


def make_config(host="127.0.0.1", port=4419, public_url=""):
    return SimpleNamespace(host=host, port=port, public_url=public_url)


class NormaliseTests(TestCase):
    """People write an address four different ways. Being strict here only
    produces a broken link with a confusing error."""

    def test_a_bare_host_gains_a_scheme_and_the_port(self):
        self.assertEqual(
            public_address.normalise_base("example.org", 4419),
            "http://example.org:4419",
        )

    def test_a_bare_ip_gains_a_scheme_and_the_port(self):
        self.assertEqual(
            public_address.normalise_base("93.184.216.34", 4419),
            "http://93.184.216.34:4419",
        )

    def test_an_explicit_port_is_not_doubled(self):
        self.assertEqual(
            public_address.normalise_base("example.org:8080", 4419),
            "http://example.org:8080",
        )

    def test_a_scheme_is_kept(self):
        self.assertEqual(
            public_address.normalise_base("http://example.org", 4419),
            "http://example.org:4419",
        )

    def test_an_https_url_is_left_alone(self):
        """That is a reverse proxy on 443, and adding :4419 would break it."""
        self.assertEqual(
            public_address.normalise_base("https://bot.example.org", 4419),
            "https://bot.example.org",
        )

    def test_a_path_is_preserved(self):
        self.assertEqual(
            public_address.normalise_base("https://example.org/streamerbot", 4419),
            "https://example.org:4419/streamerbot",
        )

    def test_a_trailing_slash_is_dropped(self):
        self.assertEqual(
            public_address.normalise_base("http://example.org:9/", 4419),
            "http://example.org:9",
        )

    def test_nothing_in_gives_nothing_out(self):
        self.assertEqual(public_address.normalise_base("", 4419), "")


class PrivateAddressTests(TestCase):
    def test_lan_and_loopback_ranges_are_private(self):
        for address in ("10.0.0.5", "192.168.1.10", "172.16.0.1", "127.0.0.1",
                        "169.254.1.1"):
            self.assertTrue(public_address.is_private(address), address)

    def test_a_routable_address_is_not(self):
        for address in ("93.184.216.34", "8.8.8.8", "1.1.1.1"):
            self.assertFalse(public_address.is_private(address), address)

    def test_the_documentation_ranges_count_as_private(self):
        """Worth pinning because it surprises people, this author included.
        Python 3.13's is_private covers the IETF reserved blocks, so TEST-NET
        addresses like 203.0.113.7 are private. That is the behaviour we want —
        handing a user a link to a reserved address is no better than loopback —
        but it means those addresses cannot be used as stand-ins for public ones
        in a test."""
        for address in ("203.0.113.7", "198.51.100.5"):
            self.assertTrue(public_address.is_private(address), address)

    def test_a_hostname_is_assumed_routable(self):
        """The user chose it, so it is not ours to second-guess."""
        self.assertFalse(public_address.is_private("bot.example.org"))


class ResolutionOrderTests(TestCase):
    def setUp(self):
        # The environment must not leak between tests.
        self._saved = os.environ.pop(public_address.ENV_VAR, None)

    def tearDown(self):
        os.environ.pop(public_address.ENV_VAR, None)
        if self._saved is not None:
            os.environ[public_address.ENV_VAR] = self._saved

    def test_the_configured_url_wins_over_everything(self):
        """It is the only source that can be right behind a reverse proxy."""
        config = make_config(public_url="https://bot.example.org")
        os.environ[public_address.ENV_VAR] = "http://ignored.example"

        with patch.object(public_address, "detect_outbound_address",
                          return_value="93.184.216.34"):
            base, how = public_address.resolve(config)

        self.assertEqual(base, "https://bot.example.org")
        self.assertIn("public_url", how)

    def test_the_environment_beats_detection(self):
        os.environ[public_address.ENV_VAR] = "bot.example.org"

        with patch.object(public_address, "detect_outbound_address",
                          return_value="93.184.216.34"):
            base, how = public_address.resolve(make_config())

        self.assertEqual(base, "http://bot.example.org:4419")
        self.assertIn(public_address.ENV_VAR, how)

    def test_a_public_address_is_detected_and_used(self):
        with patch.object(public_address, "detect_outbound_address",
                          return_value="93.184.216.34"):
            base, how = public_address.resolve(make_config())

        self.assertEqual(base, "http://93.184.216.34:4419")
        self.assertIn("public address", how)

    def test_a_lan_address_is_used_but_the_reason_says_so(self):
        """Right for someone on the same network, wrong for anyone else. Still
        better than loopback, which is wrong for everybody but the machine."""
        with patch.object(public_address, "detect_outbound_address",
                          return_value="192.168.1.50"):
            base, how = public_address.resolve(make_config())

        self.assertEqual(base, "http://192.168.1.50:4419")
        self.assertIn("NAT", how)

    def test_detection_failing_falls_back_to_the_bind_address(self):
        with patch.object(public_address, "detect_outbound_address", return_value=None):
            base, how = public_address.resolve(make_config(host="127.0.0.1"))

        self.assertEqual(base, "http://127.0.0.1:4419")
        self.assertIn("bind address", how)

    def test_the_port_follows_the_configuration(self):
        with patch.object(public_address, "detect_outbound_address",
                          return_value="93.184.216.34"):
            base, _ = public_address.resolve(make_config(port=9999))

        self.assertEqual(base, "http://93.184.216.34:9999")

    def test_nothing_ever_resolves_to_a_bare_loopback_link_when_detection_works(self):
        """The whole point: a remote user must not be handed 127.0.0.1."""
        with patch.object(public_address, "detect_outbound_address",
                          return_value="93.184.216.34"):
            base, _ = public_address.resolve(make_config(host="127.0.0.1"))

        self.assertNotIn("127.0.0.1", base)


class ReachabilityAdviceTests(TestCase):
    """Detecting the address is half the job. A correct address is still
    unreachable if nothing is listening on a public interface."""

    def test_a_loopback_bind_with_a_public_link_is_called_out(self):
        config = make_config(host="127.0.0.1", port=4419)

        advice = public_address.reachability_warning(config, "http://93.184.216.34:4419")

        self.assertIsNotNone(advice)
        self.assertIn("0.0.0.0", advice)
        self.assertIn("4419", advice)
        self.assertIn("firewall", advice)

    def test_binding_to_all_interfaces_needs_no_advice(self):
        config = make_config(host="0.0.0.0")

        self.assertIsNone(
            public_address.reachability_warning(config, "http://93.184.216.34:4419")
        )

    def test_a_loopback_link_says_to_set_public_url(self):
        config = make_config(host="0.0.0.0")

        advice = public_address.reachability_warning(config, "http://127.0.0.1:4419")

        self.assertIsNotNone(advice)
        self.assertIn("public_url", advice)

    def test_the_advice_names_the_configured_port_not_a_hardcoded_one(self):
        config = make_config(host="127.0.0.1", port=9999)

        advice = public_address.reachability_warning(config, "http://93.184.216.34:9999")

        self.assertIn("9999", advice)


class DetectionTests(TestCase):
    def test_detection_never_raises_when_there_is_no_network(self):
        """Startup must not fail because a socket could not be opened."""
        with patch("socket.socket") as sock:
            sock.return_value.connect.side_effect = OSError("network unreachable")
            self.assertIsNone(public_address.detect_outbound_address())

    def test_the_socket_is_always_closed(self):
        with patch("socket.socket") as sock:
            sock.return_value.connect.side_effect = OSError("boom")
            public_address.detect_outbound_address()
            sock.return_value.close.assert_called_once_with()

    def test_it_uses_udp_so_nothing_is_actually_sent(self):
        """A datagram connect only sets the default peer; 8.8.8.8 is never
        contacted, and no third party learns where this bot lives."""
        import socket as socket_module

        with patch("socket.socket") as sock:
            sock.return_value.getsockname.return_value = ("93.184.216.34", 0)
            public_address.detect_outbound_address()

        sock.assert_called_once_with(socket_module.AF_INET, socket_module.SOCK_DGRAM)


if __name__ == "__main__":
    unittest.main()
