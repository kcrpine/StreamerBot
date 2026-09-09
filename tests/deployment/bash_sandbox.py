from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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
