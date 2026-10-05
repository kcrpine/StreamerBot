"""update.sh syncs with `git reset --hard` and `git clean -fd` and takes no copy
of bots/ around them. That is only safe because /bots/ is gitignored: neither
command touches an ignored path. This test pins that premise against a scratch
repository carrying the project's own .gitignore: without the rule, clean -fd
deletes the untracked bots/ and the test fails, rather than an update wiping bots.

It builds its own repository instead of asking the checkout, because inside the
test image the checkout is a bind mount owned by another uid and git refuses it
as dubious ownership (exit 128).

The copy used to exist, and it put all of bots/ into /tmp on every update --
3.2 GB on a host whose cPanel /tmp is 3.9 GB.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]


class UpdateKeepsBotsTests(TestCase):
    def setUp(self):
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not available on this host")
        if not (ROOT / ".gitignore").is_file():
            raise unittest.SkipTest(".gitignore is not present")

    def git(self, repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=60,
        )

    def test_reset_and_clean_leave_an_ignored_bots_folder_alone(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        shutil.copy2(ROOT / ".gitignore", repo / ".gitignore")
        for args in (
            ("init", "-q"),
            ("-c", "user.name=t", "-c", "user.email=t@t", "add", ".gitignore"),
            ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"),
        ):
            self.assertEqual(self.git(repo, *args).returncode, 0)
        bot = repo / "bots" / "doug"
        bot.mkdir(parents=True)
        (bot / "config.json").write_text('{"keep": true}', encoding="utf-8")
        (repo / "stray.txt").write_text("untracked", encoding="utf-8")

        self.git(repo, "reset", "--hard", "HEAD")
        self.git(repo, "clean", "-fd")

        self.assertFalse((repo / "stray.txt").exists(), "clean -fd should still remove untracked files")
        self.assertEqual((bot / "config.json").read_text(encoding="utf-8"), '{"keep": true}')
