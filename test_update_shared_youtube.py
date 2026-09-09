import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class SharedYouTubeUpdateMigrationTests(unittest.TestCase):
    def test_updater_reexecutes_after_replacing_its_own_code(self):
        script = (ROOT / "update.sh").read_text(encoding="utf-8")

        self.assertIn("TTMEDIABOT_UPDATE_REEXECED", script)
        self.assertRegex(script, r'exec bash "\$SCRIPT_DIR/update\.sh"')

    def test_updater_reconciles_shared_service_without_a_rebuild(self):
        script = (ROOT / "update.sh").read_text(encoding="utf-8")

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
        script = (ROOT / "auto_updater.sh").read_text(encoding="utf-8")

        self.assertIn("YOUTUBE_BRIDGE_URL", script)
        self.assertRegex(
            script,
            re.compile(r"curl.+YOUTUBE_BRIDGE_URL.+SHOULD_UPDATE=true", re.DOTALL),
        )


if __name__ == "__main__":
    unittest.main()
