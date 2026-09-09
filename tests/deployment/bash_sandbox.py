from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def flock_available() -> bool:
    """True when flock exists and actually excludes a second holder.

    update.sh serialises itself with flock. Git Bash on Windows ships no flock at
    all, so acquire_update_lock silently does nothing there and any test of
    mutual exclusion measures the missing binary rather than the script. These
    tests therefore skip on such a host and run on Linux, which is where the
    script actually runs.

    Presence is not assumed to mean working: some environments provide a stub.
    """
    if not shutil.which("flock"):
        return False
    bash = find_bash()
    if not bash:
        return False
    probe = (
        'T="$(mktemp)"; exec 9>"$T"; '
        'flock -n 9 || exit 1; '
        'flock -n "$T" -c true && exit 1; '
        'exit 0'
    )
    try:
        return subprocess.run(
            [bash, "-c", probe], capture_output=True, timeout=20
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def deployment_scripts_present(*names: str) -> bool:
    """True when the host-side shell scripts are on disk.

    .dockerignore keeps update.sh, auto_updater.sh and streamerbot.sh out of the
    runtime image on purpose: they manage containers from the host and have no
    job inside one. So these tests pass on a host and must skip in the image
    rather than failing on files that are absent by design.
    """
    return all((ROOT / name).is_file() for name in (names or ("update.sh", "auto_updater.sh")))


def find_bash() -> str | None:
    return shutil.which("bash")


class BashSandbox:
    """Run copied deployment scripts with all external effects redirected."""

    def __init__(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "trace.log"

    def close(self) -> None:
        self._temporary_directory.cleanup()

    def copy(self, name: str) -> Path:
        destination = self.root / name
        shutil.copy2(ROOT / name, destination)
        destination.chmod(0o755)
        return destination

    def shim(self, name: str, body: str) -> Path:
        executable = self.bin / name
        executable.write_text("#!/bin/bash\nset -eu\n" + body + "\n", encoding="utf-8")
        executable.chmod(0o755)
        return executable

    def run(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 5,
    ) -> subprocess.CompletedProcess[str]:
        bash = find_bash()
        if bash is None:
            raise unittest.SkipTest("bash is required for deployment behavior tests")
        process_env = os.environ.copy()
        process_env.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{process_env.get('PATH', '')}",
                "TEST_TRACE": str(self.trace),
                "LC_ALL": "C",
            }
        )
        if env:
            process_env.update(env)
        return subprocess.run(
            [bash, *command],
            cwd=self.root,
            env=process_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
