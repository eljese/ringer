from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "tools" / "ringer_supervisor.py"
spec = importlib.util.spec_from_file_location("ringer_supervisor", MODULE_PATH)
assert spec and spec.loader
ringer_supervisor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ringer_supervisor
spec.loader.exec_module(ringer_supervisor)


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


class RingerSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_event_writer_rejects_post_terminal(self) -> None:
        path = self.root / "events.jsonl"
        writer = ringer_supervisor.EventWriter(path, "run-1")
        writer.emit("RUN_STARTED")
        writer.emit("RUN_COMPLETED")
        with self.assertRaises(ringer_supervisor.SupervisorError):
            writer.emit("WORKER_STARTED")
        events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(
            [event["type"] for event in events],
            ["RUN_STARTED", "RUN_COMPLETED"],
        )

    def test_preflight_checks_common_ancestor_and_capacity(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        source = {"workdir": str(self.root / "run"), "repo": str(repo)}
        report = ringer_supervisor.preflight(
            source,
            supervisor={
                "base_ref": "HEAD",
                "minimum_free_bytes": 1,
                "minimum_free_inodes": 1,
            },
            routes=[ringer_supervisor.Route("fake")],
        )
        self.assertEqual(report.head_sha, git(repo, "rev-parse", "HEAD"))
        self.assertEqual(report.common_ancestor, report.head_sha)
        self.assertGreater(report.free_bytes, 0)
        self.assertGreater(report.free_inodes, 0)

    def test_routes_from_minimax_to_grok_without_orchestrator_polling(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        run_dir = self.root / "run"
        fake = self.root / "fake-ringer.py"
        fake.write_text(
            """#!/usr/bin/env python3
import json
import pathlib
import sys
manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())
task = manifest["tasks"][0]
workdir = pathlib.Path(manifest["workdir"]) / task["key"]
if task.get("engine") == "grok":
    (workdir / "result.txt").write_text("fixed\\n")
    (workdir / "base.txt").write_text("changed\\n")
    sys.exit(0)
print("check failed")
sys.exit(1)
""",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        manifest = {
            "run_name": "routing-test",
            "workdir": str(run_dir),
            "repo": str(repo),
            "supervisor": {
                "base_ref": "HEAD",
                "minimum_free_bytes": 1,
                "minimum_free_inodes": 1,
                "heartbeat_seconds": 0.05,
                "routes": [
                    {
                        "engine": "opencode",
                        "model": "MiniMax-M3",
                        "timeout_seconds": 5,
                    },
                    {
                        "engine": "grok",
                        "model": "grok-build",
                        "timeout_seconds": 5,
                    },
                ],
                "fallback_on": ["CHECK_FAILURE"],
            },
            "tasks": [
                {
                    "key": "task-1",
                    "spec": "repair",
                    "check": "true",
                    "expect_files": ["{{TASK_DIR}}/result.txt"],
                }
            ],
        }
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        args = type(
            "Args",
            (),
            {
                "manifest": manifest_path,
                "ringer": fake,
                "config": None,
                "identity": "test",
                "artifact_dir": self.root / "artifacts",
                "cleanup": False,
            },
        )()
        self.assertEqual(ringer_supervisor.command_run(args), 0)
        artifact_root = self.root / "artifacts"
        outcome = json.loads(
            (artifact_root / "supervisor-outcome.json").read_text()
        )
        task = outcome["tasks"][0]
        self.assertEqual(task["selected_engine"], "grok")
        self.assertEqual(
            [attempt["status"] for attempt in task["attempts"]],
            ["failed", "succeeded"],
        )
        events = [
            json.loads(line)["type"]
            for line in (artifact_root / "supervisor-events.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(events.count("RUN_STARTED"), 1)
        self.assertEqual(events[-1], "RUN_COMPLETED")
        self.assertIn("WORKER_FAILED", events)
        self.assertIn("WORKER_SUCCEEDED", events)


if __name__ == "__main__":
    unittest.main()
