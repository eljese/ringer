from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
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
        stderr=subprocess.STDOUT,
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
        self.provider = self.root / "opencode"
        self.provider.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '--model' not in sys.argv or not any('minimax' in item.lower() for item in sys.argv):\n"
            "    raise SystemExit(9)\n"
            "print('PROBE_OK')\n",
            encoding="utf-8",
        )
        self.provider.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self, manifest: Path, ringer: Path, *, cleanup: bool = True):
        return argparse.Namespace(
            manifest=manifest,
            ringer=ringer,
            config=None,
            identity="test",
            artifact_dir=self.artifacts,
            cleanup=cleanup,
        )

    def write_manifest(
        self,
        task: dict,
        routes: list[dict],
        *,
        provider_probes: dict | None = None,
        supervisor_overrides: dict | None = None,
    ) -> Path:
        probes = provider_probes or {}
        for route in routes:
            model = route["model"]
            probes.setdefault(
                "opencode:" + model.lower(),
                {
                    "kind": "inference",
                    "argv": [
                        str(self.provider),
                        "run",
                        "--model",
                        model,
                        "Return exactly PROBE_OK",
                    ],
                    "expected_output": "PROBE_OK",
                },
            )
        supervisor = {
            "base_ref": "HEAD",
            "minimum_free_bytes": 1,
            "minimum_free_inodes": 1,
            "heartbeat_seconds": 0.05,
            "no_progress_seconds": 2,
            "routes": routes,
            "fallback_on": ["CHECK_FAILURE", "PROVIDER_TIMEOUT", "NO_PROGRESS"],
            "provider_probes": probes,
        }
        if supervisor_overrides:
            supervisor.update(supervisor_overrides)
        manifest = {
            "run_name": "hardened-test",
            "workdir": str(self.run_dir),
            "repo": str(self.repo),
            "supervisor": supervisor,
            "tasks": [task],
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    @staticmethod
    def task(*, objective: list[str] | None = None, expect: bool = True) -> dict:
        return {
            "key": "task-1",
            "spec": "make the scoped change",
            "check": "true",
            "expect_files": ["{{TASK_DIR}}/result.txt"] if expect else [],
            "objective_checks": [{"argv": objective or ["git", "diff", "--check"]}],
        }

    def test_rejects_cross_role_duplicate_and_excess_routes(self) -> None:
        with self.assertRaisesRegex(hardened.HardenedSupervisorError, "MANIFEST_POLICY_FAILURE"):
            hardened._route_policy({}, [hardened.legacy.Route("grok", "grok-4.6")])
        routes = [
            hardened.legacy.Route("opencode", "minimax-one"),
            hardened.legacy.Route("opencode", "minimax-one"),
        ]
        with self.assertRaisesRegex(hardened.HardenedSupervisorError, "duplicate"):
            hardened._route_policy({}, routes)
        with self.assertRaisesRegex(hardened.HardenedSupervisorError, "limited"):
            hardened._route_policy({}, routes + [hardened.legacy.Route("opencode", "minimax-three")])

    def test_rejects_report_indirection_and_completion_marker_checks(self) -> None:
        cases = [
            ["bash", "-lc", "grep IMPLEMENTATION_COMPLETE notes.md"],
            ["sh", "-c", "grep IMPLEMENTATION_COMPLETE notes.md"],
            [sys.executable, "-c", "print(open('notes.md').read())"],
            ["grep", "IMPLEMENTATION_COMPLETE", "renamed.txt"],
            ["/bin/true"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaisesRegex(
                hardened.HardenedSupervisorError, "MANIFEST_POLICY_FAILURE"
            ):
                hardened._objective_check_argvs(
                    {"objective_checks": [{"argv": argv}]},
                    run_dir=self.run_dir,
                    task_dir=self.run_dir / "task",
                    artifact_dir=self.artifacts / "task",
                    repository=self.repo,
                    attempt=1,
                    baseline_sha=git(self.repo, "rev-parse", "HEAD"),
                )

        task_dir = self.run_dir / "task"
        task_dir.mkdir(parents=True)
        (task_dir / "notes.md").write_text("IMPLEMENTATION_COMPLETE\n", encoding="utf-8")
        (task_dir / "renamed.txt").symlink_to(task_dir / "notes.md")
        with self.assertRaisesRegex(hardened.HardenedSupervisorError, "symlink"):
            hardened._objective_check_argvs(
                {"objective_checks": [{"argv": ["grep", "x", "renamed.txt"]}]},
                run_dir=self.run_dir,
                task_dir=task_dir,
                artifact_dir=self.artifacts / "task",
                repository=self.repo,
                attempt=1,
                baseline_sha=git(self.repo, "rev-parse", "HEAD"),
            )

    def test_fallback_timeout_starts_clean_from_immutable_baseline(self) -> None:
        fake = self.root / "fake-ringer.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys, time\n"
            "args = sys.argv[1:]\n"
            "manifest = json.loads(pathlib.Path(args[args.index('run') + 1]).read_text())\n"
            "task = manifest['tasks'][0]\n"
            "worktree = pathlib.Path(manifest['workdir']) / task['key']\n"
            "if task['model'] == 'minimax-first':\n"
            "    (worktree / 'poison.txt').write_text('poison\\n')\n"
            "    (worktree / 'base.txt').write_text('dirty\\n')\n"
            "    time.sleep(10)\n"
            "else:\n"
            "    assert not (worktree / 'poison.txt').exists()\n"
            "    assert (worktree / 'base.txt').read_text() == 'base\\n'\n"
            "    (worktree / 'base.txt').write_text('changed\\n')\n"
            "    (worktree / 'result.txt').write_text('done\\n')\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        task = self.task()
        routes = [
            {"engine": "opencode", "model": "minimax-first", "timeout_seconds": 0.25},
            {"engine": "opencode", "model": "minimax-second", "timeout_seconds": 5},
        ]
        manifest = self.write_manifest(task, routes)
        self.assertEqual(hardened.command_run(self.args(manifest, fake)), 0)
        outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
        task_outcome = outcome["tasks"][0]
        self.assertEqual(task_outcome["selected_model"], "minimax-second")
        self.assertEqual([item["status"] for item in task_outcome["attempts"]], ["fail", "pass"])
        first, second = task_outcome["attempts"]
        self.assertEqual(first["source_baseline_sha"], second["source_baseline_sha"])
        self.assertNotEqual(first["worktree"], second["worktree"])
        self.assertEqual(first["failure_class"], "PROVIDER_TIMEOUT")
        self.assertEqual(second["post_check_head_sha"], first["source_baseline_sha"])

    def test_objective_mutation_is_bound_to_post_check_patch(self) -> None:
        fake = self.root / "fake-ringer.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "manifest = json.loads(pathlib.Path(sys.argv[sys.argv.index('run') + 1]).read_text())\n"
            "worktree = pathlib.Path(manifest['workdir']) / manifest['tasks'][0]['key']\n"
            "(worktree / 'result.txt').write_text('worker\\n')\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        manifest = self.write_manifest(self.task(), [{"engine": "opencode", "model": "minimax-only", "timeout_seconds": 5}], supervisor_overrides={"objective_check_timeout_seconds": 5})
        data = json.loads(manifest.read_text())
        data["tasks"][0]["objective_checks"] = [{"argv": ["/usr/bin/touch", "post-check.txt"]}]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(hardened.command_run(self.args(manifest, fake)), 0)
        outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
        attempt = outcome["tasks"][0]["attempts"][0]
        patch = Path(attempt["patch_path"])
        self.assertTrue(patch.is_file())
        self.assertEqual(attempt["patch_sha256"], hardened.lifecycle.sha256_file(patch))
        self.assertIn("post-check.txt", patch.read_text(encoding="utf-8"))
        self.assertNotEqual(attempt["patch_sha256"], attempt["worker_patch_sha256"])

    def test_objective_cannot_mutate_source_checkout(self) -> None:
        fake = self.root / "fake-ringer.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "manifest = json.loads(pathlib.Path(sys.argv[sys.argv.index('run') + 1]).read_text())\n"
            "worktree = pathlib.Path(manifest['workdir']) / manifest['tasks'][0]['key']\n"
            "(worktree / 'result.txt').write_text('worker\\n')\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        manifest = self.write_manifest(
            self.task(objective=["/usr/bin/touch", "{{SOURCE_REPO}}/source-mutated.txt"]),
            [{"engine": "opencode", "model": "minimax-only", "timeout_seconds": 5}],
        )
        self.assertEqual(hardened.command_run(self.args(manifest, fake)), 1)
        outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
        self.assertEqual(outcome["tasks"][0]["failure_class"], "MANIFEST_POLICY_FAILURE")
        self.assertTrue((self.repo / "source-mutated.txt").exists())

    def test_worker_commit_is_rejected_and_source_head_stays_immutable(self) -> None:
        fake = self.root / "fake-ringer.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, subprocess, sys\n"
            "manifest = json.loads(pathlib.Path(sys.argv[sys.argv.index('run') + 1]).read_text())\n"
            "worktree = pathlib.Path(manifest['workdir']) / manifest['tasks'][0]['key']\n"
            "(worktree / 'base.txt').write_text('forged\\n')\n"
            "subprocess.run(['git', '-C', str(worktree), 'add', 'base.txt'], check=True)\n"
            "subprocess.run(['git', '-C', str(worktree), 'commit', '-qm', 'forged'], check=True)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        baseline = git(self.repo, "rev-parse", "HEAD")
        manifest = self.write_manifest(self.task(), [{"engine": "opencode", "model": "minimax-only", "timeout_seconds": 5}])
        self.assertEqual(hardened.command_run(self.args(manifest, fake)), 1)
        outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
        self.assertEqual(outcome["tasks"][0]["failure_class"], "MANIFEST_POLICY_FAILURE")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), baseline)

    def test_marker_cannot_override_failed_objective(self) -> None:
        fake = self.root / "fake-ringer.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "manifest = json.loads(pathlib.Path(sys.argv[sys.argv.index('run') + 1]).read_text())\n"
            "worktree = pathlib.Path(manifest['workdir']) / manifest['tasks'][0]['key']\n"
            "(worktree / 'notes.md').write_text('IMPLEMENTATION_COMPLETE\\n')\n"
            "(worktree / 'result.txt').write_text('done\\n')\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        manifest = self.write_manifest(
            self.task(objective=["/usr/bin/test", "-f", "missing-objective-file"]),
            [{"engine": "opencode", "model": "minimax-only", "timeout_seconds": 5}],
        )
        self.assertEqual(hardened.command_run(self.args(manifest, fake)), 1)
        outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
        attempt = outcome["tasks"][0]["attempts"][0]
        self.assertEqual(outcome["status"], "fail")
        self.assertEqual(attempt["failure_class"], "CHECK_FAILURE")
        self.assertEqual(attempt["objective_checks"][0]["returncode"], 1)

    def test_path_isolation_rejects_checkout_and_symlink_reentry(self) -> None:
        inside = self.repo / "runtime"
        manifest = self.write_manifest(self.task(), [{"engine": "opencode", "model": "minimax-only"}])
        data = json.loads(manifest.read_text())
        data["workdir"] = str(inside)
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(hardened.HardenedSupervisorError, "RUNTIME_PATH_ESCAPE"):
            hardened.command_run(self.args(manifest, self.root / "never-called"))

        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "run-link"
        link.symlink_to(self.repo, target_is_directory=True)
        data["workdir"] = str(link)
        data["runtime_root"] = str(outside / "runtime")
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(hardened.HardenedSupervisorError, "RUNTIME_PATH_ESCAPE"):
            hardened.command_run(self.args(manifest, self.root / "never-called-2"))

    def test_provider_probe_is_real_exact_and_fail_closed_before_worker(self) -> None:
        fake_worker = self.root / "worker-called"
        worker_marker = self.root / "worker-started"
        fake_worker.write_text("#!/bin/sh\n touch %s\n" % worker_marker, encoding="utf-8")
        fake_worker.chmod(0o755)
        bad_probe = {
            "opencode:minimax-only": {
                "kind": "inference",
                "argv": [str(self.provider), "--version", "--model", "minimax-only"],
            }
        }
        manifest = self.write_manifest(
            self.task(),
            [{"engine": "opencode", "model": "minimax-only"}],
            provider_probes=bad_probe,
        )
        self.assertEqual(hardened.command_run(self.args(manifest, fake_worker)), 2)
        outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
        self.assertEqual(outcome["failure_class"], "PREFLIGHT_FAILURE")
        self.assertFalse(worker_marker.exists())

    def test_no_progress_timeout_has_one_terminal_event(self) -> None:
        fake = self.root / "sleep-worker.py"
        fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n", encoding="utf-8")
        fake.chmod(0o755)
        manifest = self.write_manifest(
            self.task(expect=False),
            [{"engine": "opencode", "model": "minimax-only", "timeout_seconds": 5}],
            supervisor_overrides={"no_progress_seconds": 0.15},
        )
        self.assertEqual(hardened.command_run(self.args(manifest, fake)), 1)
        events = [
            json.loads(line)
            for line in (self.artifacts / "supervisor-events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(item["type"] in {"RUN_COMPLETED", "RUN_FAILED"} for item in events), 1)
        self.assertEqual(events[-1]["type"], "RUN_FAILED")
        outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
        self.assertEqual(outcome["failure_class"], "NO_PROGRESS")

    def test_subprocess_sigterm_kills_worker_descendant_and_writes_atomic_outcome(self) -> None:
        pid_file = self.root / "worker.pid"
        fake = self.root / "live-worker.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, subprocess, sys, time\n"
            "manifest = json.loads(pathlib.Path(sys.argv[sys.argv.index('run') + 1]).read_text())\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            "pathlib.Path(os.environ['PID_FILE']).write_text(str(child.pid))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        os.environ["PID_FILE"] = str(pid_file)
        try:
            manifest = self.write_manifest(
                self.task(expect=False),
                [{"engine": "opencode", "model": "minimax-only", "timeout_seconds": 30}],
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "run",
                    str(manifest),
                    "--ringer",
                    str(fake),
                    "--artifact-dir",
                    str(self.artifacts),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.time() + 5
            while time.time() < deadline and not pid_file.exists():
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "worker did not start")
            child_pid = int(pid_file.read_text())
            process.send_signal(signal.SIGTERM)
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, output)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            events = [
                json.loads(line)
                for line in (self.artifacts / "supervisor-events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(sum(item["type"] in {"RUN_COMPLETED", "RUN_FAILED"} for item in events), 1)
            outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
            self.assertEqual(outcome["failure_class"], "SUPERVISOR_SIGNAL")
        finally:
            os.environ.pop("PID_FILE", None)

    def test_subprocess_sigint_writes_one_terminal_outcome(self) -> None:
        fake = self.root / "live-worker-int.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys, time\n"
            "json.loads(pathlib.Path(sys.argv[sys.argv.index('run') + 1]).read_text())\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        manifest = self.write_manifest(
            self.task(expect=False),
            [{"engine": "opencode", "model": "minimax-only", "timeout_seconds": 30}],
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(MODULE_PATH),
                "run",
                str(manifest),
                "--ringer",
                str(fake),
                "--artifact-dir",
                str(self.artifacts),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.time() + 5
            events_path = self.artifacts / "supervisor-events.jsonl"
            while time.time() < deadline and not events_path.exists():
                time.sleep(0.02)
            self.assertTrue(events_path.exists(), "supervisor did not start")
            process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, output)
            events = [
                json.loads(line)
                for line in (self.artifacts / "supervisor-events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(sum(item["type"] in {"RUN_COMPLETED", "RUN_FAILED"} for item in events), 1)
            outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
            self.assertEqual(outcome["failure_class"], "SUPERVISOR_SIGNAL")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_exception_after_run_started_writes_terminal_outcome(self) -> None:
        fake = self.root / "fake-ringer.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "manifest = json.loads(pathlib.Path(sys.argv[sys.argv.index('run') + 1]).read_text())\n"
            "worktree = pathlib.Path(manifest['workdir']) / manifest['tasks'][0]['key']\n"
            "(worktree / 'result.txt').write_text('done\\n')\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        manifest = self.write_manifest(self.task(), [{"engine": "opencode", "model": "minimax-only", "timeout_seconds": 5}])
        data = json.loads(manifest.read_text())
        data["tasks"][0]["objective_checks"] = [{"argv": ["/bin/true"], "unexpected": "field"}]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(hardened.command_run(self.args(manifest, fake)), 1)
        outcome = json.loads((self.artifacts / "supervisor-outcome.json").read_text())
        self.assertEqual(outcome["status"], "fail")
        events = [
            json.loads(line)
            for line in (self.artifacts / "supervisor-events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(events[-1]["type"], "RUN_FAILED")
        self.assertEqual(sum(item["type"] == "RUN_FAILED" for item in events), 1)


if __name__ == "__main__":
    unittest.main()
