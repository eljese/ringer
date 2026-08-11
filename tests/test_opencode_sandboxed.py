#!/usr/bin/env python3
"""Offline regression tests for the cross-platform OpenCode wrapper."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "engines" / "opencode-sandboxed.sh"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class OpenCodeSandboxWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="opencode-wrapper-test-")
        self.root = Path(self.temp.name)
        self.taskdir = self.root / "taskdir"
        self.stubbin = self.root / "bin"
        self.taskdir.mkdir()
        self.stubbin.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_linux_sandbox_uses_bwrap_and_binds_taskdir(self) -> None:
        args_file = self.root / "bwrap-args.txt"
        bwrap = self.stubbin / "bwrap"
        write_executable(
            bwrap,
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$@\" > '{args_file}'\n",
        )
        write_executable(
            self.stubbin / "opencode",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        env = os.environ.copy()
        env["PATH"] = f"{self.stubbin}{os.pathsep}{env.get('PATH', '')}"
        env["RINGER_OPENCODE_BWRAP_BIN"] = str(bwrap)

        result = subprocess.run(
            [str(WRAPPER), str(self.taskdir), "run", "--auto", "noop"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        args = args_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("--ro-bind", args)
        self.assertIn("--bind", args)
        self.assertIn(str(self.taskdir), args)
        self.assertIn("--chdir", args)
        self.assertIn("run", args)
        self.assertIn("--auto", args)

    def test_full_access_switch_skips_bwrap(self) -> None:
        marker = self.root / "opencode-ran.txt"
        write_executable(
            self.stubbin / "opencode",
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$PWD\" > '{marker}'\n",
        )
        env = os.environ.copy()
        env["PATH"] = f"{self.stubbin}{os.pathsep}{env.get('PATH', '')}"
        env["RINGER_OPENCODE_BWRAP_BIN"] = str(self.root / "missing-bwrap")

        result = subprocess.run(
            [str(WRAPPER), str(self.taskdir), "--no-sandbox", "run", "noop"],
            cwd=self.taskdir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(self.taskdir), marker.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
