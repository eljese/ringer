#!/usr/bin/env python3
"""Integrated fail-closed PR-train supervisor entrypoint."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ringer_pr_train_engine as engine  # noqa: E402
import ringer_pr_train_guards as guards  # noqa: E402
import ringer_supervisor_hardened as hardened  # noqa: E402


class IntegratedSupervisorError(hardened.HardenedSupervisorError):
    pass


@dataclass(frozen=True)
class RuntimeLayout:
    root: Path
    home: Path
    xdg_config_home: Path
    xdg_cache_home: Path
    xdg_state_home: Path
    xdg_data_home: Path
    tmpdir: Path
    opencode_auth: Path

    @classmethod
    def from_root(cls, root: Path) -> "RuntimeLayout":
        resolved = root.expanduser().resolve()
        return cls(
            root=resolved,
            home=resolved / "home",
            xdg_config_home=resolved / "xdg-config",
            xdg_cache_home=resolved / "xdg-cache",
            xdg_state_home=resolved / "xdg-state",
            xdg_data_home=resolved / "xdg-data",
            tmpdir=resolved / "tmp",
            opencode_auth=resolved / "xdg-data" / "opencode" / "auth.json",
        )

    def create(self) -> None:
        for path in (
            self.root,
            self.home,
            self.xdg_config_home,
            self.xdg_cache_home,
            self.xdg_state_home,
            self.xdg_data_home,
            self.tmpdir,
            self.opencode_auth.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        environment = dict(base or os.environ)
        environment.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.xdg_config_home),
                "XDG_CACHE_HOME": str(self.xdg_cache_home),
                "XDG_STATE_HOME": str(self.xdg_state_home),
                "XDG_DATA_HOME": str(self.xdg_data_home),
                "TMPDIR": str(self.tmpdir),
                "RINGER_RUNTIME_ROOT": str(self.root),
            }
        )
        return environment


class ProgressWriter:
    """Atomic telemetry writer. The file is advisory and never proves PASS."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.sequence = 0

    def write(self, state: str, **payload: Any) -> None:
        self.sequence += 1
        body = {
            "schema_version": 1,
            "sequence": self.sequence,
            "state": state,
            "telemetry_only": True,
            **payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(raw, self.path)
        finally:
            if os.path.exists(raw):
                os.unlink(raw)


def _inside(path: Path, parent: Path) -> bool:
    path = path.resolve()
    parent = parent.resolve()
    return path == parent or parent in path.parents


def _artifact_root(source: dict[str, Any], args: argparse.Namespace) -> Path:
    if args.artifact_dir is not None:
        return args.artifact_dir.expanduser().resolve()
    workdir = Path(str(source.get("workdir") or ".")).expanduser().resolve()
    return (workdir / "artifacts").resolve()


def _runtime_layout(source: dict[str, Any], artifact_root: Path) -> RuntimeLayout:
    raw = source.get("runtime_root") or artifact_root / ".pr-train-runtime"
    layout = RuntimeLayout.from_root(Path(str(raw)))
    repository = hardened._repo_root(source.get("repo"))
    if _inside(layout.root, repository):
        raise IntegratedSupervisorError(
            "RUNTIME_PATH_ESCAPE: runtime_root must resolve outside the source repository"
        )
    return layout


def _normalize_expected_path(raw: str, *, workdir: Path, task_key: str) -> str:
    value = raw.strip()
    if not value:
        raise IntegratedSupervisorError(
            "MANIFEST_POLICY_FAILURE: expect_files cannot contain blanks"
        )
    if value.startswith("{{TASK_DIR}}/"):
        guards.normalize_owned_path(
            value.removeprefix("{{TASK_DIR}}/"), IntegratedSupervisorError
        )
        return value
    if "{{" in value:
        return value
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        pure = PurePosixPath(value.replace("\\", "/"))
        if ".." in pure.parts:
            raise IntegratedSupervisorError(
                "MANIFEST_POLICY_FAILURE: relative expect_files cannot escape the task worktree"
            )
        relative = guards.normalize_owned_path(
            str(pure).lstrip("./"), IntegratedSupervisorError
        )
        return "{{TASK_DIR}}/" + relative
    logical_task_dir = (workdir / task_key).resolve()
    resolved = candidate.resolve()
    if not _inside(resolved, logical_task_dir):
        raise IntegratedSupervisorError(
            "MANIFEST_POLICY_FAILURE: absolute expect_files must be inside the logical task directory"
        )
    relative = resolved.relative_to(logical_task_dir).as_posix()
    return "{{TASK_DIR}}/" + guards.normalize_owned_path(
        relative, IntegratedSupervisorError
    )


def normalize_manifest(source: dict[str, Any], *, artifact_root: Path) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise IntegratedSupervisorError("manifest root must be an object")
    normalized = json.loads(json.dumps(source))
    workdir = Path(str(normalized.get("workdir") or "")).expanduser().resolve()
    tasks = normalized.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise IntegratedSupervisorError("manifest tasks must be a non-empty list")
    policy_by_task: dict[str, list[str]] = {}
    for task in tasks:
        if not isinstance(task, dict) or not str(task.get("key") or "").strip():
            raise IntegratedSupervisorError("every task must be an object with a key")
        key = str(task["key"])
        raw_expected = task.get("expect_files") or []
        if not isinstance(raw_expected, list) or not all(
            isinstance(item, str) for item in raw_expected
        ):
            raise IntegratedSupervisorError("expect_files must be a list of strings")
        expected = [
            _normalize_expected_path(item, workdir=workdir, task_key=key)
            for item in raw_expected
        ]
        task["expect_files"] = expected
        policy_by_task[key] = guards.policy_from_task(
            task, expected, IntegratedSupervisorError
        )
    layout = _runtime_layout(normalized, artifact_root)
    normalized["runtime_root"] = str(layout.root)
    supervisor = normalized.setdefault("supervisor", {})
    if not isinstance(supervisor, dict):
        raise IntegratedSupervisorError("manifest supervisor must be an object")
    supervisor["_pr_train_allowed_changed_paths"] = policy_by_task
    seed = supervisor.get("credential_seed")
    if seed is not None and not isinstance(seed, dict):
        raise IntegratedSupervisorError("supervisor.credential_seed must be an object")
    if isinstance(seed, dict) and seed.get("destination"):
        requested = Path(str(seed["destination"])).expanduser().resolve()
        if requested != layout.opencode_auth:
            raise IntegratedSupervisorError(
                "PREFLIGHT_FAILURE: credential destination does not match canonical XDG_DATA_HOME"
            )
    if supervisor.get("provider_probes") and not isinstance(
        supervisor.get("worker_capability_probes"), dict
    ):
        raise IntegratedSupervisorError(
            "PREFLIGHT_FAILURE: real provider routes require worker_capability_probes"
        )
    return normalized


def _credential_source(supervisor: dict[str, Any]) -> tuple[Path | None, bool]:
    seed = supervisor.get("credential_seed") or {}
    required = bool(seed.get("required", False)) if isinstance(seed, dict) else False
    configured = seed.get("source") if isinstance(seed, dict) else None
    if configured:
        return Path(str(configured)).expanduser().resolve(), required
    env_source = os.environ.get("OPENCODE_AUTH_SOURCE")
    if env_source:
        return Path(env_source).expanduser().resolve(), required
    original_home = Path(os.environ.get("HOME", "~")).expanduser()
    original_data = Path(
        os.environ.get("XDG_DATA_HOME", str(original_home / ".local" / "share"))
    ).expanduser()
    candidates = (
        original_data / "opencode" / "auth.json",
        original_home / ".local" / "share" / "opencode" / "auth.json",
        original_home / ".config" / "opencode" / "auth.json",
    )
    return next((path.resolve() for path in candidates if path.is_file()), None), required


def seed_opencode_credentials(
    source: dict[str, Any], layout: RuntimeLayout
) -> Path | None:
    supervisor = source.get("supervisor") or {}
    credential_source, required = _credential_source(supervisor)
    if credential_source is None:
        if required:
            raise IntegratedSupervisorError(
                "PREFLIGHT_FAILURE: required OpenCode credential source was not found"
            )
        return None
    if not credential_source.is_file():
        raise IntegratedSupervisorError(
            f"PREFLIGHT_FAILURE: OpenCode credential source does not exist: {credential_source}"
        )
    layout.create()
    shutil.copyfile(credential_source, layout.opencode_auth)
    os.chmod(layout.opencode_auth, 0o600)
    return layout.opencode_auth


def assert_candidate_is_source_only(
    worktree: Path, allowed_changed_paths: tuple[str, ...] | list[str] | None = None
) -> list[str]:
    return guards.assert_candidate(
        worktree, IntegratedSupervisorError, allowed_changed_paths
    )


def _install_runtime_guards(
    progress: ProgressWriter,
    *,
    source: dict[str, Any],
    layout: RuntimeLayout,
    artifact_root: Path,
) -> Callable[[], None]:
    original_export = hardened.lifecycle.export_worktree_patch
    original_emit = hardened.legacy.EventWriter.emit
    original_checks = hardened._run_objective_checks
    original_worker = hardened.legacy._run_worker
    original_classify = hardened.lifecycle.classify_failure
    original_preflight = hardened.preflight
    policies = guards.policies_by_worktree(source, hardened)

    def guarded_export(worktree: Path, *args: Any, **kwargs: Any):
        policy = guards.policy_for(worktree, policies, IntegratedSupervisorError)
        assert_candidate_is_source_only(worktree, policy)
        return original_export(worktree, *args, **kwargs)

    def emit(writer: Any, event_type: str, **payload: Any) -> None:
        original_emit(writer, event_type, **payload)
        state = {
            "RUN_STARTED": "PREFLIGHT_RUNNING",
            "PREFLIGHT_STARTED": "PREFLIGHT_RUNNING",
            "PREFLIGHT_PASSED": "PROVIDER_RUNNING",
            "WORKER_STARTED": "PROVIDER_RUNNING",
            "WORKER_HEARTBEAT": "PROVIDER_RUNNING",
            "RUN_COMPLETED": "TERMINAL",
            "RUN_FAILED": "TERMINAL",
            "PREFLIGHT_FAILED": "TERMINAL",
        }.get(event_type)
        if state:
            progress.write(state, event_type=event_type, **payload)

    def checks(*args: Any, **kwargs: Any):
        cwd = Path(kwargs["cwd"])
        policy = guards.policy_for(cwd, policies, IntegratedSupervisorError)
        assert_candidate_is_source_only(cwd, policy)
        values = args[0] if args else kwargs.get("checks", [])
        progress.write("OBJECTIVE_CHECK_RUNNING", checks_total=len(values))
        outcomes = original_checks(*args, **kwargs)
        assert_candidate_is_source_only(cwd, policy)
        progress.write(
            "REVIEW_PENDING"
            if outcomes and all(item.status == "pass" for item in outcomes)
            else "TERMINAL",
            checks_completed=len(outcomes),
            checks_passed=bool(outcomes)
            and all(item.status == "pass" for item in outcomes),
        )
        return outcomes

    def worker(*args: Any, **kwargs: Any):
        try:
            return original_worker(*args, **kwargs)
        finally:
            moved = guards.relocate_inner_artifacts(
                Path(kwargs["cwd"]),
                Path(kwargs["log_path"]).parent / "ringer-lifecycle-artifacts",
                IntegratedSupervisorError,
            )
            if moved:
                progress.write("PROVIDER_RUNNING", relocated_inner_artifacts=moved)

    def classify(text: str, *, returncode: int | None = None) -> str:
        if engine.engine_error(text) is not None:
            return engine.ENGINE_RUNTIME_ERROR
        return original_classify(text, returncode=returncode)

    def preflight(*args: Any, **kwargs: Any):
        report = original_preflight(*args, **kwargs)
        supervisor = kwargs["supervisor"]
        health = dict(report.provider_health)
        seen: set[str] = set()
        for route in kwargs["routes"]:
            key = hardened._route_key(route)
            if key in seen:
                continue
            seen.add(key)
            probe = engine.validate_probe(
                supervisor, route, key, IntegratedSupervisorError
            )
            capability = engine.run_probe(
                route,
                key,
                probe,
                runtime_root=layout.root,
                artifact_root=artifact_root,
                environment=kwargs["environment"],
                error_type=IntegratedSupervisorError,
            )
            health[key] = {**health.get(key, {}), "worker_capability": capability}
        return replace(report, provider_health=health)

    hardened.lifecycle.export_worktree_patch = guarded_export
    hardened.legacy.EventWriter.emit = emit
    hardened._run_objective_checks = checks
    hardened.legacy._run_worker = worker
    hardened.lifecycle.classify_failure = classify
    hardened.preflight = preflight

    def restore() -> None:
        hardened.lifecycle.export_worktree_patch = original_export
        hardened.legacy.EventWriter.emit = original_emit
        hardened._run_objective_checks = original_checks
        hardened.legacy._run_worker = original_worker
        hardened.lifecycle.classify_failure = original_classify
        hardened.preflight = original_preflight

    return restore


def command_run(args: argparse.Namespace) -> int:
    original_manifest = args.manifest.expanduser().resolve()
    source = json.loads(original_manifest.read_text(encoding="utf-8"))
    artifact_root = _artifact_root(source, args)
    normalized = normalize_manifest(source, artifact_root=artifact_root)
    layout = _runtime_layout(normalized, artifact_root)
    layout.create()
    seeded = seed_opencode_credentials(normalized, layout)
    manifest_dir = layout.root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    normalized_manifest = manifest_dir / f"manifest-{uuid.uuid4().hex}.json"
    normalized_manifest.write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    progress = ProgressWriter(artifact_root / "supervisor-progress.json")
    progress.write(
        "PREFLIGHT_RUNNING",
        source_manifest=str(original_manifest),
        normalized_manifest=str(normalized_manifest),
    )
    restore = _install_runtime_guards(
        progress, source=normalized, layout=layout, artifact_root=artifact_root
    )
    delegated = argparse.Namespace(**vars(args))
    delegated.manifest = normalized_manifest
    result = 2
    try:
        result = hardened.command_run(delegated)
        return result
    finally:
        restore()
        log_paths, combined = engine.collect_logs(layout.root, artifact_root)
        engine.enrich_outcome(
            artifact_root, log_paths, combined, hardened.legacy._atomic_json
        )
        progress.write(
            "TERMINAL",
            exit_code=result,
            canonical_outcome=str(artifact_root / "supervisor-outcome.json"),
        )
        if seeded is not None:
            seeded.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="supervise an integrated hardened lifecycle manifest")
    run.add_argument("manifest", type=Path)
    run.add_argument(
        "--ringer",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "ringer.py",
    )
    run.add_argument("--config", type=Path)
    run.add_argument("--identity", default="ringer-supervisor-integrated")
    run.add_argument("--artifact-dir", type=Path)
    run.add_argument("--cleanup", action=argparse.BooleanOptionalAction, default=True)
    run.set_defaults(func=command_run)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except (OSError, ValueError, json.JSONDecodeError, IntegratedSupervisorError) as error:
        print(f"ringer-supervisor-integrated: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
