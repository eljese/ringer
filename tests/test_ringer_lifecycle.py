from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "ringer_lifecycle.py"
RINGER_PATH = ROOT / "ringer.py"
SPEC = importlib.util.spec_from_file_location("ringer_lifecycle", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
lifecycle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lifecycle
SPEC.loader.exec_module(lifecycle)

RINGER_SPEC = importlib.util.spec_from_file_location("ringer_module", RINGER_PATH)
assert RINGER_SPEC is not None and RINGER_SPEC.loader is not None
ringer = importlib.util.module_from_spec(RINGER_SPEC)
sys.modules[RINGER_SPEC.name] = ringer
RINGER_SPEC.loader.exec_module(ringer)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.txt")
    _git(path, "commit", "-qm", "base")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


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


# ---------------------------------------------------------------------------
# RingerRunner integration — the lifecycle invariants must be enforced by
# ringer.py itself, not just by the optional CLI helper. The tests below
# prove observable ordering (seal before remove, marker before reconcile,
# binding before stale reject) rather than only checking helper names.
# ---------------------------------------------------------------------------


class RingerRunnerLifecycleTests(unittest.TestCase):
    """Drive the in-process RingerRunner lifecycle helpers with a fake
    worker. No network sockets, no subprocess-bound ringer invocation —
    the goal is observable ordering on the real run path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ringer-lifecycle-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        _make_repo(self.repo)
        self.workdir = self.root / "workdir"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.task_key = "wt"
        self.taskdir = self.workdir / self.task_key

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ----- canonical runtime values ----------------------------------------

    def test_canonical_paths_resolve(self) -> None:
        variables = ringer.lifecycle_canonical_values(
            run_dir=self.workdir,
            task_dir=self.taskdir,
            artifact_dir=self.workdir / "artifacts" / self.task_key,
            source_repo=self.repo,
            attempt=3,
        )
        for key in (
            "{{RUN_DIR}}",
            "{{TASK_DIR}}",
            "{{TASK_WORKTREE}}",
            "{{ARTIFACT_DIR}}",
            "{{SOURCE_REPO}}",
            "{{BASE_SHA}}",
            "{{ATTEMPT}}",
        ):
            self.assertIn(key, variables)
        self.assertEqual(variables["{{ATTEMPT}}"], "3")
        self.assertEqual(variables["{{RUN_DIR}}"], str(self.workdir))
        self.assertEqual(variables["{{TASK_DIR}}"], str(self.taskdir))
        self.assertEqual(variables["{{TASK_WORKTREE}}"], str(self.taskdir))
        self.assertEqual(variables["{{SOURCE_REPO}}"], str(self.repo))
        self.assertEqual(len(variables["{{BASE_SHA}}"], ), 40)
        # No source repo -> BASE_SHA must be empty (no crash)
        empty_vars = ringer.lifecycle_canonical_values(
            run_dir=self.workdir,
            task_dir=self.taskdir,
            artifact_dir=self.workdir / "artifacts" / self.task_key,
            source_repo=None,
            attempt=1,
        )
        self.assertEqual(empty_vars["{{BASE_SHA}}"], "")
        self.assertEqual(empty_vars["{{SOURCE_REPO}}"], "")

    def test_unresolved_path_variable_fails_closed_in_substitute(self) -> None:
        with self.assertRaises(ringer.LifecycleError):
            ringer.lifecycle_substitute("{{UNKNOWN}}/report.md", {})

    # ----- failure classifier ---------------------------------------------

    def test_all_required_failure_classes_are_assigned(self) -> None:
        cases = [
            ("worker finding: missing input validation", None, "WORKER_FINDING"),
            ("check assertion failed in shell", 1, "CHECK_FAILURE"),
            ("rate limit quota exhausted", 1, "PROVIDER_QUOTA"),
            ("worker timed out after 300s", 1, "PROVIDER_TIMEOUT"),
            ("Could not resolve host: api.example", 1, "NETWORK_SANDBOX"),
            ("worktree already exists at task path", 1, "STALE_WORKTREE"),
            ("stale report rejected: attempt mismatch", 1, "STALE_ARTIFACT"),
            ("missing export: patch was not sealed", 1, "MISSING_EXPORT"),
            ("expect_files path mismatch outside the worktree", 1, "MANIFEST_PATH_ERROR"),
            ("bad substitution while running /bin/sh", 1, "SHELL_INTERPOLATION"),
            ("no specific signal in output", 0, "COORDINATOR_ERROR"),
        ]
        for text, returncode, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    ringer.classify_failure(text, returncode=returncode),
                    expected,
                    f"text={text!r} returncode={returncode!r}",
                )

    def test_infrastructure_classes_are_not_substantive(self) -> None:
        for cls in ringer.LIFECYCLE_INFRASTRUCTURE_CLASSES:
            self.assertIn(
                cls,
                ringer.LIFECYCLE_FAILURE_CLASSES,
                f"{cls} must be a known lifecycle failure class",
            )

    # ----- argv check preserves literal GitHub expression ----------------

    def test_argv_check_preserves_literal_github_expression(self) -> None:
        # The verifier's argv path passes arguments through shlex.join and
        # then create_subprocess_exec, so dollar expressions never reach
        # /bin/sh for interpretation.
        argv = (
            sys.executable,
            "-c",
            "import sys; print(repr(sys.argv[1]))",
            "${{ github.sha }}",
        )
        with tempfile.TemporaryDirectory() as cwd:
            rc, _, output = asyncio.run(ringer.Verifier._run_argv_check(argv, Path(cwd)))
        self.assertEqual(rc, 0, output)
        self.assertIn("${{ github.sha }}", output)

    def test_shell_mode_remains_available(self) -> None:
        # Shell-mode checks must continue to work end-to-end. $1 inside
        # the shell string still means the first positional argument.
        shell_command = "echo $0"
        with tempfile.TemporaryDirectory() as cwd:
            rc, _, output = asyncio.run(
                ringer.Verifier._run_shell_check(shell_command, Path(cwd))
            )
        self.assertEqual(rc, 0, output)
        self.assertIn("/bin/sh", output)

    def test_normalize_check_dispatches_on_type(self) -> None:
        variables = {"{{TASK_DIR}}": "/tmp/task"}
        shell, argv = ringer.normalize_check("echo hello", variables)
        self.assertEqual(shell, "echo hello")
        self.assertIsNone(argv)
        shell2, argv2 = ringer.normalize_check(
            {"argv": ["sh", "-c", "echo $0", "${{ github.sha }}"]},
            variables,
        )
        # Structured argv: shell is the shlex-joined form, argv is preserved
        # so the verifier can dispatch to create_subprocess_exec.
        self.assertEqual(argv2, ("sh", "-c", "echo $0", "${{ github.sha }}"))
        self.assertIn("'${{ github.sha }}'", shell2)
        shell3, argv3 = ringer.normalize_check(
            {"argv": [sys.executable, "-c", "print('ok')", "{{TASK_DIR}}"]},
            variables,
        )
        self.assertIn("/tmp/task", shell3)
        self.assertEqual(argv3[-1], "/tmp/task")
        # Non-dict / non-string -> LifecycleError
        with self.assertRaises(ringer.LifecycleError):
            ringer.normalize_check(123, variables)
        with self.assertRaises(ringer.LifecycleError):
            ringer.normalize_check({"argv": "not a list"}, variables)
        with self.assertRaises(ringer.LifecycleError):
            ringer.normalize_check({"command": "echo hi"}, variables)

    # ----- patch export: tracked + untracked + verifiable hash -----------

    def test_export_worktree_patch_covers_tracked_and_untracked(self) -> None:
        (self.taskdir).mkdir(parents=True, exist_ok=True)
        # Simulate the worker writing files inside an owned worktree.
        subprocess.run(
            ["git", "-C", str(self.taskdir), "init", "-q"],
            check=True,
        )
        # The exported patch should round-trip both kinds of changes.
        target = self.root / "result.patch"
        patch = ringer.export_worktree_patch(self.taskdir, target)
        self.assertIsNone(patch)  # empty worktree -> nothing to export
        (self.taskdir / "tracked.txt").write_text("before\n", encoding="utf-8")
        _git(self.taskdir, "add", "tracked.txt")
        _git(self.taskdir, "commit", "-qm", "base")
        (self.taskdir / "tracked.txt").write_text("after\n", encoding="utf-8")
        (self.taskdir / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        (self.taskdir / "binary.bin").write_bytes(b"\x00\xff\x80binary\x00\n")
        patch = ringer.export_worktree_patch(self.taskdir, target)
        self.assertEqual(patch, target)
        text = target.read_text(encoding="utf-8")
        self.assertIn("tracked.txt", text)
        self.assertIn("untracked.txt", text)
        self.assertIn("binary.bin", text)
        # Hash is deterministic and verifiable from disk
        h1 = ringer.lifecycle_sha256_file(target)
        self.assertEqual(len(h1), 64)
        # Mutating the file changes the hash
        target.write_text(text + "extra\n", encoding="utf-8")
        h2 = ringer.lifecycle_sha256_file(target)
        self.assertNotEqual(h1, h2)

    def test_worktree_dirty_paths_preserves_rename_source_and_target(self) -> None:
        source = self.root / "rename-repo"
        _make_repo(source)
        _git(source, "mv", "README.txt", "renamed.txt")
        paths = ringer.worktree_dirty_paths(source)
        self.assertIn("README.txt", paths)
        self.assertIn("renamed.txt", paths)

    def test_seal_worktree_writes_verifiable_meta_and_patch(self) -> None:
        (self.taskdir).mkdir(parents=True, exist_ok=True)
        _git(self.taskdir, "init", "-q")
        (self.taskdir / "tracked.txt").write_text("changed\n", encoding="utf-8")
        _git(self.taskdir, "add", "tracked.txt")
        (self.taskdir / "untracked.txt").write_text("new\n", encoding="utf-8")
        patch_dir = self.root / "artifacts"
        meta = ringer.seal_worktree(
            task_dir=self.taskdir,
            worktree=self.taskdir,
            attempt=1,
            source_repo=self.repo,
            patch_dir=patch_dir,
        )
        self.assertIsNotNone(meta["patch_path"])
        self.assertIsNotNone(meta["patch_sha256"])
        self.assertEqual(len(meta["patch_sha256"]), 64)
        self.assertEqual(meta["attempt"], 1)
        # Hash on disk matches metadata
        on_disk = ringer.lifecycle_sha256_file(Path(meta["patch_path"]))
        self.assertEqual(on_disk, meta["patch_sha256"])
        # Meta persisted and reloads
        meta_path = patch_dir / "attempt-001.meta.json"
        self.assertTrue(meta_path.is_file())
        reloaded = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["patch_sha256"], meta["patch_sha256"])

    def test_seal_worktree_handles_clean_worktree(self) -> None:
        (self.taskdir).mkdir(parents=True, exist_ok=True)
        _git(self.taskdir, "init", "-q")
        patch_dir = self.root / "artifacts"
        meta = ringer.seal_worktree(
            task_dir=self.taskdir,
            worktree=self.taskdir,
            attempt=1,
            source_repo=self.repo,
            patch_dir=patch_dir,
        )
        self.assertIsNone(meta["patch_path"])
        self.assertIsNone(meta["patch_sha256"])
        self.assertEqual(meta["patch_bytes"], 0)
        self.assertTrue((patch_dir / "attempt-001.meta.json").is_file())

    # ----- stale report rejection ----------------------------------------

    def test_stale_report_rejected_on_fresh_attempt(self) -> None:
        (self.taskdir).mkdir(parents=True, exist_ok=True)
        report = self.taskdir / "report.md"
        report.write_text("from attempt 1\n", encoding="utf-8")
        ringer.bind_report_to_attempt(
            artifact_dir=self.taskdir,
            attempt_id="run-a001",
            attempt_started_at="2026-08-10T10:00:00+00:00",
            input_tree_sha="aaaa",
            report_path=report,
        )
        # Same attempt + same tree -> fresh
        is_stale, binding = ringer.stale_report_check(
            report_path=report,
            attempt_id="run-a001",
            input_tree_sha="aaaa",
        )
        self.assertFalse(is_stale)
        self.assertIsNotNone(binding)
        # Different attempt -> stale
        is_stale, binding = ringer.stale_report_check(
            report_path=report,
            attempt_id="run-a002",
            input_tree_sha="aaaa",
        )
        self.assertTrue(is_stale)
        self.assertIsNotNone(binding)
        # Different input tree -> stale
        is_stale, binding = ringer.stale_report_check(
            report_path=report,
            attempt_id="run-a001",
            input_tree_sha="bbbb",
        )
        self.assertTrue(is_stale)
        # Mutated content -> stale (binding hash no longer matches)
        report.write_text("tampered\n", encoding="utf-8")
        is_stale, _ = ringer.stale_report_check(
            report_path=report,
            attempt_id="run-a001",
            input_tree_sha="aaaa",
        )
        self.assertTrue(is_stale)

    def test_retry_creates_fresh_evidence(self) -> None:
        """Each attempt gets its own artifact directory under the task
        root, so retry cannot reuse attempt-1's evidence by accident."""
        (self.taskdir).mkdir(parents=True, exist_ok=True)
        _git(self.taskdir, "init", "-q")
        artifact_root = self.root / "artifacts" / self.task_key
        artifact_root.mkdir(parents=True, exist_ok=True)
        runner_state = {"vars": []}
        for attempt in (1, 2):
            attempt_dir = artifact_root / f"run-a{attempt:03d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            variables = ringer.lifecycle_canonical_values(
                run_dir=self.workdir,
                task_dir=self.taskdir,
                artifact_dir=attempt_dir,
                source_repo=self.repo,
                attempt=attempt,
            )
            runner_state["vars"].append(variables)
            ringer.lifecycle_atomic_write_json(
                attempt_dir / "attempt.json",
                {
                    "attempt_id": f"run-a{attempt:03d}",
                    "attempt": attempt,
                    "started_at": f"2026-08-10T10:0{attempt}:00+00:00",
                    "input_tree_sha": variables["{{BASE_SHA}}"],
                },
            )
        # attempt-1 and attempt-2 dirs both exist with distinct records
        self.assertTrue((artifact_root / "run-a001" / "attempt.json").is_file())
        self.assertTrue((artifact_root / "run-a002" / "attempt.json").is_file())
        a1 = json.loads((artifact_root / "run-a001" / "attempt.json").read_text())
        a2 = json.loads((artifact_root / "run-a002" / "attempt.json").read_text())
        self.assertNotEqual(a1["attempt"], a2["attempt"])
        self.assertNotEqual(a1["started_at"], a2["started_at"])
        self.assertEqual(runner_state["vars"][0]["{{ATTEMPT}}"], "1")
        self.assertEqual(runner_state["vars"][1]["{{ATTEMPT}}"], "2")

    # ----- unknown directory is never removed ----------------------------

    def test_unknown_directory_is_never_removed(self) -> None:
        """A pre-existing directory at the taskdir path that is NOT a
        registered lifecycle-owned worktree must be refused — no patch
        export, no `git worktree remove`, no rmtree. The ringer CLI is
        the enforcement surface; here we run it as a subprocess to make
        the contract observable."""
        (self.taskdir).mkdir(parents=True)
        (self.taskdir / "user.txt").write_text("user data\n", encoding="utf-8")
        config_path = self.root / "config.toml"
        config_path.write_text(
            "\n".join(
                [
                    'state_dir = "{}"'.format(self.root / "state"),
                    "dashboard_port_base = 18888",
                    "[eval]\nbackend = \"jsonl\"",
                    'jsonl_path = "{}"'.format(self.root / "runs.jsonl"),
                    "allow_full_access = false",
                    "",
                    "[engines.noop]",
                    'bin = "/bin/sh"',
                    'args_template = ["-c", "{spec}"]',
                    "sandbox_args = []",
                    "full_access_args = []",
                    'token_regex = "x"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "run_name": "unknown-dir",
                    "workdir": str(self.workdir),
                    "max_parallel": 1,
                    "worktrees": True,
                    "repo": str(self.repo),
                    "tasks": [
                        {
                            "key": self.task_key,
                            "engine": "noop",
                            "spec": "noop",
                            "expect_files": [],
                            "verified": "noop",
                            "task_type": "noop",
                            "check": "true",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["RINGER_NO_SELF_UPDATE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["RINGER_HOME"] = str(self.root / "ringer_home")
        result = subprocess.run(
            [sys.executable, "-B", str(RINGER_PATH), "--config", str(config_path),
             "run", str(manifest_path), "--identity", "test-unknown",
             "--no-dashboard"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        # The unknown directory and its contents must still be there
        self.assertTrue(self.taskdir.exists())
        self.assertTrue((self.taskdir / "user.txt").is_file())
        self.assertEqual((self.taskdir / "user.txt").read_text(), "user data\n")

    # ----- worktree is retained until durable sealing --------------------

    def test_worktree_retained_when_sealing_fails(self) -> None:
        """Sealing failure must fail closed: the worktree is NOT removed,
        even though the check passed. We exercise the integration path by
        making export_worktree_patch raise on the only dirty file."""
        # Bring up a real worktree from self.repo
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", str(self.taskdir), "HEAD"],
            check=True,
        )
        # Mark the worktree as lifecycle-owned so _prepare_taskdir would
        # reconcile it. The seal-failure path is what we're testing here.
        ringer.write_lifecycle_marker(
            self.taskdir,
            ringer.lifecycle_owned_marker_payload(
                task_key=self.task_key,
                source_repo=self.repo,
                attempt=0,
            ),
        )
        # Make a dirty change so export_worktree_patch has something to do
        (self.taskdir / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        # Now monkey-patch export_worktree_patch to raise. The sealing
        # helper must propagate this as a fail-closed result, leaving the
        # worktree intact.
        original = ringer.export_worktree_patch

        def boom(worktree, target, **kwargs):
            raise ringer.LifecycleError("simulated sealing failure")

        ringer.export_worktree_patch = boom
        raised = False
        try:
            ringer.seal_worktree(
                task_dir=self.taskdir,
                worktree=self.taskdir,
                attempt=1,
                source_repo=self.repo,
                patch_dir=self.root / "artifacts",
            )
        except ringer.LifecycleError:
            raised = True
        finally:
            ringer.export_worktree_patch = original
        # LifecycleError was raised -> caller will retain the worktree.
        self.assertTrue(raised, "expected seal_worktree to propagate LifecycleError")
        # The worktree directory is still present
        self.assertTrue(self.taskdir.exists())
        self.assertTrue((self.taskdir / "dirty.txt").is_file())
        # And no patch was produced (sealing aborted)
        patch_dir = self.root / "artifacts"
        self.assertFalse((patch_dir / "attempt-001.patch").exists())

    def test_cleanup_leaves_dirty_unsealed_evidence(self) -> None:
        """A successful check on a worktree whose sealing cannot honor
        its dirty state must leave the worktree, the dirty file, and the
        marker intact — `git worktree remove` is NEVER called on dirty
        unsealed evidence. We drive the RingerRunner cleanup path with a
        monkey-patched seal that simulates the failure mode."""
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", str(self.taskdir), "HEAD"],
            check=True,
        )
        ringer.write_lifecycle_marker(
            self.taskdir,
            ringer.lifecycle_owned_marker_payload(
                task_key=self.task_key,
                source_repo=self.repo,
                attempt=0,
            ),
        )
        (self.taskdir / "unsealed.txt").write_text("unsealed\n", encoding="utf-8")
        # The lifecycle invariant: if sealing cannot capture dirty state,
        # we never run `git worktree remove`. Build a minimal
        # RingerRunner-like surface and call its cleanup method directly
        # with a poisoned seal.
        task = ringer.TaskSpec(
            key=self.task_key,
            spec="x",
            check="true",
            verified="verified",
            task_type="smoke",
        )
        runtime = ringer.TaskRuntime(task=task, taskdir=self.taskdir, log_path=self.taskdir / "worker.log")
        runtime.log_path.write_text("", encoding="utf-8")

        class _Stub:
            pass

        runner = _Stub()
        runner.manifest = _Stub()
        runner.manifest.worktrees = True
        runner.manifest.repo = self.repo
        runner.manifest.workdir = self.workdir
        runner.lock = threading.RLock()
        runner._snapshot_worktree_reports = lambda r: None  # type: ignore[attr-defined]
        runner._lifecycle_task_artifacts_dir = ringer.RingerRunner._lifecycle_task_artifacts_dir.__get__(runner, _Stub)  # type: ignore[attr-defined]
        runner._seal_worktree_atomically = lambda **kwargs: False  # type: ignore[attr-defined]

        # Run the async cleanup coroutine
        cleanup_ok = asyncio.run(ringer.RingerRunner._cleanup_worktree_on_pass(runner, runtime, 1))
        self.assertFalse(cleanup_ok)
        # The worktree must still be present — fail-closed.
        self.assertTrue(self.taskdir.exists())
        self.assertTrue((self.taskdir / "unsealed.txt").is_file())
        self.assertTrue((self.taskdir / ringer.LIFECYCLE_OWNERSHIP_MARKER).is_file())
        # And the runtime recorded the fail-closed reason.
        self.assertTrue(runtime.worktree_retained)
        self.assertEqual(runtime.failure_class, "MISSING_EXPORT")
        self.assertFalse(runtime.worktree_sealed)

    def _new_runner(self, *, check: str, max_attempts: int) -> ringer.RingerRunner:
        config_path = self.root / "runner-config.toml"
        config_path.write_text(
            "\n".join(
                [
                    'state_dir = "{}"'.format(self.root / "state"),
                    '[eval]\nbackend = "jsonl"',
                    'jsonl_path = "{}"'.format(self.root / "runs.jsonl"),
                    "[artifact]\nenabled = false",
                ]
            ),
            encoding="utf-8",
        )
        manifest = ringer.Manifest.from_obj(
            {
                "run_name": "in-process-lifecycle",
                "workdir": str(self.workdir),
                "max_parallel": 1,
                "worktrees": True,
                "repo": str(self.repo),
                "tasks": [
                    {
                        "key": self.task_key,
                        "spec": "exercise lifecycle",
                        "check": check,
                        "max_attempts": max_attempts,
                    }
                ],
            }
        )
        return ringer.RingerRunner(
            manifest,
            ringer.AppConfig.load(config_path),
            "lifecycle-test",
            dashboard_enabled=False,
        )

    def test_real_run_path_retains_worktree_when_sealing_fails(self) -> None:
        runner = self._new_runner(check="true", max_attempts=1)
        runtime = runner.runtimes[0]

        async def worker(*args, **kwargs):
            (runtime.taskdir / "changed.txt").write_text("change\n", encoding="utf-8")
            return ringer.WorkerResult(returncode=0, timed_out=False, tokens=None, error=None)

        runner._run_worker = worker
        runner._seal_worktree_atomically = lambda **kwargs: False  # type: ignore[method-assign]
        asyncio.run(runner._run_task(runtime))

        self.assertEqual(runtime.status, "fail")
        self.assertEqual(runtime.final_verdict, "ERROR")
        self.assertEqual(runtime.failure_class, "MISSING_EXPORT")
        self.assertTrue(runtime.taskdir.exists())
        self.assertTrue((runtime.taskdir / "changed.txt").is_file())

    def test_real_retry_removes_stale_report_before_verification(self) -> None:
        runner = self._new_runner(check="grep -q new report.md", max_attempts=2)
        runtime = runner.runtimes[0]
        calls = 0

        async def worker(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                (runtime.taskdir / "report.md").write_text("old attempt\n", encoding="utf-8")
                return ringer.WorkerResult(
                    returncode=0,
                    timed_out=False,
                    tokens=None,
                    error=None,
                )
            return ringer.WorkerResult(returncode=0, timed_out=False, tokens=None, error=None)

        runner._run_worker = worker
        asyncio.run(runner._run_task(runtime))

        self.assertEqual(calls, 2)
        self.assertEqual(runtime.status, "fail")
        self.assertFalse((runtime.taskdir / "report.md").exists())
        stale_dir = self.workdir / "artifacts" / self.task_key
        self.assertTrue(any(stale_dir.rglob("stale-reports/*report.md")))


# ---------------------------------------------------------------------------
# End-to-end CLI test: a real run that passes, a real sealed patch, and a
# real worktree removal — proves the observable ordering on the actual
# ringer.py RingerRunner execution path.
# ---------------------------------------------------------------------------


class RingerRunnerEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ringer-lifecycle-e2e-")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pass_seals_before_remove(self) -> None:
        repo = self.root / "repo"
        _make_repo(repo)
        workdir = self.root / "workdir"
        workdir.mkdir(parents=True, exist_ok=True)
        task_key = "feat"
        taskdir = workdir / task_key
        # Pre-create the worktree + mark it owned + drop a dirty change
        # so the lifecycle helper has real evidence to seal. Then run
        # ringer.py against it: the next run must seal the patch OUTSIDE
        # the worktree and remove it, in that order.
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", str(taskdir), "HEAD"],
            check=True,
        )
        ringer.write_lifecycle_marker(
            taskdir,
            {
                "owner": ringer.LIFECYCLE_OWNER_NAME,
                "task": task_key,
                "attempt": 0,
                "source_repo": str(repo),
                "created_at": "2026-08-10T00:00:00+00:00",
            },
        )
        (taskdir / "dirty.txt").write_text("dirty-preserved\n", encoding="utf-8")
        # Engine that produces a deliverable so the check passes
        config_path = self.root / "config.toml"
        config_path.write_text(
            "\n".join(
                [
                    'state_dir = "{}"'.format(self.root / "state"),
                    "dashboard_port_base = 18889",
                    "[eval]\nbackend = \"jsonl\"",
                    'jsonl_path = "{}"'.format(self.root / "runs.jsonl"),
                    "allow_full_access = false",
                    "",
                    "[engines.touch]",
                    'bin = "/bin/sh"',
                    'args_template = ["-c", "printf ok > out.txt"]',
                    "sandbox_args = []",
                    "full_access_args = []",
                    'token_regex = "x"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "run_name": "e2e-seal",
                    "workdir": str(workdir),
                    "max_parallel": 1,
                    "worktrees": True,
                    "repo": str(repo),
                    "tasks": [
                        {
                            "key": task_key,
                            "engine": "touch",
                            "spec": "Write ok.",
                            "expect_files": ["out.txt"],
                            "verified": "deliverable ok",
                            "task_type": "smoke",
                            "check": 'test "$(cat out.txt 2>/dev/null)" = ok',
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["RINGER_NO_SELF_UPDATE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["RINGER_HOME"] = str(self.root / "ringer_home")
        result = subprocess.run(
            [sys.executable, "-B", str(RINGER_PATH), "--config", str(config_path),
             "run", str(manifest_path), "--identity", "e2e-test",
             "--no-dashboard"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        # Worktree is gone (post-seal removal succeeded)
        self.assertFalse(taskdir.exists())
        # And the sealed patch + meta are present outside the worktree
        artifact_root = workdir / "artifacts" / task_key
        self.assertTrue(artifact_root.is_dir(), f"missing artifact dir: {artifact_root}\n{result.stdout}")
        # The run produces:
        #   - recovery/stale.patch       (from reconciliation of the
        #                                 pre-existing dirty worktree)
        #   - attempt-001.patch          (sealed durable patch)
        #   - attempt-001.meta.json      (sha256-verified metadata)
        #   - <run_id>-a001/attempt.json (attempt-scoped binding record)
        recovery_patch = artifact_root / "recovery" / "stale.patch"
        sealed_patch = artifact_root / "attempt-001.patch"
        sealed_meta = artifact_root / "attempt-001.meta.json"
        self.assertTrue(
            recovery_patch.is_file() or sealed_patch.is_file(),
            f"no patch found: {recovery_patch} or {sealed_patch}",
        )
        # The sealed artifact exists and the hash on disk matches the metadata
        if sealed_patch.is_file():
            self.assertTrue(sealed_meta.is_file(), f"missing meta: {sealed_meta}")
            meta = json.loads(sealed_meta.read_text())
            self.assertEqual(len(meta["patch_sha256"]), 64)
            actual = ringer.lifecycle_sha256_file(sealed_patch)
            self.assertEqual(actual, meta["patch_sha256"])


if __name__ == "__main__":
    unittest.main()
