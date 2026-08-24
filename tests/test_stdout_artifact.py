from __future__ import annotations

import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer  # noqa: E402


class StdoutArtifactUnitTests(unittest.TestCase):
    def test_engine_config_accepts_only_fixed_review_artifact(self) -> None:
        engine = ringer.load_engines(
            {
                "agy": {
                    "bin": "agy",
                    "args_template": ["-p", "{spec}"],
                    "stdout_artifact": "report.md",
                }
            }
        )["agy"]
        self.assertEqual(engine.stdout_artifact, "report.md")
        for invalid in ("../report.md", "/tmp/report.md", "nested/report.md", "other.md"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "exactly 'report.md'"
            ):
                ringer.load_engines(
                    {
                        "agy": {
                            "bin": "agy",
                            "args_template": ["-p", "{spec}"],
                            "stdout_artifact": invalid,
                        }
                    }
                )

    def test_contract_is_fixed_and_secret_aware(self) -> None:
        spec = ringer.append_stdout_artifact_contract("Review this tree.", "report.md")
        self.assertTrue(spec.startswith("Review this tree."))
        self.assertIn("Do not call file-write", spec)
        self.assertIn("Begin the final response with the exact heading `## Findings`", spec)
        self.assertIn("Do not include credentials", spec)

    def test_persistence_rejects_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ringer-stdout-unit-") as raw:
            root = Path(raw)
            taskdir = root / "task"
            taskdir.mkdir()
            outside = root / "outside"
            outside.write_text("unchanged\n", encoding="utf-8")
            (taskdir / "report.md").symlink_to(outside)
            error = ringer.persist_stdout_artifact(
                taskdir,
                "report.md",
                "## Findings\nNO FINDINGS\n",
            )
            self.assertIn("symlink", error or "")
            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")

    def test_persistence_is_private_and_preserves_existing_regular_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ringer-stdout-unit-") as raw:
            taskdir = Path(raw)
            target = taskdir / "report.md"
            self.assertIsNone(
                ringer.persist_stdout_artifact(
                    taskdir,
                    "report.md",
                    "## Findings\nNO FINDINGS\n",
                )
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "## Findings\nNO FINDINGS\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertIsNone(
                ringer.persist_stdout_artifact(taskdir, "report.md", "replacement")
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "## Findings\nNO FINDINGS\n")


class StdoutArtifactIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ringer-stdout-integration-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_fake(
        self,
        script_body: str,
        *,
        check: str = "grep -q '^## Findings' report.md",
        timeout_s: int = 5,
    ) -> tuple[ringer.TaskRuntime, Path]:
        script = self.root / "fake_agy.py"
        script.write_text(script_body, encoding="utf-8")
        config_path = self.root / "config.toml"
        config_path.write_text(
            "\n".join(
                [
                    f'state_dir = "{self.root / "state"}"',
                    "allow_full_access = false",
                    "[eval]",
                    'backend = "jsonl"',
                    f'jsonl_path = "{self.root / "runs.jsonl"}"',
                    "[artifact]",
                    "enabled = false",
                    "[engines.fake-review]",
                    f'bin = "{sys.executable}"',
                    f'args_template = ["{script}", "{{spec}}"]',
                    "sandbox_args = []",
                    "full_access_args = []",
                    'token_regex = ""',
                    'stdout_artifact = "report.md"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        manifest = ringer.Manifest.from_obj(
            {
                "run_name": "stdout-artifact-integration",
                "workdir": str(self.root / "work"),
                "max_parallel": 1,
                "tasks": [
                    {
                        "key": "review",
                        "engine": "fake-review",
                        "spec": "Review without writing files.",
                        "check": check,
                        "expect_files": ["report.md"],
                        "max_attempts": 1,
                        "timeout_s": timeout_s,
                    }
                ],
            }
        )
        runner = ringer.RingerRunner(
            manifest,
            ringer.AppConfig.load(config_path),
            "stdout-artifact-test",
            dashboard_enabled=False,
        )
        runtime = runner.runtimes[0]
        asyncio.run(runner._run_task(runtime))
        return runtime, runtime.taskdir / "report.md"

    def test_successful_final_response_becomes_verified_report(self) -> None:
        runtime, report = self.run_fake(
            "print('## Findings\\nNO FINDINGS')\n"
        )
        self.assertEqual(runtime.status, "pass")
        self.assertEqual(report.read_text(encoding="utf-8"), "## Findings\nNO FINDINGS\n")
        self.assertIn(ringer.STDOUT_ARTIFACT_CONTRACT.rstrip(), runtime.last_worker_command[-1])

    def test_empty_success_fails_closed(self) -> None:
        runtime, report = self.run_fake("pass\n")
        self.assertEqual(runtime.status, "fail")
        self.assertEqual(runtime.final_verdict, "ERROR")
        self.assertFalse(report.exists())

    def test_nonzero_child_does_not_create_report_and_preserves_exit(self) -> None:
        runtime, report = self.run_fake("raise SystemExit(23)\n")
        self.assertEqual(runtime.status, "fail")
        self.assertEqual(runtime.final_verdict, "FAIL")
        self.assertFalse(report.exists())
        log = runtime.log_path.read_text(encoding="utf-8")
        self.assertIn("exited rc=23", log)

    def test_timeout_does_not_create_report(self) -> None:
        runtime, report = self.run_fake("import time\ntime.sleep(30)\n", timeout_s=1)
        self.assertEqual(runtime.status, "fail")
        self.assertEqual(runtime.final_verdict, "TIMEOUT")
        self.assertFalse(report.exists())

    def test_worker_created_symlink_fails_closed(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("unchanged\n", encoding="utf-8")
        runtime, report = self.run_fake(
            "import os\n"
            f"os.symlink({os.fspath(outside)!r}, 'report.md')\n"
            "print('## Findings\\nNO FINDINGS')\n"
        )
        self.assertEqual(runtime.status, "fail")
        self.assertEqual(runtime.final_verdict, "ERROR")
        self.assertTrue(report.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")

    def test_existing_regular_report_is_not_overwritten(self) -> None:
        runtime, report = self.run_fake(
            "from pathlib import Path\n"
            "Path('report.md').write_text('## Findings\\nprovider file\\n', encoding='utf-8')\n"
            "print('## Findings\\nprovider stdout')\n"
        )
        self.assertEqual(runtime.status, "pass")
        self.assertEqual(report.read_text(encoding="utf-8"), "## Findings\nprovider file\n")


if __name__ == "__main__":
    unittest.main()
