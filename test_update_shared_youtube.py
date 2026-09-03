import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent


def read_deployment_script(name: str) -> str:
    """Read a host-side deployment script, skipping if it is not present.

    .dockerignore keeps update.sh, auto_updater.sh and streamerbot.sh out of the
    runtime image on purpose -- they manage containers from the host and have no
    job inside one. Running the suite in the container should report these as
    skipped rather than failing on a file that is absent by design.
    """
    path = ROOT / name
    if not path.is_file():
        raise unittest.SkipTest(
            f"{name} is not present; host-only deployment test skipped"
        )
    return path.read_text(encoding="utf-8")


class SharedYouTubeUpdateMigrationTests(unittest.TestCase):
    def test_updater_reexecutes_after_replacing_its_own_code(self):
        script = read_deployment_script("update.sh")

        self.assertIn("STREAMERBOT_UPDATE_REEXECED", script)
        self.assertRegex(script, r'exec bash "\$SCRIPT_DIR/update\.sh"')

    def test_updater_reconciles_shared_service_without_a_rebuild(self):
        script = read_deployment_script("update.sh")

        self.assertIn("reconcile_shared_youtube_service", script)
        main = script[script.index("main() {") :]
        self.assertRegex(
            main,
            re.compile(
                r"update_and_fix_permissions.*reconcile_shared_youtube_service",
                re.DOTALL,
            ),
        )

    def test_auto_updater_detects_an_unavailable_shared_service(self):
        script = read_deployment_script("auto_updater.sh")

        self.assertIn("YOUTUBE_BRIDGE_URL", script)
        self.assertRegex(
            script,
            re.compile(r"curl.+YOUTUBE_BRIDGE_URL.+SHOULD_UPDATE=true", re.DOTALL),
        )


if __name__ == "__main__":
    unittest.main()
