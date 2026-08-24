#!/usr/bin/env python3
"""Regression tests for the validator-compatible AGY review entrypoint."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "engines" / "agy"
PROFILE = ROOT / "profiles" / "agy-review-settings.json"

import ringer  # noqa: E402


@unittest.skipIf(os.name == "nt", "the AGY safe-run entrypoint is POSIX-only")
class AgySafeReviewEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ringer-agy-entrypoint-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.home = self.runtime / "engine-homes" / "agy" / "run-1" / "review"
        self.home.mkdir(parents=True)
        self.workdir = self.root / "work"
        self.taskdir = self.workdir / "review"
        self.taskdir.mkdir(parents=True)

    def test_entrypoint_is_executable_and_named_agy(self) -> None:
        self.assertEqual(ENTRYPOINT.name, "agy")
        self.assertTrue(ENTRYPOINT.is_file())
        self.assertTrue(ENTRYPOINT.stat().st_mode & stat.S_IXUSR)

    def test_entrypoint_satisfies_composed_agy_command_policy(self) -> None:
        ringer.inspect_worker_command_flags(
            [
                str(ENTRYPOINT),
                "--add-dir",
                str(self.workdir),
                "--add-dir",
                str(self.taskdir),
                "--sandbox",
                "-p",
                "review",
            ],
            taskdir=self.taskdir,
            workdir=self.workdir,
            engine_name="agy",
        )

    def test_entrypoint_installs_profile_then_executes_real_agy(self) -> None:
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake_agy = bin_dir / "agy"
        fake_agy.write_text(
            """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
settings = pathlib.Path(os.environ["HOME"]) / ".gemini" / "antigravity-cli" / "settings.json"
print(json.dumps({
    "argv": sys.argv[1:],
    "settings": json.loads(settings.read_text(encoding="utf-8")),
}, sort_keys=True))
""",
            encoding="utf-8",
        )
        fake_agy.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "RINGER_SAFE_ENFORCE": "1",
                "RINGER_RUNTIME_ROOT": str(self.runtime),
                "HOME": str(self.home),
                "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        result = subprocess.run(
            [str(ENTRYPOINT), "--version"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["argv"], ["--version"])
        self.assertEqual(
            payload["settings"],
            json.loads(PROFILE.read_text(encoding="utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
