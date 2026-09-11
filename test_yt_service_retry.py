from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from bot import errors
from bot.services.yt import YtService


class YtServiceGetRetryTests(TestCase):
    def setUp(self):
        self.service = object.__new__(YtService)
        self.service.name = "yt"
        self.service._max_retries = 2
        self.service._TRANSIENT_RETRY_BACKOFF_SECONDS = 1.0

    def test_transient_service_error_is_retried_before_raising(self):
        # A bare bridge/CDN hiccup used to raise immediately with zero
        # retries (only messages that looked auth-related were retried).
        # It should now get the same bounded retry.
        self.service._get_inner = Mock(
            side_effect=[
                errors.ServiceError("YouTube.js returned no stream URL"),
                ["track"],
            ]
        )

        with patch("bot.services.yt.time.sleep") as mock_sleep:
            result = self.service.get("https://youtube.com/watch?v=x")

        self.assertEqual(result, ["track"])
        self.assertEqual(self.service._get_inner.call_count, 2)
        mock_sleep.assert_called_once_with(1.0)

    def test_transient_service_error_raises_after_max_retries(self):
        self.service._get_inner = Mock(
            side_effect=errors.ServiceError("YouTube.js bridge unavailable")
        )

        with patch("bot.services.yt.time.sleep"):
            with self.assertRaises(errors.ServiceError):
                self.service.get("https://youtube.com/watch?v=x")

        self.assertEqual(self.service._get_inner.call_count, self.service._max_retries + 1)

    def test_auth_error_still_uses_exponential_backoff(self):
        self.service._get_inner = Mock(
            side_effect=[
                errors.ServiceError("Sign in to confirm you're not a bot"),
                ["track"],
            ]
        )

        with patch("bot.services.yt.time.sleep") as mock_sleep:
            result = self.service.get("https://youtube.com/watch?v=x")

        self.assertEqual(result, ["track"])
        mock_sleep.assert_called_once_with(2)  # 2 ** attempt(1), unchanged from before
