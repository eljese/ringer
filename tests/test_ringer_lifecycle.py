from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "ringer_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("ringer_lifecycle", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
lifecycle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lifecycle
SPEC.loader.exec_module(lifecycle)


class RingerLifecycleTests(unittest.TestCase):
    def test_structured_argv_check_quotes_github_expression(self) -> None:
        variables = {
            "{{TASK_DIR}}": "/tmp/task",
            "{{RUN_DIR}}": "/tmp/run",
            "{{ARTIFACT_DIR}}": "/tmp/artifacts",
            "{{SOURCE_REPO}}": "/tmp/repo",
            "{{BASE_SHA}}": "abc123",
            "{{ATTEMPT}}": "1",
        }
        check = lifecycle.normalize_check(
            {"argv": ["python3", "check.py", "--literal", "${{ github.sha }}", "{{TASK_DIR}}"]},
            variables,
        )
        self.assertIn("'${{ github.sha }}'", check)
        self.assertIn("/tmp/task", check)

    def test_unresolved_path_variable_fails_closed(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.substitute("{{UNKNOWN_PATH}}/report.md", {})

    def test_failure_classifier_distinguishes_infrastructure(self) -> None:
        self.assertEqual(lifecycle.classify_failure("rate limit quota exhausted", returncode=1), "PROVIDER_QUOTA")
        self.assertEqual(lifecycle.classify_failure("worker timed out after 300s", returncode=1), "PROVIDER_TIMEOUT")
        self.assertEqual(lifecycle.classify_failure("Could not resolve host: api.example", returncode=1), "NETWORK_SANDBOX")
        self.assertEqual(lifecycle.classify_failure("check assertion failed", returncode=1), "CHECK_FAILURE")

    def test_review_packet_obeys_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "a.txt").write_text("a\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            (repo / "a.txt").write_text("b\n" * 1000, encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "change"], cwd=repo, check=True)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            packet = lifecycle.build_review_packet(repo, base, head, tier=2, max_bytes=700)
            self.assertLessEqual(len(packet.encode("utf-8")), 760)
            self.assertIn("Review packet tier 2", packet)

    def test_export_worktree_patch_includes_untracked_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
            (repo / "new.txt").write_text("new\n", encoding="utf-8")
            target = repo.parent / "result.patch"
            patch = lifecycle.export_worktree_patch(repo, target)
            self.assertEqual(patch, target)
            text = target.read_text(encoding="utf-8")
            self.assertIn("tracked.txt", text)
            self.assertIn("new.txt", text)
            self.assertEqual(len(lifecycle.sha256_file(target)), 64)

    def test_atomic_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            lifecycle.atomic_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})


if __name__ == "__main__":
    unittest.main()
