#!/usr/bin/env python3
"""Credential scrub behaviour for bin/ringer-safe-run."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "ringer-safe-run"
OAUTH_NAMES = (
    "antigravity-oauth-token",
    "oauth_creds.json",
    "google_accounts.json",
    "auth.json",
)

_ISO_SPEC = importlib.util.spec_from_file_location(
    "test_runtime_isolation",
    Path(__file__).resolve().parent / "test_runtime_isolation.py",
)
assert _ISO_SPEC is not None and _ISO_SPEC.loader is not None
iso = importlib.util.module_from_spec(_ISO_SPEC)
_ISO_SPEC.loader.exec_module(iso)
IsolationTestCase = iso.IsolationTestCase


def oauth_paths(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.exists():
        return found
    for path in root.rglob("*"):
        if path.name in OAUTH_NAMES:
            found.append(path)
    return found


RECORD_SEED_SCRIPT = """\
import os
import pathlib
import sys
import time

marker = pathlib.Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else "ok"
home = pathlib.Path(os.environ.get("HOME", ""))
token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
status = "present\\n" if token.is_file() else "absent\\n"
marker.parent.mkdir(parents=True, exist_ok=True)
marker.write_text(status, encoding="utf-8")
pathlib.Path("seeded.txt").write_text(status, encoding="utf-8")
if mode == "sleep":
    time.sleep(20)
if mode == "fail":
    sys.exit(1)
"""


class SafeRunAuthScrubTests(IsolationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runtimes = self.root / "runtimes"
        self.runtimes.mkdir()
        self.allowed = self.root / "safe-manifests"
        self.allowed.mkdir()
        self.workdir = self.root / "work"
        self.fake_home = self.root / "real-home"
        token = self.fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token.parent.mkdir(parents=True)
        token.write_text("fake-token\n", encoding="utf-8")
        (self.fake_home / ".gemini" / "oauth_creds.json").write_text(
            "fake-oauth\n", encoding="utf-8"
        )
        (self.fake_home / ".gemini" / "google_accounts.json").write_text(
            "fake-accounts\n", encoding="utf-8"
        )
        opencode_auth = self.fake_home / ".local" / "share" / "opencode" / "auth.json"
        opencode_auth.parent.mkdir(parents=True)
        opencode_auth.write_text("fake-opencode\n", encoding="utf-8")
        self.record_script = self.root / "record_seed.py"
        self.record_script.write_text(RECORD_SEED_SCRIPT, encoding="utf-8")
        self.marker = self.root / "seeded.txt"

    def write_seed_config(self, *, mode: str = "ok") -> Path:
        path = self.root / f"seed-{mode}.toml"
        path.write_text(
            "\n".join(
                [
                    "allow_full_access = false",
                    "",
                    "[eval]",
                    'backend = "jsonl"',
                    "",
                    "[artifact]",
                    "enabled = true",
                    "",
                    "[engines.probe]",
                    f'bin = "{sys.executable}"',
                    "args_template = [",
                    f'  "{self.record_script}",',
                    f'  "{self.marker}",',
                    f'  "{mode}",',
                    "]",
                    "sandbox_args = []",
                    "full_access_args = []",
                    "",
                    "[engines.mock]",
                    f'bin = "{sys.executable}"',
                    "args_template = [",
                    f'  "{ROOT / "engines" / "mock_worker.py"}",',
                    '  "{spec}",',
                    "]",
                    "sandbox_args = []",
                    "full_access_args = []",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def write_manifest(self, *, fail: bool = False, engine: str = "probe") -> Path:
        spec = "record seed"
        if engine == "mock":
            spec = "MOCK_FAIL" if fail else "MOCK_FILE: hello.txt\nhello\nMOCK_END"
        manifest = self.allowed / ("fail.json" if fail else "ok.json")
        if engine == "mock":
            check = "grep -q hello hello.txt"
            expect = ["hello.txt"]
        else:
            check = "grep -q present seeded.txt"
            expect = ["seeded.txt"]
        task = {
            "key": "hello-task",
            "engine": engine,
            "spec": spec,
            "check": check,
            "expect_files": expect,
        }
        manifest.write_text(
            json.dumps(
                {
                    "run_name": "auth-scrub",
                    "workdir": str(self.workdir),
                    "max_parallel": 1,
                    "tasks": [task],
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def wrapper_env(self, *, mode: str = "ok") -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.fake_home)
        env["RINGER_SAFE_MANIFEST_ROOTS"] = str(self.allowed)
        env["RINGER_SAFE_ALLOWED_ENGINES"] = "probe:mock"
        env["RINGER_SAFE_CONFIG"] = str(self.write_seed_config(mode=mode))
        env["RINGER_SAFE_RUNTIME_PARENT"] = str(self.runtimes)
        env["RINGER_NO_SELF_UPDATE"] = "1"
        env.pop("RINGER_SAFE_AGY_COPY_PATHS", None)
        env.pop("RINGER_SAFE_CLEAN_SUCCESS", None)
        return env

    def run_wrapper(
        self,
        manifest: Path,
        env: dict[str, str],
        extra: list[str] | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(WRAPPER),
                "--manifest",
                str(manifest),
                "--identity",
                "iso-test",
                *(extra or []),
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )

    def parse_runtime(self, output: str) -> Path:
        lines = [
            line
            for line in output.splitlines()
            if line.startswith("RINGER_RUNTIME_ROOT=")
            or "runtime preserved at " in line
            or "runtime retained at " in line
        ]
        self.assertTrue(lines, output)
        last = lines[-1]
        if "=" in last and last.startswith("RINGER_RUNTIME_ROOT="):
            return Path(last.split("=", 1)[1])
        return Path(last.rsplit(" ", 1)[-1])

    def source_wrapper(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        merged["RINGER_SAFE_RUNTIME_PARENT"] = str(self.runtimes)
        return subprocess.run(
            ["bash", "-c", 'source "$1"; shift; "$@"', "bash", str(WRAPPER), *args],
            cwd=str(ROOT),
            env=merged,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )

    def make_owned_runtime(self) -> Path:
        runtime = self.runtimes / "ringer-runtime.testown"
        host = runtime / "host-home" / ".gemini" / "antigravity-cli"
        host.mkdir(parents=True)
        (host / "antigravity-oauth-token").write_text("fake-token\n", encoding="utf-8")
        (runtime / "host-home" / ".gemini" / "oauth_creds.json").write_text(
            "fake-oauth\n", encoding="utf-8"
        )
        engine = runtime / "engine-homes" / "probe" / "run" / "task" / ".gemini"
        engine.mkdir(parents=True)
        (engine / "google_accounts.json").write_text("fake-accounts\n", encoding="utf-8")
        oc = runtime / "host-home" / ".local" / "share" / "opencode"
        oc.mkdir(parents=True)
        (oc / "auth.json").write_text("fake-opencode\n", encoding="utf-8")
        (runtime / ".ringer-safe-runtime").write_text("ringer-safe-run\n", encoding="utf-8")
        (runtime / "logs").mkdir()
        (runtime / "logs" / "worker.log").write_text("log\n", encoding="utf-8")
        (runtime / "artifacts").mkdir()
        (runtime / "artifacts" / "report.md").write_text("ok\n", encoding="utf-8")
        return runtime

    def test_success_scrubs_seeded_auth_from_retained_runtime(self) -> None:
        result = self.run_wrapper(self.write_manifest(), self.wrapper_env(mode="ok"), extra=["--keep-runtime"])
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime = self.parse_runtime(result.stdout)
        self.assertEqual([], oauth_paths(runtime))

    def test_failure_scrubs_auth_but_preserves_diagnostics(self) -> None:
        result = self.run_wrapper(
            self.write_manifest(fail=True, engine="mock"),
            self.wrapper_env(mode="ok"),
            extra=["--keep-runtime"],
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        runtime = self.parse_runtime(result.stdout)
        self.assertEqual([], oauth_paths(runtime))
        self.assertTrue(any(runtime.rglob("*.json")), result.stdout)

    def test_keep_runtime_never_keeps_auth_files(self) -> None:
        result = self.run_wrapper(self.write_manifest(), self.wrapper_env(mode="ok"), extra=["--keep-runtime"])
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime = self.parse_runtime(result.stdout)
        self.assertTrue((runtime / "host-home").is_dir())
        self.assertTrue((runtime / "engine-homes").is_dir())
        self.assertEqual([], oauth_paths(runtime))

    def test_clean_success_preserves_reports_and_non_sensitive_state(self) -> None:
        env = self.wrapper_env(mode="ok")
        report_dir = self.root / "reports"
        env["RINGER_SAFE_REPORT_DIR"] = str(report_dir)
        result = self.run_wrapper(self.write_manifest(), env)
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime = self.parse_runtime(result.stdout)
        self.assertFalse((runtime / "host-home").exists())
        self.assertFalse((runtime / "engine-homes").exists())
        self.assertFalse((runtime / "tmp").exists())
        self.assertFalse((runtime / "work").exists())
        self.assertTrue((runtime / "artifacts").exists())
        self.assertTrue(any(report_dir.iterdir()))
        self.assertTrue((runtime / "logs").exists())
        self.assertTrue((runtime / "runs.jsonl").exists())
        self.assertEqual([], oauth_paths(runtime))
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "present\n")

    def test_default_tmpdir_parent_still_scrubs_auth(self) -> None:
        env = self.wrapper_env(mode="ok")
        env.pop("RINGER_SAFE_RUNTIME_PARENT", None)
        env["TMPDIR"] = str(self.runtimes)
        result = self.run_wrapper(self.write_manifest(), env, extra=["--keep-runtime"])
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime = self.parse_runtime(result.stdout)
        self.assertEqual([], oauth_paths(runtime))
        self.assertTrue((runtime / "host-home").is_dir())

    def test_exit_trap_scrubs_auth_after_sigterm(self) -> None:
        env = self.wrapper_env(mode="sleep")
        proc = subprocess.Popen(
            [
                str(WRAPPER),
                "--manifest",
                str(self.write_manifest()),
                "--identity",
                "iso-test",
                "--keep-runtime",
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.marker.is_file():
                break
            if proc.poll() is not None:
                out, _ = proc.communicate()
                self.fail(f"wrapper exited before seed marker: {out}")
            time.sleep(0.05)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
            self.fail("worker never wrote seeded.txt")
        os.killpg(proc.pid, signal.SIGTERM)
        proc.communicate(timeout=15)
        found = list(self.runtimes.glob("ringer-runtime.*"))
        self.assertTrue(found)
        self.assertEqual([], oauth_paths(found[0]))

    def test_exit_trap_scrubs_auth_after_sigint(self) -> None:
        env = self.wrapper_env(mode="sleep")
        proc = subprocess.Popen(
            [
                str(WRAPPER),
                "--manifest",
                str(self.write_manifest()),
                "--identity",
                "iso-test",
                "--keep-runtime",
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.marker.is_file():
                break
            if proc.poll() is not None:
                out, _ = proc.communicate()
                self.fail(f"wrapper exited before seed marker: {out}")
            time.sleep(0.05)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
            self.fail("worker never wrote seeded.txt")
        os.kill(proc.pid, signal.SIGINT)
        proc.communicate(timeout=15)
        found = list(self.runtimes.glob("ringer-runtime.*"))
        self.assertTrue(found)
        self.assertEqual([], oauth_paths(found[0]))

    def test_cleanup_refuses_empty_runtime_path(self) -> None:
        result = self.source_wrapper("assert_owned_runtime", "")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("CLEANUP_FAILURE", result.stdout)

    def test_cleanup_refuses_runtime_outside_owned_parent(self) -> None:
        outside = self.root / "outside" / "ringer-runtime.evil"
        outside.mkdir(parents=True)
        (outside / ".ringer-safe-runtime").write_text("ringer-safe-run\n", encoding="utf-8")
        secret = outside / "host-home" / ".gemini"
        secret.mkdir(parents=True)
        token = secret / "oauth_creds.json"
        token.write_text("fake-oauth\n", encoding="utf-8")
        result = self.source_wrapper("scrub_seeded_auth", str(outside))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("CLEANUP_FAILURE", result.stdout)
        self.assertTrue(token.is_file())

    def test_cleanup_does_not_follow_symlinks(self) -> None:
        runtime = self.make_owned_runtime()
        outside = self.root / "outside-secret.json"
        outside.write_text("keep-me\n", encoding="utf-8")
        link = runtime / "host-home" / ".gemini" / "oauth_creds.json"
        if link.exists():
            link.unlink()
        link.symlink_to(outside)
        result = self.source_wrapper("scrub_seeded_auth", str(runtime))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(outside.is_file())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep-me\n")
        self.assertFalse(link.exists())

    def test_cleanup_does_not_follow_directory_symlinks(self) -> None:
        runtime = self.make_owned_runtime()
        outside = self.root / "outside-gemini"
        outside.mkdir()
        secret = outside / "oauth_creds.json"
        secret.write_text("keep-me\n", encoding="utf-8")
        gemini = runtime / "host-home" / ".gemini"
        shutil.rmtree(gemini)
        gemini.symlink_to(outside)
        result = self.source_wrapper("scrub_seeded_auth", str(runtime))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("CLEANUP_FAILURE", result.stdout)
        self.assertTrue(secret.is_file())
        self.assertEqual(secret.read_text(encoding="utf-8"), "keep-me\n")

    def test_list_auth_rels_keeps_opencode_auth_when_env_repeats_defaults(self) -> None:
        env = self.wrapper_env()
        env["RINGER_SAFE_AGY_COPY_PATHS"] = (
            ".gemini/antigravity-cli/antigravity-oauth-token:"
            ".gemini/oauth_creds.json:"
            ".gemini/google_accounts.json:"
            ".local/share/opencode/auth.json"
        )
        result = self.source_wrapper("list_auth_rels", env=env)
        self.assertIn(".local/share/opencode/auth.json", result.stdout.splitlines())

    def test_cleanup_is_idempotent(self) -> None:
        runtime = self.make_owned_runtime()
        first = self.source_wrapper("scrub_seeded_auth", str(runtime))
        self.assertEqual(first.returncode, 0, first.stdout)
        second = self.source_wrapper("scrub_seeded_auth", str(runtime))
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertEqual([], oauth_paths(runtime))
        self.assertTrue((runtime / "logs" / "worker.log").is_file())

    def test_no_oauth_filename_remains_under_runtime_after_success(self) -> None:
        result = self.run_wrapper(self.write_manifest(), self.wrapper_env(mode="ok"))
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime = self.parse_runtime(result.stdout)
        self.assertEqual([], oauth_paths(runtime))

    def test_no_oauth_filename_remains_under_runtime_after_failure(self) -> None:
        result = self.run_wrapper(
            self.write_manifest(fail=True, engine="mock"),
            self.wrapper_env(mode="ok"),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        runtime = self.parse_runtime(result.stdout)
        self.assertEqual([], oauth_paths(runtime))


if __name__ == "__main__":
    unittest.main(verbosity=2)
