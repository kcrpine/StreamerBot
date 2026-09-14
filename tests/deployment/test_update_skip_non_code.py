"""The auto-updater does not rebuild for a push that changes no bot code.

An update rebuilds the image, restarts every bot and announces itself in every
channel. Doing that because the plan or a README changed interrupts people for
nothing, so a push touching only .claude/, .github/ or Markdown is skipped.

These run the real shell function against a real git repository, because the
decision is made from `git diff --name-only` and a test that only read the script
would not catch a pattern that matches the wrong paths.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]


def extract(script, name):
    start = script.find(f"\n{name}() {{")
    if start == -1:
        raise unittest.SkipTest(f"{name} is not in auto_updater.sh")
    body = []
    for line in script[start + 1:].split("\n"):
        body.append(line)
        if line == "}":
            return "\n".join(body)
    raise unittest.SkipTest(f"{name} has no closing brace in column zero")


class OnlyNonCodeChangesTests(TestCase):
    def setUp(self):
        for tool in ("bash", "git"):
            if not shutil.which(tool):
                raise unittest.SkipTest(f"{tool} is not available")
        path = ROOT / "auto_updater.sh"
        if not path.is_file():
            raise unittest.SkipTest("auto_updater.sh is not present; host-only test skipped")
        script = path.read_text(encoding="utf-8", errors="replace").replace("\r", "")
        regex_line = next(
            (ln for ln in script.splitlines() if ln.startswith("NON_CODE_PATHS_REGEX=")), None
        )
        if regex_line is None:
            raise unittest.SkipTest("NON_CODE_PATHS_REGEX is not in auto_updater.sh")
        self.shell = regex_line + "\n" + extract(script, "only_non_code_changes")

        self.repo = Path(tempfile.mkdtemp())
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")
        self.write("bot/__init__.py", "x = 1\n")
        self.write("README.md", "readme\n")
        self.base = self.commit("base")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def skipped(self, head):
        result = subprocess.run(
            ["bash", "-c", self.shell + f'\nonly_non_code_changes {self.base} {head}'],
            cwd=self.repo, capture_output=True, text=True,
        )
        return result.returncode == 0

    def test_a_plan_change_is_skipped(self):
        self.write(".claude/plans/streamerbot-plan.md", "phase 9\n")
        self.assertTrue(self.skipped(self.commit("plan")))

    def test_deleting_the_plan_is_skipped(self):
        """Asked for explicitly: an addition or a deletion."""
        self.write(".claude/plans/streamerbot-plan.md", "phase 9\n")
        self.base = self.commit("add plan")
        (self.repo / ".claude/plans/streamerbot-plan.md").unlink()
        self.assertTrue(self.skipped(self.commit("delete plan")))

    def test_documentation_anywhere_is_skipped(self):
        self.write("CHANGELOG.md", "entry\n")
        self.write("CLAUDE.md", "notes\n")
        self.write("docs/guide.md", "guide\n")
        self.assertTrue(self.skipped(self.commit("docs")))

    def test_github_settings_are_skipped(self):
        self.write(".github/ISSUE_TEMPLATE/bug_report.yml", "name: bug\n")
        self.assertTrue(self.skipped(self.commit("forms")))

    def test_a_code_change_is_not_skipped(self):
        self.write("bot/__init__.py", "x = 2\n")
        self.assertFalse(self.skipped(self.commit("code")))

    def test_code_with_a_changelog_entry_is_not_skipped(self):
        """Every code push carries a CHANGELOG entry, so the Markdown in it must
        not be what decides."""
        self.write("bot/__init__.py", "x = 3\n")
        self.write("CHANGELOG.md", "entry\n")
        self.assertFalse(self.skipped(self.commit("code and changelog")))

    def test_a_docs_push_followed_by_a_code_push_updates(self):
        """The skipped commit is not forgotten: the next diff spans both."""
        self.write(".claude/plans/streamerbot-plan.md", "phase 9\n")
        self.commit("plan")
        self.write("bot/__init__.py", "x = 4\n")
        self.assertFalse(self.skipped(self.commit("code")))

    def test_shell_scripts_and_the_dockerfile_are_code(self):
        for rel in ("streamerbot.sh", "Dockerfile", "project.env", "entrypoint.sh"):
            with self.subTest(rel=rel):
                self.base = self.git("rev-parse", "HEAD")
                self.write(rel, f"changed {rel}\n")
                self.assertFalse(self.skipped(self.commit(rel)))

    def test_a_markdown_like_name_that_is_not_markdown_is_code(self):
        self.write("bot/readme.md.py", "x = 5\n")
        self.assertFalse(self.skipped(self.commit("tricky name")))

    def test_an_unreadable_range_means_update(self):
        """Any doubt is resolved by updating, never by skipping."""
        self.assertFalse(self.skipped("0000000000000000000000000000000000000000"))


class WiringTests(TestCase):
    def setUp(self):
        path = ROOT / "auto_updater.sh"
        if not path.is_file():
            raise unittest.SkipTest("auto_updater.sh is not present")
        self.script = path.read_text(encoding="utf-8", errors="replace")

    def test_the_decision_is_made_before_triggering_an_update(self):
        check = self.script.index("only_non_code_changes \"$LOCAL_HASH\" \"$REMOTE_HASH\"")
        trigger = self.script.index('SHOULD_UPDATE=true', check)
        self.assertLess(check, trigger)

    def test_a_skip_is_logged_once_not_every_interval(self):
        self.assertIn('LAST_SKIPPED_REMOTE="$REMOTE_HASH"', self.script)
        self.assertIn('"$REMOTE_HASH" = "$LAST_SKIPPED_REMOTE"', self.script)


if __name__ == "__main__":
    unittest.main()
