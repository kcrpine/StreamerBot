from __future__ import annotations

import unittest

from tests.deployment.bash_sandbox import (
    BashSandbox,
    ROOT,
    deployment_scripts_present,
    find_bash,
)


@unittest.skipUnless(find_bash(), "bash is required")
@unittest.skipUnless(
    deployment_scripts_present("update.sh"),
    "update.sh is not present; host-only test skipped",
)
class UpdateNotifiesAfterBuildTests(unittest.TestCase):
    """A bot announces "I will restart in a few moments" when it finds
    update_in_progress. The marker used to be written before the build, so a
    build that failed sent that message into every channel on each five-minute
    retry, and no restart ever followed."""

    def setUp(self) -> None:
        self.sandbox = BashSandbox()
        self.bots = self.sandbox.root / "bots"
        (self.bots / "alpha").mkdir(parents=True)
        self.sandbox.shim(
            "docker",
            """
case "${1:-}" in
  ps) echo alpha ;;
  build) exit "${TEST_BUILD_EXIT:-0}" ;;
esac
exit 0
""",
        )
        self.sandbox.shim("git", "echo abc123")
        self.sandbox.shim("sleep", ":")
        self.sandbox.shim("journalctl", ":")

    def tearDown(self) -> None:
        self.sandbox.close()

    def _harness(self) -> str:
        source = (ROOT / "update.sh").read_text(encoding="utf-8")
        function = source[source.index("perform_image_rebuild() {") :]
        function = function[: function.index("\n}\n") + 3]
        script = self.sandbox.root / "rebuild-harness.sh"
        script.write_text(
            "#!/bin/bash\n"
            f'BOTS_ROOT="{self.bots}"\n'
            "BOT_IMAGE=streamerbot\nIMAGE_BUILD_ARGS=()\n"
            "create_shared_youtube_service() { :; }\n"
            "start_shared_youtube_service() { :; }\n"
            "recreate_bot_containers() { :; }\n"
            + function
            + "perform_image_rebuild\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script.name

    def test_a_failed_build_tells_no_bot_it_is_restarting(self) -> None:
        result = self.sandbox.run([self._harness()], env={"TEST_BUILD_EXIT": "1"})

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("Error building image", result.stdout)
        self.assertFalse((self.bots / "alpha" / "update_in_progress").exists())

    def test_a_successful_build_tells_running_bots(self) -> None:
        result = self.sandbox.run([self._harness()])

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((self.bots / "alpha" / "update_in_progress").exists())


if __name__ == "__main__":
    unittest.main()
