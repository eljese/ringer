from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).parents[1] / "tools" / "ringer_supervisor_integrated.py"
spec = importlib.util.spec_from_file_location("ringer_supervisor_integrated", MODULE_PATH)
assert spec and spec.loader
integrated = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integrated
spec.loader.exec_module(integrated)


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


class IntegratedSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        init_repo(self.repo)
        self.workdir = self.root / "work"
        self.artifacts = self.root / "artifacts"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def manifest(self, expect_files: list[str]) -> dict:
        return {
            "repo": str(self.repo),
            "workdir": str(self.workdir),
            "supervisor": {},
            "tasks": [
                {
                    "key": "pr-01",
                    "spec": "test",
                    "expect_files": expect_files,
                    "objective_checks": [{"argv": ["git", "diff", "--check"]}],
                }
            ],
        }

    def test_normalizes_logical_attempt_paths_to_task_placeholder(self) -> None:
        logical = self.workdir / "pr-01" / "reports" / "result.json"
        normalized = integrated.normalize_manifest(
            self.manifest([str(logical), "docs/evidence.md"]),
            artifact_root=self.artifacts,
        )
        self.assertEqual(
            normalized["tasks"][0]["expect_files"],
            ["{{TASK_DIR}}/reports/result.json", "{{TASK_DIR}}/docs/evidence.md"],
        )
        self.assertEqual(
            Path(normalized["runtime_root"]),
            (self.artifacts / ".pr-train-runtime").resolve(),
        )

    def test_rejects_absolute_expected_file_outside_logical_task(self) -> None:
        with self.assertRaisesRegex(
            integrated.IntegratedSupervisorError,
            "absolute expect_files",
        ):
            integrated.normalize_manifest(
                self.manifest([str(self.root / "wrong" / "result.json")]),
                artifact_root=self.artifacts,
            )

    def test_credentials_are_seeded_into_the_worker_xdg_data_home(self) -> None:
        source = self.root / "host-auth.json"
        source.write_text('{"token":"secret"}\n', encoding="utf-8")
        manifest = self.manifest([])
        manifest["supervisor"]["credential_seed"] = {
            "source": str(source),
            "required": True,
        }
        normalized = integrated.normalize_manifest(
            manifest,
            artifact_root=self.artifacts,
        )
        layout = integrated._runtime_layout(normalized, self.artifacts)
        seeded = integrated.seed_opencode_credentials(normalized, layout)
        self.assertEqual(seeded, layout.opencode_auth)
        self.assertEqual(
            Path(layout.environment({})["XDG_DATA_HOME"]) / "opencode" / "auth.json",
            seeded,
        )
        self.assertEqual(seeded.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
        self.assertEqual(seeded.stat().st_mode & 0o777, 0o600)

    def test_rejects_credential_destination_that_disagrees_with_xdg(self) -> None:
        manifest = self.manifest([])
        manifest["supervisor"]["credential_seed"] = {
            "destination": str(self.root / "other" / "auth.json")
        }
        with self.assertRaisesRegex(
            integrated.IntegratedSupervisorError,
            "canonical XDG_DATA_HOME",
        ):
            integrated.normalize_manifest(manifest, artifact_root=self.artifacts)

    def test_candidate_patch_rejects_runtime_owned_artifacts(self) -> None:
        worktree = self.root / "attempt"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "--detach", str(worktree), "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        (worktree / "result.txt").write_text("valid\n", encoding="utf-8")
        integrated.assert_candidate_is_source_only(worktree)
        (worktree / "attempt.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            integrated.IntegratedSupervisorError,
            "RUNTIME_ARTIFACT_CONTAMINATION",
        ):
            integrated.assert_candidate_is_source_only(worktree)

    def test_progress_is_telemetry_only_and_atomic(self) -> None:
        path = self.artifacts / "supervisor-progress.json"
        writer = integrated.ProgressWriter(path)
        writer.write("PROVIDER_RUNNING", pid=123)
        first = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(first["telemetry_only"])
        self.assertNotIn("status", first)
        writer.write("TERMINAL", canonical_outcome="outcome.json")
        second = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(second["state"], "TERMINAL")
        self.assertTrue(second["telemetry_only"])

    def test_missing_required_credentials_fails_before_provider(self) -> None:
        manifest = self.manifest([])
        manifest["supervisor"]["credential_seed"] = {
            "source": str(self.root / "missing.json"),
            "required": True,
        }
        normalized = integrated.normalize_manifest(
            manifest,
            artifact_root=self.artifacts,
        )
        layout = integrated._runtime_layout(normalized, self.artifacts)
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            integrated.IntegratedSupervisorError,
            "credential source does not exist",
        ):
            integrated.seed_opencode_credentials(normalized, layout)

    def test_runtime_guards_restore_on_all_command_exit_paths(self) -> None:
        manifest = self.manifest([])
        manifest["supervisor"] = {
            "routes": [{"engine": "opencode", "model": "minimax-coding-plan/MiniMax-M3"}]
        }
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        guarded_names = (
            "export_worktree_patch",
            "emit",
            "_run_objective_checks",
            "_run_worker",
            "classify_failure",
            "preflight",
        )

        def args(artifact_dir: Path) -> argparse.Namespace:
            return argparse.Namespace(
                manifest=manifest_path,
                ringer=self.root / "ringer.py",
                config=None,
                identity="test",
                artifact_dir=artifact_dir,
                cleanup=True,
            )

        originals = {
            name: getattr(
                integrated.hardened.lifecycle
                if name in {"export_worktree_patch", "classify_failure"}
                else integrated.hardened.legacy.EventWriter
                if name == "emit"
                else integrated.hardened.legacy
                if name == "_run_worker"
                else integrated.hardened,
                name,
            )
            for name in guarded_names
        }

        for index, failure in enumerate((RuntimeError("boom"), KeyboardInterrupt())):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(
                    integrated.hardened, "command_run", side_effect=failure
                ), mock.patch.object(
                    integrated.engine, "collect_logs", return_value=([], "")
                ):
                    with self.assertRaises(type(failure)):
                        integrated.command_run(args(self.artifacts / f"case-{index}"))
                self.assertEqual(
                    getattr(integrated.hardened.lifecycle, "export_worktree_patch"),
                    originals["export_worktree_patch"],
                )
                self.assertEqual(
                    integrated.hardened.legacy.EventWriter.emit,
                    originals["emit"],
                )
                self.assertEqual(
                    integrated.hardened._run_objective_checks,
                    originals["_run_objective_checks"],
                )
                self.assertEqual(
                    integrated.hardened.legacy._run_worker,
                    originals["_run_worker"],
                )
                self.assertEqual(
                    integrated.hardened.lifecycle.classify_failure,
                    originals["classify_failure"],
                )
                self.assertEqual(integrated.hardened.preflight, originals["preflight"])

        with mock.patch.object(integrated.hardened, "command_run", return_value=0), mock.patch.object(
            integrated.engine, "collect_logs", side_effect=OSError("cleanup failed")
        ):
            with self.assertRaises(OSError):
                integrated.command_run(args(self.artifacts / "cleanup-failure"))
        self.assertEqual(integrated.hardened.preflight, originals["preflight"])


if __name__ == "__main__":
    unittest.main()
