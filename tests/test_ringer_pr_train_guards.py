from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

TOOLS = Path(__file__).parents[1] / "tools"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


guards = load("ringer_pr_train_guards")
engine = load("ringer_pr_train_engine")


class GuardError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ).stdout.strip()


class CandidateGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Tests")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def worktree(self, name: str) -> Path:
        path = self.root / name
        git(self.repo, "worktree", "add", "--detach", str(path), "HEAD")
        return path

    def test_exact_allowlist_rejects_extra_file_and_directory(self) -> None:
        tree = self.worktree("candidate")
        (tree / "docs").mkdir()
        (tree / "docs" / "allowed.md").write_text("ok\n", encoding="utf-8")
        guards.assert_candidate(tree, GuardError, ["docs/allowed.md"])
        (tree / "generated-run").mkdir()
        (tree / "generated-run" / "state.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(GuardError, "generated-run/state.json"):
            guards.assert_candidate(tree, GuardError, ["docs/allowed.md"])

    def test_rename_checks_old_and_new_paths(self) -> None:
        tree = self.worktree("rename")
        git(tree, "mv", "base.txt", "renamed.txt")
        with self.assertRaisesRegex(GuardError, "base.txt"):
            guards.assert_candidate(tree, GuardError, ["renamed.txt"])
        guards.assert_candidate(tree, GuardError, ["base.txt", "renamed.txt"])

    def test_delete_and_symlink_are_enforced(self) -> None:
        deleted = self.worktree("delete")
        (deleted / "base.txt").unlink()
        with self.assertRaisesRegex(GuardError, "base.txt"):
            guards.assert_candidate(deleted, GuardError, [])
        guards.assert_candidate(deleted, GuardError, ["base.txt"])

        linked = self.worktree("link")
        os.symlink("base.txt", linked / "alias.txt")
        with self.assertRaisesRegex(GuardError, "changed symlinks"):
            guards.assert_candidate(linked, GuardError, ["alias.txt"])

        deleted_link = self.worktree("deleted-link")
        os.symlink("base.txt", deleted_link / "tracked-link.txt")
        git(deleted_link, "add", "tracked-link.txt")
        git(deleted_link, "commit", "-qm", "track symlink")
        (deleted_link / "tracked-link.txt").unlink()
        with self.assertRaisesRegex(GuardError, "changed symlinks"):
            guards.assert_candidate(deleted_link, GuardError, ["tracked-link.txt"])

    def test_harness_artifact_is_rejected(self) -> None:
        tree = self.worktree("runtime")
        (tree / "attempt.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(GuardError, "RUNTIME_ARTIFACT_CONTAMINATION"):
            guards.assert_candidate(tree, GuardError, ["attempt.json"])

    def test_inner_attempt_artifact_is_relocated(self) -> None:
        candidate = self.root / "source"
        candidate.mkdir()
        inner = candidate / "run-20260822T170108Z-p1618-a001"
        inner.mkdir()
        (inner / "attempt.json").write_text("{}\n", encoding="utf-8")
        destination = self.root / "artifacts"
        moved = guards.relocate_inner_artifacts(candidate, destination, GuardError)
        self.assertEqual(moved, [str(destination / inner.name)])
        self.assertFalse(inner.exists())
        self.assertTrue((destination / inner.name / "attempt.json").is_file())

    def test_policy_rejects_escape_and_supports_subtree(self) -> None:
        self.assertEqual(guards.normalize_owned_path("src/**", GuardError), "src/**")
        for value in ("../escape", "/absolute", ".", "C:/windows", "attempt.json"):
            with self.subTest(value=value), self.assertRaises(GuardError):
                guards.normalize_owned_path(value, GuardError)
        with self.assertRaisesRegex(GuardError, "duplicates"):
            guards.policy_from_task(
                {"allowed_changed_paths": ["src/a.py", "src/a.py"]}, [], GuardError
            )


class EngineEvidenceTests(unittest.TestCase):
    def test_unknown_error_is_engine_runtime_error_evidence(self) -> None:
        parsed = engine.engine_error(
            '{"type":"error","name":"UnknownError",'
            '"message":"Unexpected server error","ref":"err_29dccdf5"}'
        )
        self.assertEqual(parsed["error_ref"], "err_29dccdf5")
        self.assertFalse(parsed["provider_stream_started"])
        self.assertFalse(parsed["provider_request_confirmed"])

    def test_scrubber_removes_credentials_but_preserves_error_ref(self) -> None:
        value = (
            'Authorization: Bearer secret-value '
            '{"access_token":"abc","password":"pw"} err_29dccdf5'
        )
        scrubbed = engine.scrub(value)
        self.assertNotIn("secret-value", scrubbed)
        self.assertNotIn('"abc"', scrubbed)
        self.assertNotIn('"pw"', scrubbed)
        self.assertIn("err_29dccdf5", scrubbed)

    def test_collect_logs_never_copies_auth_database_or_wal_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = root / "runtime-logs"
            runtime.mkdir()
            (runtime / "worker.log").write_text(
                'Authorization: Bearer secret-value err_29dccdf5\n', encoding="utf-8"
            )
            for name in ("auth.json", "state.sqlite", "state.sqlite-wal", "state.sqlite-shm"):
                (runtime / name).write_text("credential-content\n", encoding="utf-8")
            artifacts = root / "log-artifacts"
            copied, combined = engine.collect_logs(runtime, artifacts)
            self.assertEqual(len(copied), 1)
            self.assertIn("[REDACTED]", combined)
            self.assertIn("err_29dccdf5", combined)
            self.assertFalse(
                any(path.endswith(("auth.json", "state.sqlite", "-wal", "-shm")) for path in copied)
            )

    def test_engine_runtime_failure_cannot_retain_patch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = root / "outcome"
            artifacts.mkdir()
            patch = artifacts / "candidate.patch"
            patch.write_text("secret-value\n", encoding="utf-8")
            outcome = artifacts / "supervisor-outcome.json"
            outcome.write_text(
                '{"status":"fail","tasks":[{"status":"fail",'
                '"patch_path":"' + str(patch) + '","patch_sha256":"' + "a" * 64 + '",'
                '"attempts":[{"failure_class":"ENGINE_RUNTIME_ERROR"}]}]}',
                encoding="utf-8",
            )
            engine.enrich_outcome(
                artifacts,
                [],
                'UnknownError Unexpected server error err_29dccdf5',
                lambda path, payload: path.write_text(
                    json.dumps(payload), encoding="utf-8"
                ),
            )
            self.assertFalse(patch.exists())
            self.assertIsNone(
                json.loads(outcome.read_text())["tasks"][0]["patch_path"]
            )

    def test_capability_probe_requires_wrapper_disposable_dir_and_exact_model(self) -> None:
        route = SimpleNamespace(engine="opencode", model="minimax-coding-plan/MiniMax-M3")
        key = "opencode:minimax-coding-plan/minimax-m3"
        probe = {
            "kind": "sandboxed_inference",
            "argv": [
                "/opt/ringer/engines/opencode-sandboxed.sh",
                "{{CAPABILITY_TASK_DIR}}",
                "run",
                "--model",
                route.model,
                "--format",
                "json",
                "CPT_WORKER_CAPABILITY_OK",
            ],
            "expected_output": "CPT_WORKER_CAPABILITY_OK",
        }
        result = engine.validate_probe(
            {"worker_capability_probes": {key: probe}}, route, key, GuardError
        )
        self.assertEqual(result["expected_output"], "CPT_WORKER_CAPABILITY_OK")
        probe["argv"].insert(2, "--no-sandbox")
        with self.assertRaisesRegex(GuardError, "disposable sandboxed task"):
            engine.validate_probe(
                {"worker_capability_probes": {key: probe}}, route, key, GuardError
            )


if __name__ == "__main__":
    unittest.main()
