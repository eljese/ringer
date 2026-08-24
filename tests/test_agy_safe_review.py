#!/usr/bin/env python3
"""Tests for the isolated AGY review-settings launcher."""

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
LAUNCHER = ROOT / "engines" / "agy_safe_review.py"
PROFILE = ROOT / "profiles" / "agy-review-settings.json"

sys.path.insert(0, str(ROOT / "engines"))
import agy_safe_review as safe_review  # noqa: E402


class AgySafeReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ringer-agy-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.home = self.runtime / "engine-homes" / "agy" / "run-1" / "review"
        self.home.mkdir(parents=True)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.fake_agy = self.bin_dir / "agy"
        self.fake_agy.write_text(
            """#!/usr/bin/env python3
import json
import os
import pathlib
import stat
import sys
settings = pathlib.Path(os.environ["HOME"]) / ".gemini" / "antigravity-cli" / "settings.json"
print(json.dumps({
    "argv": sys.argv[1:],
    "settings": json.loads(settings.read_text(encoding="utf-8")),
    "mode": stat.S_IMODE(settings.stat().st_mode),
    "path": str(settings),
}, sort_keys=True))
""",
            encoding="utf-8",
        )
        self.fake_agy.chmod(0o755)

    def environment(self, *, home: Path | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "RINGER_SAFE_ENFORCE": "1",
                "RINGER_RUNTIME_ROOT": str(self.runtime),
                "HOME": str(home or self.home),
                "PATH": str(self.bin_dir) + os.pathsep + env.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return env

    def run_launcher(
        self,
        *arguments: str,
        home: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LAUNCHER), *arguments],
            cwd=ROOT,
            env=self.environment(home=home),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )

    def test_launcher_writes_exact_profile_and_executes_agy(self) -> None:
        result = self.run_launcher("--model", "gemini-test", "-p", "review")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        expected = json.loads(PROFILE.read_text(encoding="utf-8"))
        self.assertEqual(payload["settings"], expected)
        self.assertEqual(payload["mode"], 0o600)
        self.assertEqual(payload["argv"], ["--model", "gemini-test", "-p", "review"])
        settings = payload["settings"]
        allow = set(settings["permissions"]["allow"])
        deny = set(settings["permissions"]["deny"])
        self.assertEqual(allow, {"read_file(*)", "write_file(*)"})
        self.assertEqual(
            deny,
            {
                "read_url(*)",
                "execute_url(*)",
                "command(*)",
                "unsandboxed(*)",
                "mcp(*)",
            },
        )
        self.assertNotIn("toolPermission", settings)
        self.assertNotIn("artifactReviewPolicy", settings)
        settings_path = Path(payload["path"])
        self.assertTrue(settings_path.is_file())
        self.assertEqual(settings_path.read_bytes(), PROFILE.read_bytes())
        self.assertEqual(stat.S_IMODE(settings_path.parent.stat().st_mode), 0o700)

    def test_launcher_refuses_home_outside_isolated_agy_tree(self) -> None:
        outside = self.root / "outside-home"
        outside.mkdir()
        result = self.run_launcher("-p", "review", home=outside)
        self.assertEqual(result.returncode, 2)
        self.assertIn("outside the isolated AGY engine-home tree", result.stderr)
        self.assertFalse((outside / ".gemini").exists())

    def test_launcher_refuses_to_overwrite_seeded_settings(self) -> None:
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"permissions":{"allow":["command(*)"]}}\n', encoding="utf-8")
        before = settings.read_bytes()

        result = self.run_launcher("-p", "review")

        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual(settings.read_bytes(), before)

    def test_profile_is_narrow_and_sparse_persistence_cannot_broaden_it(self) -> None:
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        self.assertFalse(profile["enableTelemetry"])
        self.assertFalse(profile["allowNonWorkspaceAccess"])
        self.assertEqual(
            set(profile["permissions"]["allow"]),
            {"read_file(*)", "write_file(*)"},
        )
        self.assertEqual(
            set(profile["permissions"]["deny"]),
            {
                "read_url(*)",
                "execute_url(*)",
                "command(*)",
                "unsandboxed(*)",
                "mcp(*)",
            },
        )
        for rules in profile["permissions"].values():
            for rule in rules:
                self.assertIn(rule.partition("(")[0], safe_review.SUPPORTED_ACTIONS)

        persisted = self.root / "persisted-settings.json"
        persisted.write_text(
            json.dumps(
                {
                    "permissions": {
                        "allow": ["read_file(*)", "write_file(*)"],
                        "deny": [
                            "command(*)",
                            "unsandboxed(*)",
                            "mcp(*)",
                            "read_url(*)",
                            "execute_url(*)",
                        ],
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        validated = safe_review.validate_persisted_settings(persisted)
        self.assertEqual(
            set(validated["permissions"]["deny"]),
            safe_review.REQUIRED_DENY,
        )

        persisted.write_text(
            '{"permissions":{"allow":["read_file(*)","write_file(*)","command(*)"],'
            '"deny":["command(*)","unsandboxed(*)","mcp(*)","read_url(*)",'
            '"execute_url(*)"]}}\n',
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            safe_review.validate_persisted_settings(persisted)

        persisted.write_text(
            '{"permissions":{"allow":["read_file(*)","write_file(*)"],'
            '"deny":["command(*)","unsandboxed(*)","mcp(*)","read_url(*)",'
            '"execute_url(*)"],"ask":["command(*)"]}}\n',
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            safe_review.validate_persisted_settings(persisted)

        persisted.write_text(
            '{"permissions":{"allow":["read_file(*)","write_file(*)"],'
            '"deny":["command(*)","unsandboxed(*)","mcp(*)","read_url(*)",'
            '"execute_url(*)"],"fallback":["read_file(*)"]}}\n',
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            safe_review.validate_persisted_settings(persisted)

        serialized = json.dumps(profile, sort_keys=True)
        self.assertNotIn("dangerously-skip-permissions", serialized)
        self.assertNotIn("always-proceed", serialized)
        self.assertNotIn('"allow": ["command(*)"]', serialized)


if __name__ == "__main__":
    unittest.main()
