from __future__ import annotations

import unittest

from tests.deployment.bash_sandbox import BashSandbox, ROOT, find_bash


@unittest.skipUnless(find_bash(), "bash is required")
class UpdateRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = BashSandbox()

    def tearDown(self) -> None:
        self.sandbox.close()

    def _main_harness(self, reconcile_status: int) -> str:
        source = (ROOT / "update.sh").read_text(encoding="utf-8")
        functions = source[source.index("# Function: Display Header") :]
        functions = functions[: functions.index("# Execute main in memory")]
        harness = self.sandbox.root / "update-main-harness.sh"
        harness.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            f'SCRIPT_DIR="{self.sandbox.root}"\n'
            f'LOCK_FILE="{self.sandbox.root / "update.lock"}"\n'
            "AUTO_UPDATE=true\n"
            "RED= GREEN= YELLOW= NC=\n"
            + functions
            + "\nacquire_update_lock() { :; }\n"
            + "\ninstall_deps_light() { :; }\n"
            + "update_and_fix_permissions() { echo update >> \"$TEST_TRACE\"; }\n"
            + f"reconcile_shared_youtube_service() {{ echo reconcile >> \"$TEST_TRACE\"; return {reconcile_status}; }}\n"
            + "configure_auto_updater() { echo configure >> \"$TEST_TRACE\"; }\n"
            + "main\n",
            encoding="utf-8",
        )
        harness.chmod(0o755)
        return harness.name

    def _lock_harness(self) -> str:
        source = (ROOT / "update.sh").read_text(encoding="utf-8")
        lock_function = source[source.index("acquire_update_lock() {") :]
        lock_function = lock_function[: lock_function.index("# Colors")]
        harness = self.sandbox.root / "update-lock-harness.sh"
        harness.write_text(
            "#!/bin/bash\n"
            f'UPDATE_LOCK_FILE="{self.sandbox.root / "update.lock"}"\n'
            + lock_function
            + "\nacquire_update_lock || exit $?\n"
            + 'if [ "${HOLD_LOCK_AFTER_ACQUIRE:-false}" = "true" ]; then\n'
            + '    : > "$PWD/lock-ready"\n'
            + '    sleep 2\n'
            + "fi\n",
            encoding="utf-8",
        )
        harness.chmod(0o755)
        return harness.name

    def test_reconcile_failure_is_the_process_status(self) -> None:
        result = self.sandbox.run([self._main_harness(reconcile_status=23)])

        self.assertEqual(23, result.returncode, result.stdout + result.stderr)
        self.assertEqual("update\nreconcile\n", self.sandbox.trace.read_text())

    def test_successful_reconcile_continues_to_service_configuration(self) -> None:
        result = self.sandbox.run([self._main_harness(reconcile_status=0)])

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "update\nreconcile\nconfigure\n", self.sandbox.trace.read_text()
        )

    def test_automatic_update_returns_temporary_failure_when_lock_is_busy(self) -> None:
        lock_harness = self._lock_harness()
        command = (
            'exec 8>"$PWD/update.lock"; flock 8; '
            f'AUTO_UPDATE=true "./{lock_harness}"'
        )

        result = self.sandbox.run(["-c", command])

        self.assertEqual(75, result.returncode, result.stdout + result.stderr)
        self.assertIn("already in progress", result.stdout)
        self.assertTrue((self.sandbox.root / "update.lock").exists())

    def test_lock_file_is_not_deleted_by_the_lock_owner(self) -> None:
        result = self.sandbox.run(
            [self._lock_harness()], env={"AUTO_UPDATE": "true"}
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((self.sandbox.root / "update.lock").exists())

    def test_forged_lock_marker_without_fd_does_not_bypass_busy_lock(self) -> None:
        lock_harness = self._lock_harness()
        command = (
            'exec 8>"$PWD/update.lock"; flock 8; '
            f'STREAMERBOT_UPDATE_LOCK_HELD=true AUTO_UPDATE=true "./{lock_harness}"'
        )

        result = self.sandbox.run(["-c", command])

        self.assertEqual(75, result.returncode, result.stdout + result.stderr)
        self.assertIn("already in progress", result.stdout)

    def test_lock_marker_with_wrong_fd_does_not_bypass_busy_lock(self) -> None:
        lock_harness = self._lock_harness()
        command = (
            'exec 8>"$PWD/update.lock"; flock 8; exec 9>"$PWD/wrong.lock"; '
            f'STREAMERBOT_UPDATE_LOCK_HELD=true AUTO_UPDATE=true "./{lock_harness}"'
        )

        result = self.sandbox.run(["-c", command])

        self.assertEqual(75, result.returncode, result.stdout + result.stderr)
        self.assertIn("already in progress", result.stdout)

    def test_inherited_fd_reuses_lock_and_excludes_third_process(self) -> None:
        lock_harness = self._lock_harness()
        command = (
            'exec 9>"$PWD/update.lock"; flock 9; '
            f'STREAMERBOT_UPDATE_LOCK_HELD=true AUTO_UPDATE=true '
            f'HOLD_LOCK_AFTER_ACQUIRE=true "./{lock_harness}" & child=$!; '
            'for _ in 1 2 3 4 5 6 7 8 9 10; do '
            '[ -e "$PWD/lock-ready" ] && break; sleep 0.05; done; '
            '[ -e "$PWD/lock-ready" ] || exit 90; '
            '(exec 8>"$PWD/update.lock"; ! flock -n 8) || exit 91; '
            'wait "$child"'
        )

        result = self.sandbox.run(["-c", command])

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
