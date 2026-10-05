"""Behavioral deployment tests.

These tests write stub commands (docker, ufw, date, ...) and harness scripts into
a temporary directory and run them. On a host whose /tmp is mounted noexec, the
harnesses fail with "Permission denied" and, worse, bash's PATH lookup skips the
stubs it cannot execute and runs the host's real commands instead: the real
clock, the real ufw, as root. That reads as twelve unrelated test failures rather
than one mount option. So before any test runs, the temporary directory is moved
to one where an executable file actually executes.
"""

import os
import subprocess
import tempfile
from pathlib import Path


def _can_execute_in(directory):
    try:
        with tempfile.TemporaryDirectory(dir=directory) as probe_dir:
            probe = Path(probe_dir) / "probe.sh"
            probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            probe.chmod(0o755)
            return subprocess.run([str(probe)], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _use_an_executable_tempdir():
    # Windows runs these through Git Bash and has no noexec mounts to work around.
    if os.name == "nt":
        return
    default = tempfile.gettempdir()
    if _can_execute_in(default):
        return
    cache = Path.home() / ".cache" / "streamerbot-tests"
    for candidate in ("/dev/shm", "/var/tmp", str(cache)):
        try:
            Path(candidate).mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if _can_execute_in(candidate):
            tempfile.tempdir = candidate
            return


_use_an_executable_tempdir()
