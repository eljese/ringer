#!/usr/bin/env python3
"""Runtime-root isolation, engine env, safe-run policy, and preflight."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer  # noqa: E402

_VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_safe_manifest", ROOT / "tools" / "validate_safe_manifest.py"
)
assert _VALIDATOR_SPEC is not None and _VALIDATOR_SPEC.loader is not None
validator = importlib.util.module_from_spec(_VALIDATOR_SPEC)
_VALIDATOR_SPEC.loader.exec_module(validator)


DUMP_ENV_SCRIPT = """\
import json, os, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(
    json.dumps({
        "HOME": os.environ.get("HOME", ""),
        "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME", ""),
        "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", ""),
        "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME", ""),
        "TMPDIR": os.environ.get("TMPDIR", ""),
    }),
    encoding="utf-8",
)
"""


class IsolationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ringer-iso-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old_env = os.environ.copy()
        self.addCleanup(self.restore_env)
        os.environ.pop("RINGER_RUNTIME_ROOT", None)
        os.environ.pop("RINGER_HOME", None)
        os.environ.pop("RINGER_CONFIG", None)
        os.environ.pop("RINGER_SAFE_ENFORCE", None)
        os.environ.pop("RINGER_SAFE_AGY_COPY_PATHS", None)
        os.environ.pop("RINGER_SAFE_SEED_HOME", None)
        os.environ.pop("RINGER_SAFE_ALLOWED_ENGINES", None)
        os.environ.pop("RINGER_SAFE_MAX_PARALLEL", None)

    def empty_config(self) -> Path:
        path = self.root / "empty.toml"
        path.write_text("allow_full_access = false\n", encoding="utf-8")
        return path

    def restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)

    def write_legacy_config(self, path: Path) -> Path:
        legacy = self.root / "legacy-home"
        path.write_text(
            "\n".join(
                [
                    f'state_dir = "{legacy / "state"}"',
                    "allow_full_access = false",
                    "",
                    "[eval]",
                    'backend = "jsonl"',
                    f'jsonl_path = "{legacy / "runs.jsonl"}"',
                    "",
                    "[artifact]",
                    "enabled = true",
                    f'out = "{legacy / "artifacts" / "{run_id}.html"}"',
                    f'report_out = "{legacy / "artifacts" / "{run_id}-report.html"}"',
                    f'index_out = "{legacy / "artifacts" / "index.html"}"',
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
        return legacy

    def write_probe_config(self, path: Path, dump_script: Path) -> None:
        path.write_text(
            "\n".join(
                [
                    "allow_full_access = false",
                    "",
                    "[eval]",
                    'backend = "jsonl"',
                    "",
                    "[artifact]",
                    "enabled = false",
                    "",
                    "[engines.probe]",
                    f'bin = "{sys.executable}"',
                    "args_template = [",
                    f'  "{dump_script}",',
                    '  "{taskdir}/env.json",',
                    "]",
                    "sandbox_args = []",
                    "full_access_args = []",
                    "",
                    "[engines.probe.env]",
                    'HOME = "{runtime_root}/engine-homes/probe/{run_id}/{task_key}"',
                    'XDG_CONFIG_HOME = "{runtime_root}/engine-homes/probe/{run_id}/{task_key}/.config"',
                    'TMPDIR = "{runtime_root}/tmp/{run_id}/{task_key}"',
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

    def write_dump_script(self) -> Path:
        script = self.root / "dump_env.py"
        script.write_text(DUMP_ENV_SCRIPT, encoding="utf-8")
        return script

    def run_cli(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        merged["RINGER_NO_SELF_UPDATE"] = "1"
        merged["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", str(ROOT / "ringer.py"), *args],
            cwd=str(ROOT),
            env=merged,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )


class RuntimeRootTests(IsolationTestCase):
    def test_cli_runtime_root_overrides_environment(self) -> None:
        config = self.root / "config.toml"
        self.write_legacy_config(config)
        cli_root = self.root / "cli-runtime"
        env_root = self.root / "env-runtime"
        env_root.mkdir()
        result = self.run_cli(
            "--config",
            str(config),
            "--runtime-root",
            str(cli_root),
            "preflight",
            env={"RINGER_RUNTIME_ROOT": str(env_root)},
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"runtime_root={cli_root.resolve()}", result.stdout)

    def test_environment_runtime_root_overrides_legacy_config(self) -> None:
        config = self.root / "config.toml"
        legacy = self.write_legacy_config(config)
        runtime = self.root / "env-runtime"
        loaded = ringer.AppConfig.load(config, environ={"RINGER_RUNTIME_ROOT": str(runtime)})
        self.assertEqual(loaded.runtime_root, runtime.resolve())
        self.assertTrue(ringer.path_is_contained(loaded.state_dir, runtime.resolve()))
        self.assertFalse(ringer.path_is_contained(legacy, runtime.resolve()))

    def test_runtime_root_overrides_legacy_state_dir(self) -> None:
        config = self.root / "config.toml"
        self.write_legacy_config(config)
        runtime = self.root / "rr"
        loaded = ringer.AppConfig.load(config, runtime_root=runtime)
        self.assertEqual(loaded.state_dir, runtime.resolve())

    def test_runtime_root_rewrites_home_layout_even_when_contained(self) -> None:
        runtime = self.root / "rr"
        host_home = runtime / "host-home"
        host_home.mkdir(parents=True)
        os.environ["HOME"] = str(host_home)
        loaded = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime)
        self.assertEqual(loaded.state_dir, runtime.resolve())
        self.assertEqual(loaded.eval.jsonl_path, runtime.resolve() / "runs.jsonl")
        self.assertFalse(
            str(loaded.state_dir).startswith(str(host_home.resolve())),
        )

    def test_runtime_root_overrides_legacy_eval_jsonl_path(self) -> None:
        config = self.root / "config.toml"
        self.write_legacy_config(config)
        runtime = self.root / "rr"
        loaded = ringer.AppConfig.load(config, runtime_root=runtime)
        self.assertEqual(loaded.eval.jsonl_path, runtime.resolve() / "runs.jsonl")

    def test_runtime_root_overrides_legacy_artifact_paths(self) -> None:
        config = self.root / "config.toml"
        self.write_legacy_config(config)
        runtime = self.root / "rr"
        loaded = ringer.AppConfig.load(config, runtime_root=runtime)
        self.assertTrue(
            ringer.artifact_template_is_contained(loaded.artifact.out_template, runtime.resolve())
        )
        self.assertTrue(ringer.path_is_contained(loaded.artifact.index_out, runtime.resolve()))

    def test_active_runs_uses_runtime_root(self) -> None:
        runtime = self.root / "rr"
        runtime.mkdir()
        os.environ["RINGER_RUNTIME_ROOT"] = str(runtime)
        os.environ["RINGER_HOME"] = str(self.root / "legacy-ringer-home")
        self.assertEqual(ringer.active_runs_path(), runtime.resolve() / "active-runs.json")

    def test_hud_log_does_not_escape_runtime_root(self) -> None:
        config = self.root / "config.toml"
        self.write_legacy_config(config)
        runtime = self.root / "rr"
        loaded = ringer.AppConfig.load(config, runtime_root=runtime)
        self.assertTrue(ringer.path_is_contained(loaded.state_dir / "hud.log", runtime.resolve()))

    def test_self_update_state_does_not_escape_runtime_root(self) -> None:
        config = self.root / "config.toml"
        self.write_legacy_config(config)
        runtime = self.root / "rr"
        loaded = ringer.AppConfig.load(config, runtime_root=runtime)
        path = ringer.self_update_state_path(loaded.state_dir)
        self.assertTrue(ringer.path_is_contained(path, runtime.resolve()))

    def test_startup_self_update_uses_runtime_root_from_argv(self) -> None:
        config = self.root / "config.toml"
        config.write_text(
            "\n".join(
                [
                    "allow_full_access = false",
                    "[update]",
                    "auto = true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        runtime = self.root / "rr"
        home = self.root / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        os.environ.pop("RINGER_HOME", None)

        def failing_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(["git"], 1, "", "fail")

        result = ringer.maybe_self_update(
            [
                str(ROOT / "ringer.py"),
                "--config",
                str(config),
                "--runtime-root",
                str(runtime),
                "lint",
                "manifest.json",
            ],
            repo_dir=ROOT,
            runner=failing_git,
            execve=lambda *_args: None,
            environ={
                key: value
                for key, value in os.environ.items()
                if key not in {"RINGER_NO_SELF_UPDATE", "RINGER_SELF_UPDATED"}
            },
        )
        self.assertFalse((home / ".ringer" / "self-update.json").exists())
        self.assertTrue(
            (runtime.resolve() / "self-update.json").exists(),
            msg=repr(result),
        )

    def test_legacy_behavior_remains_when_runtime_root_is_unset(self) -> None:
        config = self.root / "config.toml"
        legacy = self.write_legacy_config(config)
        loaded = ringer.AppConfig.load(config)
        self.assertIsNone(loaded.runtime_root)
        self.assertEqual(loaded.state_dir, (legacy / "state").resolve())
        self.assertEqual(loaded.eval.jsonl_path, (legacy / "runs.jsonl").resolve())

    def test_apply_runtime_root_forces_allow_full_access_false(self) -> None:
        config = self.root / "open.toml"
        config.write_text("allow_full_access = true\n", encoding="utf-8")
        loaded = ringer.AppConfig.load(config, runtime_root=self.root / "rr")
        self.assertFalse(loaded.allow_full_access)

    def test_runtime_root_rejects_filesystem_root(self) -> None:
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.canonicalize_runtime_root("/")
        self.assertEqual(caught.exception.classification, "RUNTIME_PATH_ESCAPE")


class EngineEnvTests(IsolationTestCase):
    def test_engine_env_placeholders_expand_without_shell(self) -> None:
        expanded = ringer.expand_engine_env_value(
            "{runtime_root}/engine-homes/agy/{run_id}/{task_key}",
            {
                "runtime_root": "/rt",
                "run_id": "run-1",
                "task_key": "task-a",
                "taskdir": "/work/task-a",
            },
        )
        self.assertEqual(expanded, "/rt/engine-homes/agy/run-1/task-a")

    def test_engine_env_rejects_shell_interpolation(self) -> None:
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.expand_engine_env_value("$HOME/{taskdir}", {"taskdir": "/x", "runtime_root": "/r", "run_id": "r", "task_key": "k"})
        self.assertEqual(caught.exception.classification, "PREFLIGHT_FAILURE")

    def test_engine_home_escape_is_rejected(self) -> None:
        runtime = self.root / "rr"
        runtime.mkdir()
        config = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime)
        engine = ringer.EngineConfig(
            name="agy",
            bin="agy",
            args_template=("-p", "{spec}"),
            full_access_args=(),
            sandbox_args=(),
            env=(("HOME", "/tmp/ringer-escape-home"),),
        )
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.build_worker_env(
                engine,
                config=config,
                run_id="run-1",
                task_key="task-a",
                taskdir=self.root / "task-a",
            )
        self.assertEqual(caught.exception.classification, "RUNTIME_PATH_ESCAPE")

    def test_agy_receives_isolated_home_and_xdg(self) -> None:
        dump = self.write_dump_script()
        config_path = self.root / "config.toml"
        self.write_probe_config(config_path, dump)
        runtime = self.root / "rr"
        workdir = self.root / "work"
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_name": "iso-home",
                    "workdir": str(workdir),
                    "max_parallel": 1,
                    "tasks": [
                        {
                            "key": "task-a",
                            "engine": "probe",
                            "spec": "dump env",
                            "check": "test -f env.json",
                            "expect_files": ["env.json"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = self.run_cli(
            "--config",
            str(config_path),
            "--runtime-root",
            str(runtime),
            "run",
            str(manifest),
            "--identity",
            "iso-test",
            "--no-dashboard",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = json.loads((workdir / "task-a" / "env.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["HOME"].startswith(str(runtime.resolve() / "engine-homes")))
        self.assertTrue(payload["XDG_CONFIG_HOME"].startswith(payload["HOME"]))
        self.assertTrue(payload["TMPDIR"].startswith(str(runtime.resolve() / "tmp")))

    def test_parallel_tasks_do_not_share_engine_home(self) -> None:
        dump = self.write_dump_script()
        config_path = self.root / "config.toml"
        self.write_probe_config(config_path, dump)
        runtime = self.root / "rr"
        workdir = self.root / "work"
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_name": "iso-parallel",
                    "workdir": str(workdir),
                    "max_parallel": 2,
                    "tasks": [
                        {
                            "key": "alpha",
                            "engine": "probe",
                            "spec": "dump env",
                            "check": "test -f env.json",
                            "expect_files": ["env.json"],
                        },
                        {
                            "key": "bravo",
                            "engine": "probe",
                            "spec": "dump env",
                            "check": "test -f env.json",
                            "expect_files": ["env.json"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = self.run_cli(
            "--config",
            str(config_path),
            "--runtime-root",
            str(runtime),
            "run",
            str(manifest),
            "--identity",
            "iso-test",
            "--no-dashboard",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        alpha = json.loads((workdir / "alpha" / "env.json").read_text(encoding="utf-8"))
        bravo = json.loads((workdir / "bravo" / "env.json").read_text(encoding="utf-8"))
        self.assertNotEqual(alpha["HOME"], bravo["HOME"])

    def test_default_agy_copy_paths_use_cli_oauth_token(self) -> None:
        self.assertIn(
            ".gemini/antigravity-cli/antigravity-oauth-token",
            ringer.DEFAULT_SAFE_AGY_COPY_PATHS,
        )
        self.assertNotIn(
            ".gemini/antigravity-oauth-token",
            ringer.DEFAULT_SAFE_AGY_COPY_PATHS,
        )

    def test_isolated_worker_env_preserves_xdg_runtime_dir(self) -> None:
        overlay = dict(
            ringer.default_isolated_engine_env(
                engine_name="agy",
                runtime_root=Path("/rt"),
                run_id="run-1",
                task_key="task-a",
            )
        )
        self.assertNotIn("XDG_RUNTIME_DIR", overlay)
        runtime = self.root / "rr"
        config = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime)
        engine = ringer.EngineConfig(
            name="mock",
            bin=sys.executable,
            args_template=("{spec}",),
            full_access_args=(),
            sandbox_args=(),
        )
        env = ringer.build_worker_env(
            engine,
            config=config,
            run_id="run-1",
            task_key="task-a",
            taskdir=self.root / "task-a",
            inherited={"PATH": "/usr/bin", "XDG_RUNTIME_DIR": "/run/user/3000", "HOME": "/tmp/x"},
        )
        assert env is not None
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/3000")

    def test_isolated_worker_env_includes_agy_scratch_dir(self) -> None:
        overlay = dict(
            ringer.default_isolated_engine_env(
                engine_name="agy",
                runtime_root=Path("/rt"),
                run_id="run-1",
                task_key="task-a",
            )
        )
        self.assertEqual(
            overlay["AGY_RINGER_SCRATCH_DIR"],
            "/rt/engine-homes/agy/run-1/task-a/.gemini/antigravity-cli/scratch",
        )

    def test_isolated_worker_env_drops_ld_preload(self) -> None:
        runtime = self.root / "rr"
        config = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime)
        engine = ringer.EngineConfig(
            name="mock",
            bin=sys.executable,
            args_template=("{spec}",),
            full_access_args=(),
            sandbox_args=(),
        )
        env = ringer.build_worker_env(
            engine,
            config=config,
            run_id="run-1",
            task_key="task-a",
            taskdir=self.root / "task-a",
            inherited={"PATH": "/usr/bin", "LD_PRELOAD": "evil.so", "HOME": "/tmp/x"},
        )
        assert env is not None
        self.assertNotIn("LD_PRELOAD", env)

    def test_isolated_worker_env_preserves_path(self) -> None:
        runtime = self.root / "rr"
        config = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime)
        engine = ringer.EngineConfig(
            name="mock",
            bin=sys.executable,
            args_template=("{spec}",),
            full_access_args=(),
            sandbox_args=(),
        )
        env = ringer.build_worker_env(
            engine,
            config=config,
            run_id="run-1",
            task_key="task-a",
            taskdir=self.root / "task-a",
            inherited={"PATH": "/custom/bin", "HOME": "/tmp/x"},
        )
        assert env is not None
        self.assertEqual(env["PATH"], "/custom/bin")

    def test_engines_without_env_table_still_get_isolated_home(self) -> None:
        runtime = self.root / "rr"
        config = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime)
        engine = ringer.EngineConfig(
            name="mock",
            bin=sys.executable,
            args_template=("{spec}",),
            full_access_args=(),
            sandbox_args=(),
        )
        env = ringer.build_worker_env(
            engine,
            config=config,
            run_id="run-1",
            task_key="task-a",
            taskdir=self.root / "task-a",
        )
        assert env is not None
        self.assertTrue(
            env["HOME"].startswith(str(runtime.resolve() / "engine-homes" / "mock"))
        )

    def test_worker_home_receives_seeded_agy_file(self) -> None:
        seed = self.root / "seed-home"
        rel = Path(".agy-seed") / "token.txt"
        src = seed / rel
        src.parent.mkdir(parents=True)
        src.write_text("seeded\n", encoding="utf-8")
        os.environ["RINGER_SAFE_AGY_COPY_PATHS"] = str(rel)
        os.environ["RINGER_SAFE_SEED_HOME"] = str(seed)
        runtime = self.root / "rr"
        config = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime)
        engine = ringer.EngineConfig(
            name="agy",
            bin="agy",
            args_template=("--sandbox", "-p", "{spec}"),
            full_access_args=(),
            sandbox_args=(),
        )
        env = ringer.build_worker_env(
            engine,
            config=config,
            run_id="run-1",
            task_key="task-a",
            taskdir=self.root / "task-a",
        )
        assert env is not None
        self.assertEqual((Path(env["HOME"]) / rel).read_text(encoding="utf-8"), "seeded\n")


class PreflightTests(IsolationTestCase):
    def test_preflight_classifies_blocked_loopback_as_network_sandbox(self) -> None:
        class FakeSock:
            def bind(self, _addr: object) -> None:
                raise OSError("Operation not permitted")

            def close(self) -> None:
                return None

        original = ringer.socket.socket
        ringer.socket.socket = lambda *_args, **_kwargs: FakeSock()  # type: ignore[method-assign]
        self.addCleanup(lambda: setattr(ringer.socket, "socket", original))
        runtime = self.root / "rr"
        config = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime)
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.isolation_preflight(config)
        self.assertEqual(caught.exception.classification, "NETWORK_SANDBOX")
        self.assertIn("bin/ringer-safe-run", caught.exception.detail)

    def test_preflight_stops_before_worker_invocation(self) -> None:
        marker = self.root / "worker-ran"
        config_path = self.root / "config.toml"
        config_path.write_text(
            "\n".join(
                [
                    "allow_full_access = false",
                    "[engines.marker]",
                    f'bin = "{sys.executable}"',
                    "args_template = [",
                    '  "-c",',
                    f'  "from pathlib import Path; Path({str(marker)!r}).write_text(\'ran\')",',
                    "]",
                    "sandbox_args = []",
                    "full_access_args = []",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        workdir = self.root / "work"
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_name": "no-spawn",
                    "workdir": str(workdir),
                    "max_parallel": 1,
                    "tasks": [
                        {
                            "key": "mark",
                            "engine": "marker",
                            "spec": "should not run",
                            "check": "true",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        inside_repo = ROOT / ".runtime-isolation-should-not-exist"
        self.addCleanup(
            lambda: inside_repo.exists() and inside_repo.rmdir()
        )
        result = self.run_cli(
            "--config",
            str(config_path),
            "--runtime-root",
            str(inside_repo),
            "run",
            str(manifest),
            "--identity",
            "iso-test",
            "--no-dashboard",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("RUNTIME_PATH_ESCAPE", result.stdout)
        self.assertFalse(marker.exists())

    def test_unwritable_runtime_root_is_preflight_failure(self) -> None:
        runtime = self.root / "rr"
        config = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime)
        runtime.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(lambda: runtime.chmod(stat.S_IRWXU))
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.isolation_preflight(config)
        self.assertEqual(caught.exception.classification, "PREFLIGHT_FAILURE")

    def test_inspect_worker_command_rejects_extra_add_dir(self) -> None:
        taskdir = self.root / "task-a"
        taskdir.mkdir()
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.inspect_worker_command_flags(
                ["agy", "--add-dir", str(taskdir), "--add-dir", "/", "--sandbox", "-p", "x"],
                taskdir=taskdir,
                engine_name="agy",
            )
        self.assertIn("--add-dir", caught.exception.detail)

    def test_build_worker_command_expands_workdir_and_taskdir(self) -> None:
        workdir = self.root / "review-tree"
        taskdir = workdir / "review"
        taskdir.mkdir(parents=True)
        engine = ringer.EngineConfig(
            name="agy",
            bin="agy",
            args_template=("--add-dir", "{workdir}", "--add-dir", "{taskdir}", "--sandbox", "-p", "{spec}"),
            sandbox_args=(),
            full_access_args=(),
        )
        command = ringer.build_worker_command(
            engine,
            taskdir=taskdir,
            workdir=workdir,
            spec="review",
            full_access=False,
        )
        self.assertEqual(
            command,
            ["agy", "--add-dir", str(workdir), "--add-dir", str(taskdir), "--sandbox", "-p", "review"],
        )

    def test_inspect_worker_command_allows_workdir_add_dir(self) -> None:
        workdir = self.root / "review-tree"
        taskdir = workdir / "review"
        taskdir.mkdir(parents=True)
        ringer.inspect_worker_command_flags(
            [
                "agy",
                "--add-dir",
                str(workdir),
                "--add-dir",
                str(taskdir),
                "--sandbox",
                "-p",
                "x",
            ],
            taskdir=taskdir,
            workdir=workdir,
            engine_name="agy",
        )

    def test_inspect_worker_command_rejects_sibling_add_dir(self) -> None:
        workdir = self.root / "review-tree"
        taskdir = workdir / "review"
        sibling = self.root / "other"
        taskdir.mkdir(parents=True)
        sibling.mkdir()
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.inspect_worker_command_flags(
                ["agy", "--add-dir", str(sibling), "--sandbox", "-p", "x"],
                taskdir=taskdir,
                workdir=workdir,
                engine_name="agy",
            )
        self.assertIn("--add-dir", caught.exception.detail)

    def test_inspect_worker_command_requires_sandbox_for_agy(self) -> None:
        taskdir = self.root / "task-a"
        taskdir.mkdir()
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.inspect_worker_command_flags(
                ["agy", "--add-dir", str(taskdir), "-p", "x"],
                taskdir=taskdir,
                engine_name="agy",
            )
        self.assertIn("sandbox", caught.exception.detail.lower())

    def test_inspect_worker_command_requires_agy_argv0(self) -> None:
        taskdir = self.root / "task-a"
        taskdir.mkdir()
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.inspect_worker_command_flags(
                ["/bin/bash", "--sandbox", "--add-dir", str(taskdir), "-p", "x"],
                taskdir=taskdir,
                engine_name="agy",
            )
        self.assertIn("invoke agy", caught.exception.detail)

    def test_wrapper_enforces_engine_allowlist_via_env(self) -> None:
        os.environ["RINGER_SAFE_ENFORCE"] = "1"
        dump = self.write_dump_script()
        config_path = self.root / "config.toml"
        self.write_probe_config(config_path, dump)
        config = ringer.AppConfig.load(config_path, runtime_root=self.root / "rr")
        manifest = ringer.Manifest.from_obj(
            {
                "run_name": "probe-blocked",
                "workdir": str(self.root / "work"),
                "max_parallel": 1,
                "tasks": [
                    {
                        "key": "task-a",
                        "engine": "probe",
                        "spec": "dump env",
                        "check": "test -f env.json",
                    }
                ],
            }
        )
        with self.assertRaises(ringer.RuntimeIsolationError) as caught:
            ringer.isolation_preflight(config, manifest=manifest)
        self.assertEqual(caught.exception.classification, "MANIFEST_POLICY_FAILURE")
        self.assertIn("not allowlisted", caught.exception.detail)


class SafeManifestTests(IsolationTestCase):
    def write_manifest(self, name: str, payload: dict[str, object]) -> Path:
        allowed = self.root / "safe-manifests"
        allowed.mkdir(exist_ok=True)
        path = allowed / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.environ["RINGER_SAFE_MANIFEST_ROOTS"] = str(allowed)
        os.environ["RINGER_SAFE_SOURCE_REPO"] = str(ROOT)
        return path

    def base_manifest(self, **overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "run_name": "safe",
            "workdir": str(self.root / "work"),
            "max_parallel": 1,
            "tasks": [
                {
                    "key": "review",
                    "engine": "agy",
                    "spec": "Review the change.",
                    "check": "test -f report.md",
                }
            ],
        }
        data.update(overrides)
        return data

    def test_safe_manifest_rejects_full_access(self) -> None:
        path = self.write_manifest(
            "full.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "test -f report.md",
                        "full_access": True,
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("full_access", caught.exception.message)

    def test_safe_manifest_rejects_unknown_engine(self) -> None:
        path = self.write_manifest(
            "engine.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "codex",
                        "spec": "Review",
                        "check": "test -f report.md",
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("not allowlisted", caught.exception.message)

    def test_safe_manifest_rejects_repo_outside_allowlist(self) -> None:
        allowed = self.root / "allowed-repo"
        allowed.mkdir()
        os.environ["RINGER_SAFE_PROJECT_ROOTS"] = str(allowed)
        path = self.write_manifest(
            "repo.json",
            self.base_manifest(
                workdir=str(allowed / "work"),
                repo=str(self.root / "other-repo"),
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("project roots", caught.exception.message)

    def test_safe_manifest_rejects_workdir_outside_project_roots(self) -> None:
        allowed = self.root / "allowed-repo"
        allowed.mkdir()
        os.environ["RINGER_SAFE_PROJECT_ROOTS"] = str(allowed)
        path = self.write_manifest(
            "work.json",
            self.base_manifest(workdir=str(self.root / "other-work")),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("project/runtime roots", caught.exception.message)

    def test_safe_manifest_rejects_path_traversal(self) -> None:
        path = self.write_manifest(
            "trav.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "../escape",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "test -f report.md",
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("unsafe", caught.exception.message)

    def test_safe_manifest_rejects_extra_add_dir(self) -> None:
        path = self.write_manifest(
            "add-dir.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "test -f report.md",
                        "engine_args": ["--add-dir", "/"],
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("--add-dir", caught.exception.message)

    def test_safe_manifest_rejects_add_dir_equals_form(self) -> None:
        path = self.write_manifest(
            "add-dir-eq.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "test -f report.md",
                        "engine_args": ["--add-dir=/"],
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("--add-dir", caught.exception.message)

    def test_safe_manifest_rejects_add_dir_case_variant(self) -> None:
        path = self.write_manifest(
            "add-dir-case.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "test -f report.md",
                        "engine_args": ["--Add-Dir", "/"],
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("--add-dir", caught.exception.message)

    def test_safe_manifest_rejects_workdir_under_real_home(self) -> None:
        os.environ["RINGER_SAFE_REAL_HOME"] = str(self.root / "real-home")
        (self.root / "real-home").mkdir()
        path = self.write_manifest(
            "home-work.json",
            self.base_manifest(workdir=str(self.root / "real-home" / "project")),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("real home", caught.exception.message)

    def test_safe_manifest_rejects_home_relative_expect_files(self) -> None:
        path = self.write_manifest(
            "home-abs.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "test -f report.md",
                        "expect_files": ["~/.ssh/id_rsa"],
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("absolute output path", caught.exception.message)

    def test_safe_manifest_rejects_tilde_output_dir(self) -> None:
        runtime = self.root / "rt"
        runtime.mkdir()
        os.environ["RINGER_SAFE_RUNTIME_ROOTS"] = str(runtime)
        path = self.write_manifest(
            "tilde-out.json",
            self.base_manifest(
                workdir=str(runtime / "work"),
                artifact_dir="~/escaped-artifacts",
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("outside approved", caught.exception.message)

    def test_safe_manifest_rejects_absolute_expect_files(self) -> None:
        path = self.write_manifest(
            "abs.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "test -f report.md",
                        "expect_files": ["/etc/passwd"],
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("absolute output path", caught.exception.message)

    def test_safe_manifest_rejects_destructive_shell_check(self) -> None:
        path = self.write_manifest(
            "rm.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "rm -rf /tmp/ringer-not-real && test -f report.md",
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("destructive", caught.exception.message)

    def test_safe_manifest_rejects_shell_metacharacters(self) -> None:
        path = self.write_manifest(
            "meta.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "test -f report.md; echo extra",
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("metacharacter", caught.exception.message)

    def test_safe_manifest_rejects_relative_traversal_in_check(self) -> None:
        path = self.write_manifest(
            "check-trav.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "grep . ../../.ssh/id_rsa",
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("path traversal", caught.exception.message)

    def test_safe_manifest_rejects_redirect_in_check(self) -> None:
        path = self.write_manifest(
            "redir.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": "test -f report.md > /tmp/out",
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("metacharacter", caught.exception.message)

    def test_safe_manifest_rejects_non_conservative_argv(self) -> None:
        path = self.write_manifest(
            "argv-bash.json",
            self.base_manifest(
                tasks=[
                    {
                        "key": "review",
                        "engine": "agy",
                        "spec": "Review",
                        "check": {"argv": ["bash", "run.sh"]},
                    }
                ]
            ),
        )
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("argv checks must use", caught.exception.message)

    def test_safe_manifest_rejects_non_boolean_full_access(self) -> None:
        path = self.write_manifest("bool.json", self.base_manifest(full_access="yes"))
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("boolean", caught.exception.message)

    def test_safe_manifest_rejects_max_parallel_over_limit(self) -> None:
        path = self.write_manifest("parallel.json", self.base_manifest(max_parallel=5))
        with self.assertRaises(validator.PolicyError) as caught:
            validator.validate_manifest(path)
        self.assertIn("exceeds limit", caught.exception.message)


class IntegrationIsolationTests(IsolationTestCase):
    def test_run_creates_no_runtime_state_inside_source_repository(self) -> None:
        config = self.root / "config.toml"
        self.write_legacy_config(config)
        runtime = self.root / "rr"
        workdir = self.root / "work"
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_name": "no-repo-state",
                    "workdir": str(workdir),
                    "max_parallel": 1,
                    "tasks": [
                        {
                            "key": "hello-task",
                            "engine": "mock",
                            "spec": (
                                "MOCK_FILE: hello.txt\nhello from mock\nMOCK_END"
                            ),
                            "check": "grep -q hello hello.txt",
                            "expect_files": ["hello.txt"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        before = {path.relative_to(ROOT) for path in ROOT.rglob("*") if path.is_file()}
        result = self.run_cli(
            "--config",
            str(config),
            "--runtime-root",
            str(runtime),
            "run",
            str(manifest),
            "--identity",
            "iso-test",
            "--no-dashboard",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        after = {path.relative_to(ROOT) for path in ROOT.rglob("*") if path.is_file()}
        created = {path for path in after - before if "__pycache__" not in path.parts}
        self.assertEqual(created, set())

    def test_isolated_verify_rejects_absolute_expect_file(self) -> None:
        workdir = self.root / "work"
        workdir.mkdir()
        taskdir = workdir / "task-one"
        taskdir.mkdir()
        (taskdir / "hello.txt").write_text("ok\n", encoding="utf-8")
        task = ringer.TaskSpec(
            key="task-one",
            spec="noop",
            check="test -f hello.txt",
            engine="mock",
            expect_files=("/etc/passwd",),
        )
        result = asyncio.run(
            ringer.Verifier().verify(task, taskdir, isolated=True)
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.missing_files, ("/etc/passwd",))

    def test_isolated_harvest_skips_absolute_escape(self) -> None:
        runtime_root = self.root / "rr"
        config = ringer.AppConfig.load(self.empty_config(), runtime_root=runtime_root)
        workdir = self.root / "work"
        manifest = ringer.Manifest.from_obj(
            {
                "run_name": "harvest-escape",
                "workdir": str(workdir),
                "max_parallel": 1,
                "tasks": [
                    {
                        "key": "task-one",
                        "engine": "mock",
                        "spec": "noop",
                        "check": "test -f hello.txt",
                        "expect_files": ["/etc/passwd", "hello.txt"],
                    }
                ],
            }
        )
        runner = ringer.RingerRunner(manifest, config, "iso-test", dashboard_enabled=False)
        runtime = runner.runtimes[0]
        runtime.taskdir.mkdir(parents=True)
        (runtime.taskdir / "hello.txt").write_text("ok\n", encoding="utf-8")
        runtime.log_path.parent.mkdir(parents=True, exist_ok=True)
        runtime.log_path.write_text("log\n", encoding="utf-8")
        runner._harvest_deliverables_on_pass(runtime)
        names = [item["name"] for item in runtime.deliverables]
        self.assertEqual(names, ["hello.txt"])
        self.assertTrue(any("escapes" in note for note in runtime.deliverable_notes))

    def test_unwritable_real_home_does_not_break_isolated_run(self) -> None:
        fake_home = self.root / "unwritable-home"
        fake_home.mkdir()
        fake_home.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(lambda: fake_home.chmod(stat.S_IRWXU))
        config = self.root / "config.toml"
        self.write_legacy_config(config)
        runtime = self.root / "rr"
        workdir = self.root / "work"
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_name": "unwritable-home",
                    "workdir": str(workdir),
                    "max_parallel": 1,
                    "tasks": [
                        {
                            "key": "hello-task",
                            "engine": "mock",
                            "spec": (
                                "MOCK_FILE: hello.txt\nhello from mock\nMOCK_END"
                            ),
                            "check": "grep -q hello hello.txt",
                            "expect_files": ["hello.txt"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = self.run_cli(
            "--config",
            str(config),
            "--runtime-root",
            str(runtime),
            "run",
            str(manifest),
            "--identity",
            "iso-test",
            "--no-dashboard",
            env={"HOME": str(fake_home)},
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([], list(fake_home.rglob("*")))

    def test_mock_engine_smoke_files_stay_in_runtime_or_worktree(self) -> None:
        config = self.root / "config.toml"
        self.write_legacy_config(config)
        runtime = self.root / "rr"
        workdir = self.root / "work"
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_name": "smoke-containment",
                    "workdir": str(workdir),
                    "max_parallel": 1,
                    "tasks": [
                        {
                            "key": "hello-task",
                            "engine": "mock",
                            "spec": (
                                "MOCK_FILE: hello.txt\nhello from mock\nMOCK_END"
                            ),
                            "check": "grep -q hello hello.txt",
                            "expect_files": ["hello.txt"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = self.run_cli(
            "--config",
            str(config),
            "--runtime-root",
            str(runtime),
            "run",
            str(manifest),
            "--identity",
            "iso-test",
            "--no-dashboard",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        allowed = (runtime.resolve(), workdir.resolve())
        for path in runtime.rglob("*"):
            if path.is_file():
                self.assertTrue(
                    any(ringer.path_is_contained(path, root) for root in allowed),
                    f"escaped file: {path}",
                )


class SafeRunWrapperTests(IsolationTestCase):
    def test_safe_config_keeps_sandbox_and_denies_full_access(self) -> None:
        data = tomllib.loads((ROOT / "config.safe.toml").read_text(encoding="utf-8"))
        self.assertFalse(data["allow_full_access"])
        self.assertFalse(data["update"]["auto"])
        self.assertIn("--sandbox", data["engines"]["agy"]["args_template"])
        pairs = set(zip(
            data["engines"]["agy"]["args_template"],
            data["engines"]["agy"]["args_template"][1:],
        ))
        self.assertIn(("--add-dir", "{workdir}"), pairs)
        self.assertIn(("--add-dir", "{taskdir}"), pairs)

    def test_safe_config_full_access_args_empty(self) -> None:
        data = tomllib.loads((ROOT / "config.safe.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["engines"]["agy"]["full_access_args"], [])

    def test_wrapper_missing_manifest_prints_preflight_failure(self) -> None:
        result = subprocess.run(
            [
                str(ROOT / "bin" / "ringer-safe-run"),
                "--manifest",
                str(self.root / "missing.json"),
                "--identity",
                "iso-test",
            ],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("PREFLIGHT_FAILURE", result.stdout)

    def test_wrapper_rejects_manifest_outside_allowlist(self) -> None:
        manifest = self.root / "outside.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_name": "outside",
                    "workdir": str(self.root / "work"),
                    "max_parallel": 1,
                    "tasks": [
                        {
                            "key": "review",
                            "engine": "mock",
                            "spec": "no",
                            "check": "test -f report.md",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(ROOT / "bin" / "ringer-safe-run"),
                "--manifest",
                str(manifest),
                "--identity",
                "iso-test",
            ],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("MANIFEST_POLICY_FAILURE", result.stdout)

    def test_wrapper_seeds_default_agy_oauth_token(self) -> None:
        fake_home = self.root / "real-home"
        token = fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token.parent.mkdir(parents=True)
        token.write_text("fake-token\n", encoding="utf-8")
        allowed = self.root / "safe-manifests"
        allowed.mkdir()
        workdir = self.root / "work"
        manifest = allowed / "mock.json"
        config = self.root / "mock.toml"
        self.write_legacy_config(config)
        manifest.write_text(
            json.dumps(
                {
                    "run_name": "seed-oauth",
                    "workdir": str(workdir),
                    "max_parallel": 1,
                    "tasks": [
                        {
                            "key": "hello-task",
                            "engine": "mock",
                            "spec": "MOCK_FILE: hello.txt\nhello\nMOCK_END",
                            "check": "grep -q hello hello.txt",
                            "expect_files": ["hello.txt"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["HOME"] = str(fake_home)
        env["RINGER_SAFE_MANIFEST_ROOTS"] = str(allowed)
        env["RINGER_SAFE_ALLOWED_ENGINES"] = "mock"
        env["RINGER_SAFE_CONFIG"] = str(config)
        env["RINGER_SAFE_RUNTIME_PARENT"] = str(self.root / "runtimes")
        (self.root / "runtimes").mkdir()
        env.pop("RINGER_SAFE_AGY_COPY_PATHS", None)
        env.pop("RINGER_SAFE_CLEAN_SUCCESS", None)
        env["RINGER_NO_SELF_UPDATE"] = "1"
        result = subprocess.run(
            [
                str(ROOT / "bin" / "ringer-safe-run"),
                "--manifest",
                str(manifest),
                "--identity",
                "iso-test",
                "--keep-runtime",
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime_line = [
            line for line in result.stdout.splitlines() if line.startswith("RINGER_RUNTIME_ROOT=")
        ]
        self.assertTrue(runtime_line, result.stdout)
        runtime = Path(runtime_line[-1].split("=", 1)[1])
        self.assertTrue((runtime / "host-home").is_dir(), result.stdout)
        self.assertTrue((runtime / "engine-homes").is_dir(), result.stdout)
        leftover = [
            path
            for path in runtime.rglob("*")
            if path.name
            in {
                "antigravity-oauth-token",
                "oauth_creds.json",
                "google_accounts.json",
            }
        ]
        self.assertEqual([], leftover)

    def test_wrapper_rejects_cli_config_override(self) -> None:
        result = subprocess.run(
            [
                str(ROOT / "bin" / "ringer-safe-run"),
                "--manifest",
                str(self.root / "missing.json"),
                "--identity",
                "iso-test",
                "--config",
                str(self.root / "evil.toml"),
            ],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("MANIFEST_POLICY_FAILURE", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
