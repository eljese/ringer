#!/usr/bin/env python3
"""Regression tests for the validator-compatible AGY review entrypoint."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
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

    def environment(self, *, path: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "RINGER_SAFE_ENFORCE": "1",
                "RINGER_RUNTIME_ROOT": str(self.runtime),
                "HOME": str(self.home),
                "PATH": path,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return env

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

    def test_entrypoint_version_probe_bypasses_profile_then_review_installs_it(self) -> None:
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
payload = {
    "argv": sys.argv[1:],
    "settings_exists": settings.is_file(),
}
if settings.is_file():
    payload["settings"] = json.loads(settings.read_text(encoding="utf-8"))
print(json.dumps(payload, sort_keys=True))
""",
            encoding="utf-8",
        )
        fake_agy.chmod(0o755)
        env = self.environment(
            path=str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        )

        version = subprocess.run(
            [str(ENTRYPOINT), "--version"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        version_payload = json.loads(version.stdout)
        self.assertEqual(version_payload["argv"], ["--version"])
        self.assertFalse(version_payload["settings_exists"])
        settings_path = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        self.assertFalse(settings_path.exists())

        review = subprocess.run(
            [str(ENTRYPOINT), "--model", "gemini-test", "-p", "review"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(review.returncode, 0, review.stderr)
        review_payload = json.loads(review.stdout)
        self.assertEqual(
            review_payload["argv"],
            ["--model", "gemini-test", "-p", "review"],
        )
        self.assertTrue(review_payload["settings_exists"])
        self.assertEqual(
            review_payload["settings"],
            json.loads(PROFILE.read_text(encoding="utf-8")),
        )
        self.assertEqual(settings_path.read_bytes(), PROFILE.read_bytes())

    def test_entrypoint_does_not_rediscover_itself_from_path(self) -> None:
        python_dir = self.root / "python-bin"
        python_dir.mkdir()
        (python_dir / "python3").symlink_to(Path(sys.executable).resolve())

        result = subprocess.run(
            [str(ENTRYPOINT), "--version"],
            env=self.environment(
                path=str(ENTRYPOINT.parent) + os.pathsep + str(python_dir)
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(
            "agy executable was not found outside the Ringer review launcher",
            result.stderr,
        )
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        self.assertFalse(settings.exists())


if __name__ == "__main__":
    unittest.main()
