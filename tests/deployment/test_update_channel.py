"""Update channels: a server follows stable (releases) or latest (every push).

These run update_channel.sh against real git repositories, with a bare
repository standing in for GitHub, because the decisions are made by git itself:
whether origin has a stable branch at all (ls-remote --exit-code), and whether
following it would move a server backwards (merge-base --is-ancestor). A stub
answering those would only test the stub.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.deployment.bash_sandbox import ROOT, deployment_scripts_present, find_bash


@unittest.skipUnless(find_bash() and shutil.which("git"), "bash and git are required")
@unittest.skipUnless(
    deployment_scripts_present("update_channel.sh"),
    "update_channel.sh is not present; host-only test skipped",
)
class UpdateChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = self.tmp / "origin.git"
        self.server = self.tmp / "server"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.tmp, "clone", "-q", str(self.origin), str(self.server))
        self.git(self.server, "checkout", "-q", "-b", "main")
        shutil.copy2(ROOT / ".gitignore", self.server / ".gitignore")
        self.release = self.commit("1.0.0")
        self.git(self.server, "push", "-q", "origin", "main")

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=cwd, capture_output=True, text=True, check=True, timeout=60,
        ).stdout.strip()

    def commit(self, message: str) -> str:
        (self.server / "version.txt").write_text(message, encoding="utf-8")
        self.git(self.server, "add", "-A")
        self.git(self.server, "commit", "-qm", message)
        return self.git(self.server, "rev-parse", "HEAD")

    def publish_stable(self, commit: str) -> None:
        self.git(self.server, "push", "-q", "origin", f"{commit}:refs/heads/stable")

    def shell(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        prelude = f'SCRIPT_DIR="{self.server}"\n. "{ROOT / "update_channel.sh"}"\n'
        return subprocess.run(
            [find_bash(), "-c", prelude + script],
            cwd=self.server, capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", **env},
        )

    def out(self, script: str, **env: str) -> str:
        result = self.shell(script, **env)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    # --- which channel ------------------------------------------------------

    def test_a_server_that_never_chose_follows_stable(self) -> None:
        self.assertEqual("stable", self.out("update_channel"))

    def test_project_env_can_change_the_default(self) -> None:
        self.assertEqual("latest", self.out("update_channel", STREAMERBOT_DEFAULT_CHANNEL="latest"))

    def test_the_server_file_beats_the_default(self) -> None:
        self.out("set_update_channel latest")
        self.assertEqual("latest", self.out("update_channel", STREAMERBOT_DEFAULT_CHANNEL="stable"))

    def test_an_unrecognised_value_reads_as_stable(self) -> None:
        (self.server / "update_channel.env").write_text("STREAMERBOT_CHANNEL=nightly\n", encoding="utf-8")
        self.assertEqual("stable", self.out("update_channel"))

    def test_only_stable_and_latest_can_be_written(self) -> None:
        self.assertNotEqual(0, self.shell("set_update_channel nightly").returncode)
        self.assertFalse((self.server / "update_channel.env").exists())

    def test_the_choice_survives_an_update(self) -> None:
        """update.sh ends every update with reset --hard and clean -fd. The file
        survives them only because .gitignore names it."""
        self.out("set_update_channel latest")
        self.git(self.server, "reset", "--hard", "HEAD")
        self.git(self.server, "clean", "-fd")
        self.assertEqual("latest", self.out("update_channel"))

    # --- which branch -------------------------------------------------------

    def test_stable_follows_the_stable_branch_once_it_exists(self) -> None:
        self.publish_stable(self.release)
        self.assertEqual("stable", self.out("update_branch"))

    def test_latest_follows_main(self) -> None:
        self.publish_stable(self.release)
        self.out("set_update_channel latest")
        self.assertEqual("main", self.out("update_branch"))

    def test_a_repository_with_no_release_yet_follows_main(self) -> None:
        """Following a branch that does not exist would mean never updating."""
        self.assertEqual("main", self.out("update_branch"))

    def test_an_unreachable_origin_does_not_fall_back_to_main(self) -> None:
        """A network failure is not "no stable branch". Falling back on it would
        put a stable server on development code for a cycle."""
        self.git(self.server, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        self.assertEqual("stable", self.out("update_branch"))

    # --- never backwards ----------------------------------------------------

    def test_a_release_behind_the_running_code_would_be_a_downgrade(self) -> None:
        self.commit("unreleased work")
        self.assertEqual("yes", self.out(f'channel_would_downgrade {self.release} && echo yes || echo no'))

    def test_a_release_ahead_of_the_running_code_is_not(self) -> None:
        self.git(self.server, "checkout", "-q", "-b", "older", self.release)
        newer = self.commit("1.1.0")
        self.git(self.server, "checkout", "-q", "--detach", self.release)
        self.assertEqual("no", self.out(f'channel_would_downgrade {newer} && echo yes || echo no'))

    def test_the_same_commit_is_not_a_downgrade(self) -> None:
        self.assertEqual("no", self.out(f'channel_would_downgrade {self.release} && echo yes || echo no'))

    def test_a_release_is_described_by_its_tag(self) -> None:
        self.git(self.server, "tag", "v1.0.0", self.release)
        self.assertEqual("v1.0.0", self.out(f"describe_commit {self.release}"))


@unittest.skipUnless(
    deployment_scripts_present("update.sh", "auto_updater.sh", "streamerbot.sh", "masc.sh"),
    "deployment scripts are not present; host-only test skipped",
)
class WiringTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8", errors="replace")

    def test_every_script_that_updates_reads_the_channel(self) -> None:
        for name in ("update.sh", "auto_updater.sh", "streamerbot.sh", "masc.sh"):
            with self.subTest(name=name):
                self.assertIn("update_channel.sh", self.read(name))

    def test_update_sh_takes_its_branch_from_the_channel(self) -> None:
        script = self.read("update.sh")
        self.assertIn('BRANCH="$(update_branch)"', script)
        self.assertNotIn('BRANCH="${STREAMERBOT_BRANCH:-main}"', script)

    def test_update_sh_never_resets_while_a_switch_waits(self) -> None:
        script = self.read("update.sh")
        wait = script.index('if [ "${CHANNEL_WAIT:-false}" = "true" ]; then')
        reset = script.index('git reset --hard "origin/$BRANCH"')
        self.assertLess(wait, reset)

    def test_the_local_branch_is_named_after_the_one_followed(self) -> None:
        self.assertIn('git checkout -q -f -B "$BRANCH" "origin/$BRANCH"', self.read("update.sh"))

    def test_the_server_setting_is_not_tracked(self) -> None:
        self.assertIn("/update_channel.env", self.read(".gitignore").splitlines())


if __name__ == "__main__":
    unittest.main()
