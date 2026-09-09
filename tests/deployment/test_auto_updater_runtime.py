from __future__ import annotations

import unittest

from tests.deployment.bash_sandbox import BashSandbox, ROOT, find_bash


@unittest.skipUnless(find_bash(), "bash is required")
class AutoUpdaterRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = BashSandbox()
        self._install_external_shims()

    def tearDown(self) -> None:
        self.sandbox.close()

    def _install_external_shims(self) -> None:
        self.sandbox.shim(
            "git",
            """
case "$*" in
  "config "*) exit 0 ;;
  "rev-parse --abbrev-ref HEAD") echo master ;;
  "rev-parse HEAD") echo "${TEST_LOCAL_HASH:-same}" ;;
  "ls-remote "*) echo "${TEST_REMOTE_HASH:-same} refs/heads/master" ;;
esac
""",
        )
        self.sandbox.shim(
            "docker",
            """
if [ "${1:-}" = image ] || [ "${1:-}" = run ]; then exit 0; fi
if [ "${1:-}" = inspect ]; then echo "${TEST_RUNNING_HASH:-same}"; exit 0; fi
exit 0
""",
        )
        self.sandbox.shim(
            "curl",
            '[ "${TEST_HEALTH:-fail}" = "ok" ]',
        )
        self.sandbox.shim("sudo", 'exec "$@"')
        self.sandbox.shim("sleep", ":")

    def _one_cycle_script(self) -> str:
        source = (ROOT / "auto_updater.sh").read_text(encoding="utf-8")
        source = source.replace(
            "RECOVERY_FAILURES=0\nNEXT_RECOVERY_AT=0",
            'RECOVERY_FAILURES=${TEST_RECOVERY_FAILURES:-0}\n'
            'NEXT_RECOVERY_AT=${TEST_NEXT_RECOVERY_AT:-0}',
            1,
        )
        source = source.replace("while true; do", "for _test_cycle in 1; do", 1)
        source += '\necho "state=$RECOVERY_FAILURES:$NEXT_RECOVERY_AT"\n'
        script = self.sandbox.root / "auto-updater-one-cycle.sh"
        script.write_text(source, encoding="utf-8")
        script.chmod(0o755)
        update = self.sandbox.root / "update.sh"
        update.write_text(
            "#!/bin/bash\n"
            'echo "update AUTO_UPDATE=${AUTO_UPDATE:-}" >> "$TEST_TRACE"\n'
            'exit "${TEST_UPDATE_EXIT:-0}"\n',
            encoding="utf-8",
        )
        update.chmod(0o755)
        return script.name

    def _backoff_harness(self) -> str:
        source = (ROOT / "auto_updater.sh").read_text(encoding="utf-8")
        functions = source[source.index("reset_recovery_backoff() {") :]
        functions = functions[: functions.index("while true; do")]
        script = self.sandbox.root / "backoff-harness.sh"
        script.write_text(
            "#!/bin/bash\n"
            "RECOVERY_BACKOFFS=(20 40 80 160 300)\n"
            "RECOVERY_FAILURES=0\nNEXT_RECOVERY_AT=0\n"
            + functions
            + "\n"
            + "schedule_recovery_retry; echo first=$RECOVERY_FAILURES:$NEXT_RECOVERY_AT\n"
            + "schedule_recovery_retry; echo second=$RECOVERY_FAILURES:$NEXT_RECOVERY_AT\n"
            + "schedule_recovery_retry; echo third=$RECOVERY_FAILURES:$NEXT_RECOVERY_AT\n"
            + "schedule_recovery_retry; schedule_recovery_retry; schedule_recovery_retry\n"
            + "echo capped=$RECOVERY_FAILURES:$NEXT_RECOVERY_AT\n"
            + "reset_recovery_backoff; echo reset=$RECOVERY_FAILURES:$NEXT_RECOVERY_AT\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script.name

    def test_failed_recovery_uses_exponential_backoff_and_reset_clears_it(self) -> None:
        self.sandbox.shim(
            "date", '[ "${1:-}" = +%s ] && echo 1000 || echo test-date'
        )

        result = self.sandbox.run([self._backoff_harness()])

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("first=1:1020", result.stdout)
        self.assertIn("second=2:1040", result.stdout)
        self.assertIn("third=3:1080", result.stdout)
        self.assertIn("capped=6:1300", result.stdout)
        self.assertIn("reset=0:0", result.stdout)

    def test_healthy_bridge_resets_existing_backoff(self) -> None:
        result = self.sandbox.run(
            [self._one_cycle_script()],
            env={
                "TEST_HEALTH": "ok",
                "TEST_RECOVERY_FAILURES": "4",
                "TEST_NEXT_RECOVERY_AT": "9999999999",
            },
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("state=0:0", result.stdout)
        self.assertFalse(self.sandbox.trace.exists())

    def test_git_update_runs_even_while_recovery_is_backed_off(self) -> None:
        result = self.sandbox.run(
            [self._one_cycle_script()],
            env={
                "TEST_HEALTH": "fail",
                "TEST_LOCAL_HASH": "old",
                "TEST_REMOTE_HASH": "new",
                "TEST_RUNNING_HASH": "old",
                "TEST_NEXT_RECOVERY_AT": "9999999999",
            },
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "update AUTO_UPDATE=true\n", self.sandbox.trace.read_text()
        )
        self.assertIn("New version detected", result.stdout)

    def test_busy_update_is_not_counted_as_recovery_failure(self) -> None:
        result = self.sandbox.run(
            [self._one_cycle_script()],
            env={"TEST_HEALTH": "fail", "TEST_UPDATE_EXIT": "75"},
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "update AUTO_UPDATE=true\n", self.sandbox.trace.read_text()
        )
        self.assertIn("owns the lock", result.stdout)
        self.assertNotIn("recovery failed", result.stdout.lower())
        self.assertIn("state=0:0", result.stdout)


if __name__ == "__main__":
    unittest.main()
