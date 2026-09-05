"""Encrypted credential storage, log redaction, tokens, and the sign-in job."""

import logging
import os
import stat
import tempfile
import threading
import time
import unittest
from unittest import TestCase

from bot.auth import redaction
from bot.auth.session import AuthJob, AuthJobManager, AuthState
from bot.auth.store import SecretStore, generate_key, is_valid_key, KEY_ENV_VAR
from bot.auth.tokens import TokenStore


class SecretStoreTests(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = SecretStore(self.dir)

    def test_round_trips_a_credential(self):
        self.store.set("nf", username="someone@example.com", password="hunter2")

        self.assertEqual(self.store.get("nf", "username"), "someone@example.com")
        self.assertEqual(self.store.get("nf", "password"), "hunter2")

    def test_the_password_is_not_on_disk_in_the_clear(self):
        self.store.set("nf", password="correct horse battery staple")

        with open(os.path.join(self.dir, "credentials.enc"), "rb") as f:
            raw = f.read()

        self.assertNotIn(b"correct horse battery staple", raw)

    def test_each_field_is_encrypted_separately(self):
        """A partial read of the file must not reveal a whole entry."""
        self.store.set("nf", username="a@example.com", password="secret-one")

        with open(os.path.join(self.dir, "credentials.enc"), encoding="utf-8") as f:
            import json
            entry = json.load(f)["entries"]["nf"]

        self.assertNotEqual(entry["username"], entry["password"])
        for value in entry.values():
            self.assertTrue(value.startswith("gAAAAA"), value[:12])

    def test_the_key_file_is_not_readable_by_others(self):
        mode = stat.S_IMODE(os.stat(os.path.join(self.dir, "portal.key")).st_mode)

        self.assertEqual(mode & 0o077, 0, oct(mode))

    def test_a_new_store_on_the_same_directory_can_still_read(self):
        self.store.set("sp", password="spotify-secret")

        self.assertEqual(SecretStore(self.dir).get("sp", "password"), "spotify-secret")

    def test_an_undecryptable_value_reads_as_absent_rather_than_raising(self):
        """Happens when the key is replaced. The user is asked to reconnect."""
        self.store.set("nf", password="hunter2")
        os.unlink(os.path.join(self.dir, "portal.key"))

        fresh = SecretStore(self.dir)

        self.assertIsNone(fresh.get("nf", "password"))

    def test_delete_removes_only_that_service(self):
        self.store.set("nf", password="a")
        self.store.set("sp", password="b")

        self.store.delete("nf")

        self.assertFalse(self.store.has("nf"))
        self.assertTrue(self.store.has("sp"))

    def test_setting_a_field_to_none_clears_it(self):
        self.store.set("nf", username="a@example.com", password="x")
        self.store.set("nf", password=None)

        self.assertIsNone(self.store.get("nf", "password"))
        self.assertEqual(self.store.get("nf", "username"), "a@example.com")

    def test_missing_service_and_field_are_none(self):
        self.assertIsNone(self.store.get("nope", "password"))
        self.assertFalse(self.store.has("nope"))

    def test_an_environment_key_is_used_and_not_written_to_disk(self):
        d = tempfile.mkdtemp()
        key = generate_key()
        os.environ[KEY_ENV_VAR] = key
        try:
            store = SecretStore(d)
            store.set("nf", password="env-secret")
            self.assertEqual(store.get("nf", "password"), "env-secret")
            self.assertFalse(os.path.exists(os.path.join(d, "portal.key")))
        finally:
            del os.environ[KEY_ENV_VAR]

    def test_key_validation(self):
        self.assertTrue(is_valid_key(generate_key()))
        self.assertFalse(is_valid_key("not-a-key"))
        self.assertFalse(is_valid_key(""))


class RedactionTests(TestCase):
    def setUp(self):
        self.filter = redaction.SecretRedactingFilter()

    def scrub(self, text):
        return self.filter.scrub(text)

    def test_a_registered_password_is_removed(self):
        self.filter.register("hunter2-long-enough")

        self.assertNotIn(
            "hunter2-long-enough",
            self.scrub("login failed for hunter2-long-enough at netflix"),
        )

    def test_registered_values_are_replaced_longest_first(self):
        """A short secret inside a longer one must not leave a fragment."""
        self.filter.register("abcd", "abcd1234efgh")

        self.assertEqual(self.scrub("token abcd1234efgh here"), "token [redacted] here")

    def test_very_short_values_are_never_registered(self):
        """Redacting "ab" would blank out unrelated text everywhere."""
        self.filter.register("ab")

        self.assertEqual(self.scrub("a table of absolutes"), "a table of absolutes")

    def test_password_assignments_are_caught_without_registration(self):
        self.assertNotIn("s3cr3t", self.scrub("connecting with password=s3cr3t"))
        self.assertNotIn("s3cr3t", self.scrub("Password: s3cr3t"))

    def test_tokens_and_bearer_headers_are_caught(self):
        self.assertNotIn("ya29.abc", self.scrub('{"refresh_token": "ya29.abc"}'))
        self.assertNotIn("eyJhbGci", self.scrub("Authorization: Bearer eyJhbGci.x.y"))

    def test_cookies_are_caught(self):
        self.assertNotIn("SID=xyz", self.scrub("Cookie: SID=xyz"))

    def test_the_filter_rewrites_a_real_log_record(self):
        self.filter.register("hunter2-long-enough")
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1,
            "sign-in failed for %s", ("hunter2-long-enough",), None,
        )

        self.filter.filter(record)

        self.assertNotIn("hunter2-long-enough", record.getMessage())

    def test_a_secret_in_a_traceback_is_removed(self):
        self.filter.register("hunter2-long-enough")
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), None)
        record.exc_text = 'Traceback ... ValueError: bad password hunter2-long-enough'

        self.filter.filter(record)

        self.assertNotIn("hunter2-long-enough", record.exc_text)

    def test_filter_always_lets_the_record_through(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", (), None)

        self.assertTrue(self.filter.filter(record))


class TokenStoreTests(TestCase):
    def test_a_minted_token_validates_and_an_unknown_one_does_not(self):
        store = TokenStore()
        token = store.mint("alice")

        self.assertTrue(store.check(token))
        self.assertFalse(store.check("wrong"))
        self.assertFalse(store.check(""))
        self.assertFalse(store.check(None))

    def test_tokens_expire(self):
        store = TokenStore(ttl=0)
        token = store.mint()

        time.sleep(0.01)

        self.assertFalse(store.check(token))

    def test_revoke_all_clears_every_token(self):
        store = TokenStore()
        a, b = store.mint(), store.mint()

        self.assertEqual(store.revoke_all(), 2)
        self.assertFalse(store.check(a))
        self.assertFalse(store.check(b))

    def test_tokens_are_long_enough_to_not_be_guessable(self):
        self.assertGreaterEqual(len(TokenStore().mint()), 32)


class AuthJobTests(TestCase):
    def test_a_job_starts_queued(self):
        self.assertIs(AuthJob("nf").state, AuthState.Queued)

    def test_the_worker_receives_a_code_submitted_by_the_portal(self):
        job = AuthJob("nf")
        received = []

        def worker():
            received.append(job.request_otp("Enter the code we texted you"))

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(100):
            if job.state is AuthState.AwaitingOtp:
                break
            time.sleep(0.01)

        self.assertTrue(job.submit_otp("123456"))
        thread.join(timeout=5)

        self.assertEqual(received, ["123456"])
        self.assertIs(job.state, AuthState.Filling)

    def test_a_code_is_refused_when_the_job_is_not_waiting_for_one(self):
        job = AuthJob("nf")

        self.assertFalse(job.submit_otp("123456"))

    def test_a_finished_job_cannot_be_moved_back(self):
        """A late worker callback must not overwrite the reason shown."""
        job = AuthJob("nf")
        job.fail("Wrong password.")

        job.set_state(AuthState.Filling)

        self.assertIs(job.state, AuthState.Failed)
        self.assertEqual(job.reason, "Wrong password.")

    def test_captcha_is_reported_as_something_we_cannot_answer(self):
        job = AuthJob("nf")
        job.require_captcha("https://netflix.test/challenge")

        self.assertIs(job.state, AuthState.AwaitingCaptcha)
        self.assertIn("CAPTCHA", job.reason)

    def test_success_and_failure_are_terminal(self):
        self.assertTrue(_finished(AuthJob("nf"), "succeed"))
        self.assertTrue(_finished(AuthJob("nf"), "fail", "nope"))


def _finished(job, method, *args):
    getattr(job, method)(*args)
    return job.is_finished


class AuthJobManagerTests(TestCase):
    def test_a_second_attempt_reuses_a_running_job(self):
        """Two clicks must not launch two competing browser contexts."""
        manager = AuthJobManager()
        first = manager.start("nf")

        self.assertIs(manager.start("nf"), first)

    def test_a_new_job_replaces_a_finished_one(self):
        manager = AuthJobManager()
        first = manager.start("nf")
        first.fail("Wrong password.")

        self.assertIsNot(manager.start("nf"), first)

    def test_jobs_are_kept_per_service(self):
        manager = AuthJobManager()

        self.assertIsNot(manager.start("nf"), manager.start("sp"))


if __name__ == "__main__":
    unittest.main()
