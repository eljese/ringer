from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "tools" / "ringer_supervisor_hardened.py"
spec = importlib.util.spec_from_file_location("ringer_supervisor_hardened", MODULE_PATH)
assert spec and spec.loader
hardened = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hardened
spec.loader.exec_module(hardened)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Tests")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "base.txt")
    git(path, "commit", "-qm", "base")


class HardenedSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        init_repo(self.repo)
        self.run_dir = self.root / "run"
        self.artifacts = self.root / "artifacts"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self, manifest: Path, ringer: Path, *, cleanup: bool = False):
        return argparse.Namespace(
            manifest=manifest,
            ringer=ringer,
            config=None,
            identity="test",
            artifact_dir=self.artifacts,
            cleanup=cleanup,
        )

    def write_manifest(self, task: dict, routes: list[dict]) -> Path:
        manifest = {
            "run_name": "hardened-test",
            "workdir": str(self.run_dir),
            "repo": str(self.repo),
            "supervisor": {
                "base_ref": "HEAD",
                "minimum_free_bytes": 1,
                "minimum_free_inodes": 1,
                "heartbeat_seconds": 0.05,
                "no_progress_seconds": 2,
                "require_inference_probes": False,
                "routes": routes,
                "fallback_on": ["CHECK_FAILURE"],
            },
            "tasks": [task],
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_rejects_cross_role_implementation_route(self) -> None:
        with self.assertRaisesRegex(
            hardened.HardenedSupervisorError, "MANIFEST_POLICY_FAILURE"
        ):
            hardened._route_policy(
                {}, [hardened.legacy.Route("grok", "grok-4.6")]
            )

    def test_rejects_worker_authored_report_as_objective_check(self) -> None:
        task = {
            "objective_checks": [
                {"argv": ["grep", "IMPLEMENTATION_COMPLETE", "notes.md"]}
            ]
        }
        with self.assertRaisesRegex(
            hardened.HardenedSupervisorError, "worker-authored report"
        ):
            hardened._objective_check_argvs(
                task,
                run_dir=self.run_dir,
                task_dir=self.run_dir / "task",
                artifact_dir=self.artifacts / "task",
                repository=self.repo,
                attempt=1,
            )

    def test_fallback_attempt_starts_from_fresh_baseline(self) -> None:
        fake = self.root / "fake-ringer.py"
        fake.write_text(
            """#!/usr/bin/env python3
import json
import pathlib
import sys
args = sys.argv[1:]
manifest_path = pathlib.Path(args[args.index("run") + 1])
manifest = json.loads(manifest_path.read_text())
task = manifest["tasks"][0]
worktree = pathlib.Path(manifest["workdir"]) / task["key"]
if task.get("model") == "minimax-first":
    (worktree / "poison.txt").write_text("must not leak\\n")
    print("check failed")
    sys.exit(1)
if (worktree / "poison.txt").exists():
    print("dirty state leaked from prior attempt")
    sys.exit(9)
(worktree / "base.txt").write_text("changed\\n")
(worktree / "result.txt").write_text("done\\n")
sys.exit(0)
""",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        task = {
            "key": "task-1",
            "spec": "make the scoped change",
            "check": "true",
            "expect_files": ["{{TASK_DIR}}/result.txt"],
            "objective_checks": [
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; "
                        "assert Path('base.txt').read_text() == 'changed\\n'; "
                        "assert not Path('poison.txt').exists()",
                    ]
                }
            ],
        }
        routes = [
            {
                "engine": "opencode",
                "model": "minimax-first",
                "timeout_seconds": 5,
            },
            {
                "engine": "opencode",
                "model": "minimax-second",
                "timeout_seconds": 5,
            },
        ]
        manifest = self.write_manifest(task, routes)
        self.assertEqual(hardened.command_run(self.args(manifest, fake)), 0)
        outcome = json.loads(
            (self.artifacts / "supervisor-outcome.json").read_text()
        )
        task_outcome = outcome["tasks"][0]
        self.assertEqual(task_outcome["selected_model"], "minimax-second")
        self.assertEqual(
            [item["status"] for item in task_outcome["attempts"]],
            ["fail", "pass"],
        )
        first, second = task_outcome["attempts"]
        self.assertEqual(first["baseline_sha"], second["baseline_sha"])
        self.assertNotEqual(first["worktree"], second["worktree"])
        self.assertFalse(Path(second["worktree"], "poison.txt").exists())
        self.assertTrue(Path(second["provenance_path"]).is_file())

    def test_worker_zero_exit_cannot_override_failed_objective_check(self) -> None:
        fake = self.root / "fake-ringer.py"
        fake.write_text(
            """#!/usr/bin/env python3
import json
import pathlib
import sys
args = sys.argv[1:]
manifest_path = pathlib.Path(args[args.index("run") + 1])
manifest = json.loads(manifest_path.read_text())
task = manifest["tasks"][0]
worktree = pathlib.Path(manifest["workdir"]) / task["key"]
(worktree / "notes.md").write_text("IMPLEMENTATION_COMPLETE\\n")
(worktree / "result.txt").write_text("done\\n")
sys.exit(0)
""",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        task = {
            "key": "task-1",
            "spec": "claim completion",
            "check": "true",
            "expect_files": ["{{TASK_DIR}}/result.txt"],
            "objective_checks": [
                {"argv": [sys.executable, "-c", "raise SystemExit(7)"]}
            ],
        }
        manifest = self.write_manifest(
            task,
            [
                {
                    "engine": "opencode",
                    "model": "minimax-only",
                    "timeout_seconds": 5,
                }
            ],
        )
        self.assertEqual(hardened.command_run(self.args(manifest, fake)), 1)
        outcome = json.loads(
            (self.artifacts / "supervisor-outcome.json").read_text()
        )
        attempt = outcome["tasks"][0]["attempts"][0]
        self.assertEqual(outcome["status"], "fail")
        self.assertEqual(attempt["failure_class"], "CHECK_FAILURE")
        self.assertEqual(attempt["objective_checks"][0]["returncode"], 7)

    def test_exception_after_run_start_still_writes_terminal_outcome(self) -> None:
        fake = self.root / "fake-ringer.py"
        fake.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
        fake.chmod(0o755)
        task = {
            "key": "task-1",
            "spec": "test",
            "check": "true",
            "expect_files": [],
            "objective_checks": [{"argv": ["true"]}],
        }
        manifest = self.write_manifest(
            task,
            [
                {
                    "engine": "opencode",
                    "model": "minimax-only",
                    "timeout_seconds": 5,
                }
            ],
        )
        original = hardened.supervise_task
        try:
            def explode(*_args, **_kwargs):
                raise RuntimeError("synthetic supervisor failure")

            hardened.supervise_task = explode
            self.assertEqual(hardened.command_run(self.args(manifest, fake)), 2)
        finally:
            hardened.supervise_task = original
        outcome = json.loads(
            (self.artifacts / "supervisor-outcome.json").read_text()
        )
        events = [
            json.loads(line)
            for line in (self.artifacts / "supervisor-events.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(outcome["status"], "fail")
        self.assertIn("synthetic supervisor failure", outcome["error"])
        self.assertEqual(events[-1]["type"], "RUN_FAILED")
        self.assertEqual(sum(item["type"] == "RUN_FAILED" for item in events), 1)


if __name__ == "__main__":
    unittest.main()
