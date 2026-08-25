#!/usr/bin/env python3
"""Fail-closed PR-train supervisor for implementation runs.

The legacy supervisor remains available for compatibility. This entrypoint is
the narrower PR-train boundary: it owns implementation routing, path
isolation, provider inference probes, immutable baseline identity, post-check
patch sealing, and terminal process cleanup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ringer_lifecycle as lifecycle  # noqa: E402
import ringer_supervisor as legacy  # noqa: E402


APPROVED_ENGINE = "opencode"
APPROVED_MODEL_MARKER = "minimax"
MAX_ROUTES = 2
REPORT_NAMES = {name.lower() for name in lifecycle.REPORT_NAMES}
SHELL_NAMES = {"sh", "bash", "dash", "zsh", "fish", "cmd", "pwsh", "powershell"}
INTERPRETER_NAMES = {"python", "python3", "pypy", "node", "ruby", "perl"}
MARKER_NAMES = {
    "implementation_complete",
    "worker_complete",
    "task_complete",
    "completion_marker",
    "checks_passed",
    "all_checks_pass",
    "all_checks_passed",
}


class HardenedSupervisorError(legacy.SupervisorError):
    pass


class SupervisorInterrupted(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(f"supervisor interrupted by signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class ObjectiveCheckOutcome:
    index: int
    argv: list[str]
    status: str
    returncode: int
    duration_seconds: float
    log_path: str


@dataclass(frozen=True)
class HardenedAttemptOutcome:
    attempt: int
    engine: str
    model: str
    status: str
    failure_class: str | None
    source_baseline_sha: str
    candidate_head_sha: str | None
    post_check_head_sha: str | None
    worktree: str
    provenance_path: str
    worker_patch_path: str | None
    worker_patch_sha256: str | None
    patch_path: str | None
    patch_sha256: str | None
    objective_checks: list[ObjectiveCheckOutcome]
    worker: dict[str, Any]
    error: str | None


@dataclass(frozen=True)
class HardenedTaskOutcome:
    key: str
    status: str
    attempts: list[HardenedAttemptOutcome]
    selected_engine: str | None
    selected_model: str | None
    failure_class: str | None
    patch_path: str | None
    patch_sha256: str | None
    artifact_dir: str


_ACTIVE_PROCESSES: set[subprocess.Popen[Any]] = set()


def _is_within(path: Path, parent: Path) -> bool:
    path = path.resolve()
    parent = parent.resolve()
    return path == parent or parent in path.parents


def _repo_root(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise HardenedSupervisorError(
            "MANIFEST_POLICY_FAILURE: hardened implementation requires repo"
        )
    candidate = Path(raw).expanduser().resolve()
    probe = legacy._git(candidate, "rev-parse", "--show-toplevel")
    if probe.returncode != 0 or not probe.stdout.strip():
        raise HardenedSupervisorError(f"PREFLIGHT_FAILURE: repo is not a git checkout: {candidate}")
    return Path(probe.stdout.strip()).resolve()


def _path_from(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise HardenedSupervisorError(f"RUNTIME_PATH_ESCAPE: {label} must be a path")
    path = Path(value).expanduser().resolve()
    if path == Path("/"):
        raise HardenedSupervisorError(f"RUNTIME_PATH_ESCAPE: {label} cannot be the filesystem root")
    return path


def _validate_runtime_paths(
    source: dict[str, Any],
    *,
    artifact_root: Path,
) -> tuple[Path, Path, Path]:
    repository = _repo_root(source.get("repo"))
    run_dir = _path_from(source.get("workdir"), "workdir")
    artifact_root = artifact_root.resolve()
    if _is_within(run_dir, repository):
        raise HardenedSupervisorError(
            "RUNTIME_PATH_ESCAPE: workdir must resolve outside the source repository"
        )
    if _is_within(artifact_root, repository):
        raise HardenedSupervisorError(
            "RUNTIME_PATH_ESCAPE: artifact root must resolve outside the source repository"
        )

    path_fields = (
        "runtime_root",
        "state_path",
        "state_file",
        "artifacts_dir",
        "artifact_dir",
        "worktrees_dir",
        "scratch_dir",
        "tmpdir",
        "home",
        "home_dir",
        "xdg_config_home",
        "xdg_cache_home",
        "xdg_state_home",
        "xdg_data_home",
        "auth_file",
    )
    candidates = dict(source)
    if isinstance(source.get("supervisor"), dict):
        candidates.update(
            {
                f"supervisor.{key}": value
                for key, value in source["supervisor"].items()
                if key in path_fields
            }
        )
    for field in path_fields:
        for candidate_key, candidate_value in candidates.items():
            if candidate_key != field and candidate_key != f"supervisor.{field}":
                continue
            if not isinstance(candidate_value, str):
                continue
            path = _path_from(candidate_value, candidate_key)
            if _is_within(path, repository):
                raise HardenedSupervisorError(
                    f"RUNTIME_PATH_ESCAPE: {candidate_key} must resolve outside the source repository"
                )
    return repository, run_dir, artifact_root


def _route_key(route: legacy.Route) -> str:
    return f"{route.engine.strip().lower()}:{(route.model or '').strip().lower()}"


def _route_policy(supervisor: dict[str, Any], routes: list[legacy.Route]) -> None:
    if not routes:
        raise HardenedSupervisorError(
            "MANIFEST_POLICY_FAILURE: at least one implementation route is required"
        )
    if len(routes) > MAX_ROUTES:
        raise HardenedSupervisorError(
            f"MANIFEST_POLICY_FAILURE: implementation routes are limited to {MAX_ROUTES}"
        )
    if "allowed_implementation_engines" in supervisor:
        configured = {
            str(item).strip().lower()
            for item in supervisor["allowed_implementation_engines"]
        }
        if configured != {APPROVED_ENGINE}:
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: implementation engine policy cannot be widened"
            )
    if "allowed_implementation_model_markers" in supervisor:
        configured = {
            str(item).strip().lower()
            for item in supervisor["allowed_implementation_model_markers"]
        }
        if configured != {APPROVED_MODEL_MARKER}:
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: implementation model policy cannot be widened"
            )
    seen: set[str] = set()
    for route in routes:
        engine = route.engine.strip().lower()
        model = (route.model or "").strip().lower()
        if engine != APPROVED_ENGINE:
            raise HardenedSupervisorError(
                f"MANIFEST_POLICY_FAILURE: implementation route {route.engine!r} is not approved"
            )
        if not model or APPROVED_MODEL_MARKER not in model:
            raise HardenedSupervisorError(
                f"MANIFEST_POLICY_FAILURE: implementation model {route.model!r} is not approved"
            )
        key = f"{engine}:{model}"
        if key in seen:
            raise HardenedSupervisorError(
                f"MANIFEST_POLICY_FAILURE: duplicate implementation route {key}"
            )
        seen.add(key)
    fallback_on = supervisor.get("fallback_on")
    if fallback_on is not None:
        if not isinstance(fallback_on, list) or any(not isinstance(item, str) for item in fallback_on):
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: fallback_on must be a list of failure classes"
            )
        forbidden = {"MANIFEST_POLICY_FAILURE", "RUNTIME_PATH_ESCAPE", "PREFLIGHT_FAILURE"}
        if forbidden.intersection(fallback_on):
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: policy and preflight failures cannot trigger fallback"
            )


def _probe_for_route(
    probes: dict[str, Any],
    route: legacy.Route,
    *,
    route_count_by_engine: dict[str, int],
) -> dict[str, Any]:
    key = _route_key(route)
    raw = probes.get(key)
    if raw is None and route_count_by_engine.get(route.engine.lower(), 0) == 1:
        raw = probes.get(route.engine)
    if not isinstance(raw, dict):
        raise HardenedSupervisorError(
            f"PREFLIGHT_FAILURE: missing exact inference probe for {key}"
        )
    argv = raw.get("argv")
    kind = str(raw.get("kind") or "").lower()
    expected = raw.get("expected_output", "PROBE_OK")
    if kind != "inference" or not isinstance(argv, list) or not argv:
        raise HardenedSupervisorError(
            f"PREFLIGHT_FAILURE: {key} requires kind=inference and argv"
        )
    if not all(isinstance(item, str) and item for item in argv):
        raise HardenedSupervisorError(f"PREFLIGHT_FAILURE: invalid inference argv for {key}")
    if not isinstance(expected, str) or not expected:
        raise HardenedSupervisorError(f"PREFLIGHT_FAILURE: missing probe canary for {key}")
    lowered = [item.strip().lower() for item in argv]
    if any(item in {"--version", "version", "--help", "help"} for item in lowered):
        raise HardenedSupervisorError(
            f"PREFLIGHT_FAILURE: version/help is not an inference probe for {key}"
        )
    if "-c" in lowered and Path(argv[0]).name.lower() in INTERPRETER_NAMES:
        raise HardenedSupervisorError(
            f"PREFLIGHT_FAILURE: inline interpreter probes are not allowed for {key}"
        )
    model = route.model or ""
    model_positions = [
        index + 1
        for index, item in enumerate(argv[:-1])
        if item in {"--model", "-m"}
    ]
    exact_model = any(argv[index] == model for index in model_positions) or any(
        item == f"--model={model}" for item in argv
    )
    if not exact_model:
        raise HardenedSupervisorError(
            f"PREFLIGHT_FAILURE: inference probe for {key} does not select the exact model"
        )
    if Path(argv[0]).name.lower() != APPROVED_ENGINE:
        raise HardenedSupervisorError(
            f"PREFLIGHT_FAILURE: inference probe for {key} must invoke {APPROVED_ENGINE}"
        )
    return {"argv": argv, "expected_output": expected, "timeout_seconds": raw.get("timeout_seconds", 60)}


def _kill_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def _kill_active_processes() -> None:
    for process in tuple(_ACTIVE_PROCESSES):
        _kill_process(process)


def _runtime_environment(run_dir: Path, source: dict[str, Any]) -> dict[str, str]:
    runtime_root = _path_from(
        source.get("runtime_root", str(run_dir / "runtime")),
        "runtime_root",
    )
    dirs = {
        "HOME": runtime_root / "home",
        "XDG_CONFIG_HOME": runtime_root / "xdg-config",
        "XDG_CACHE_HOME": runtime_root / "xdg-cache",
        "XDG_STATE_HOME": runtime_root / "xdg-state",
        "XDG_DATA_HOME": runtime_root / "xdg-data",
        "TMPDIR": runtime_root / "tmp",
    }
    runtime_root.mkdir(parents=True, exist_ok=True)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({key: str(path) for key, path in dirs.items()})
    environment.update(
        {
            "RINGER_HOME": str(runtime_root / "ringer-home"),
            "RINGER_RUNTIME_ROOT": str(runtime_root),
            "RINGER_NO_SELF_UPDATE": "1",
            "RINGER_NO_CATALOG_REFRESH": "1",
            "RINGER_SAFE_SOURCE_REPO": str(_repo_root(source["repo"])),
        }
    )
    (runtime_root / "ringer-home").mkdir(parents=True, exist_ok=True)
    return environment


def _run_inference_probe(
    route: legacy.Route,
    probe: dict[str, Any],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    argv = [str(item) for item in probe["argv"]]
    resolved = shutil.which(argv[0], path=environment.get("PATH"))
    if resolved is None or not os.access(resolved, os.X_OK):
        raise HardenedSupervisorError(
            f"PREFLIGHT_FAILURE: provider binary cannot be resolved for {_route_key(route)}"
        )
    timeout = float(probe.get("timeout_seconds", 60))
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    _ACTIVE_PROCESSES.add(process)
    try:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process(process)
            raise HardenedSupervisorError(
                f"PROVIDER_TIMEOUT: provider inference probe timed out for {_route_key(route)}"
            )
    except BaseException:
        _kill_process(process)
        raise
    finally:
        _ACTIVE_PROCESSES.discard(process)
    if process.returncode != 0:
        failure_class = lifecycle.classify_failure(
            output or "", returncode=process.returncode
        )
        if failure_class not in {
            "PROVIDER_QUOTA",
            "PROVIDER_TIMEOUT",
            "NETWORK_SANDBOX",
        }:
            failure_class = "ENGINE_RUNTIME_ERROR"
        raise HardenedSupervisorError(
            f"{failure_class}: provider inference probe failed for {_route_key(route)} "
            f"with return code {process.returncode}"
        )
    if probe["expected_output"] not in (output or ""):
        raise HardenedSupervisorError(
            "ENGINE_RUNTIME_ERROR: provider inference probe returned no canary "
            f"for {_route_key(route)}"
        )
    return {
        "healthy": True,
        "engine": route.engine,
        "model": route.model,
        "kind": "inference",
    }


def preflight(
    source: dict[str, Any],
    *,
    supervisor: dict[str, Any],
    routes: list[legacy.Route],
    artifact_root: Path,
    environment: dict[str, str],
) -> legacy.PreflightReport:
    unique_routes: list[legacy.Route] = []
    seen_routes: set[str] = set()
    for route in routes:
        if _route_key(route) not in seen_routes:
            unique_routes.append(route)
            seen_routes.add(_route_key(route))
    _route_policy(supervisor, unique_routes)
    repository, run_dir, artifact_root = _validate_runtime_paths(
        source,
        artifact_root=artifact_root,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    canary = artifact_root / f".artifact-writable-{uuid.uuid4().hex}"
    try:
        canary.write_text("preflight\n", encoding="utf-8")
    except OSError as error:
        raise HardenedSupervisorError(
            f"PREFLIGHT_FAILURE: artifact directory is not writable: {artifact_root}"
        ) from error
    finally:
        canary.unlink(missing_ok=True)

    legacy_source = dict(source)
    legacy_source["repo"] = str(repository)
    legacy_supervisor = dict(supervisor)
    legacy_supervisor["provider_probes"] = {}
    report = legacy.preflight(
        legacy_source,
        supervisor=legacy_supervisor,
        routes=routes,
    )
    probes = supervisor.get("provider_probes")
    if not isinstance(probes, dict):
        raise HardenedSupervisorError(
            "PREFLIGHT_FAILURE: provider_probes must be an object"
        )
    counts: dict[str, int] = {}
    for route in unique_routes:
        counts[route.engine.lower()] = counts.get(route.engine.lower(), 0) + 1
    health: dict[str, dict[str, Any]] = {}
    for route in unique_routes:
        probe = _probe_for_route(probes, route, route_count_by_engine=counts)
        health[_route_key(route)] = _run_inference_probe(
            route,
            probe,
            cwd=run_dir,
            environment=environment,
        )
    return replace(report, repository=str(repository), provider_health=health)


def _run_worker_safe(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    heartbeat_seconds: float,
    no_progress_seconds: float,
    event_writer: Any,
    task_key: str,
    attempt: int,
    route: legacy.Route,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> tuple[int | None, float, str | None]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stop_reason: str | None = None
    no_progress_seconds = max(heartbeat_seconds, no_progress_seconds)
    last_progress = started
    last_signature: tuple[tuple[str, ...], int] | None = None
    process: subprocess.Popen[Any] | None = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            _ACTIVE_PROCESSES.add(process)
            next_heartbeat = started + heartbeat_seconds
            while process.poll() is None:
                now = time.monotonic()
                if now - started >= timeout_seconds:
                    stop_reason = "timeout"
                    _kill_process(process)
                    break
                if now >= next_heartbeat:
                    changed_paths = tuple(legacy._changed_paths(cwd))
                    log_bytes = log_path.stat().st_size if log_path.exists() else 0
                    signature = (changed_paths, log_bytes)
                    if signature != last_signature:
                        last_signature = signature
                        last_progress = now
                    if now - last_progress >= no_progress_seconds:
                        stop_reason = "no_progress"
                        _kill_process(process)
                        break
                    event_writer.emit(
                        "WORKER_HEARTBEAT",
                        task_id=task_key,
                        attempt=attempt,
                        engine=route.engine,
                        model=route.model,
                        duration_seconds=round(now - started, 3),
                        changed_paths=list(changed_paths),
                        log_bytes=log_bytes,
                    )
                    next_heartbeat = now + heartbeat_seconds
                time.sleep(min(1.0, max(0.05, heartbeat_seconds / 10)))
        returncode = process.returncode if process is not None else None
        return returncode, time.monotonic() - started, stop_reason
    except BaseException:
        if process is not None:
            _kill_process(process)
        raise
    finally:
        if process is not None:
            _ACTIVE_PROCESSES.discard(process)


legacy._run_worker = _run_worker_safe


def _reject_objective_bypass(argv: list[str], *, task_dir: Path) -> None:
    if not argv:
        raise HardenedSupervisorError("MANIFEST_POLICY_FAILURE: objective argv cannot be empty")
    executable = Path(argv[0]).name.lower()
    if executable in {"true", "false", ":"}:
        raise HardenedSupervisorError(
            "MANIFEST_POLICY_FAILURE: no-op objective checks are not allowed"
        )
    if executable in SHELL_NAMES:
        raise HardenedSupervisorError(
            "MANIFEST_POLICY_FAILURE: shell indirection is not allowed in objective checks"
        )
    if executable in INTERPRETER_NAMES:
        lowered = [item.lower() for item in argv]
        if "-c" in lowered or "-e" in lowered or "-m" not in lowered:
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: interpreter scripts and inline code are not allowed"
            )
    if executable in {"grep", "rg", "awk", "sed", "cat", "head", "tail", "find", "test"}:
        joined = " ".join(argv).lower()
        if any(marker in joined for marker in MARKER_NAMES):
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: objective checks cannot grep completion markers"
            )
    if Path(argv[0]).suffix.lower() in {".sh", ".bash", ".py", ".js", ".rb", ".pl"}:
        raise HardenedSupervisorError(
            "MANIFEST_POLICY_FAILURE: objective checks cannot invoke an indirect script"
        )
    for token in argv:
        token_lower = token.lower()
        token_name = Path(token).name.lower()
        if token_name in REPORT_NAMES or any(
            re.search(
                rf"(?:^|[/\\._-]){re.escape(name.rsplit('.', 1)[0])}(?:[/\\._-]|$)",
                token_lower,
            )
            for name in REPORT_NAMES
        ):
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: objective checks cannot reference worker-authored report files"
            )
        if token_lower in MARKER_NAMES or any(marker in token_lower for marker in MARKER_NAMES):
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: objective checks cannot reference completion markers"
            )
        if "/" in token or token.startswith("."):
            candidate = Path(token)
            if not candidate.is_absolute():
                candidate = task_dir / candidate
            if candidate.exists() and candidate.resolve().name.lower() in REPORT_NAMES:
                raise HardenedSupervisorError(
                    "MANIFEST_POLICY_FAILURE: objective checks cannot follow report symlinks"
                )


def _objective_check_argvs(
    task: dict[str, Any],
    *,
    run_dir: Path,
    task_dir: Path,
    artifact_dir: Path,
    repository: Path,
    attempt: int,
    baseline_sha: str,
) -> list[list[str]]:
    raw_checks = task.get("objective_checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise HardenedSupervisorError(
            "MANIFEST_POLICY_FAILURE: every implementation task requires non-empty objective_checks"
        )
    variables = lifecycle.canonical_values(
        run_dir=run_dir,
        task_dir=task_dir,
        artifact_dir=artifact_dir,
        source_repo=repository,
        attempt=attempt,
    )
    variables["{{BASE_SHA}}"] = baseline_sha
    result: list[list[str]] = []
    for index, raw in enumerate(raw_checks, start=1):
        if not isinstance(raw, dict) or set(raw) != {"argv"}:
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: objective_checks entries must contain only argv"
            )
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) and item for item in argv
        ):
            raise HardenedSupervisorError(
                f"MANIFEST_POLICY_FAILURE: objective check {index} has invalid argv"
            )
        resolved = [lifecycle.substitute(item, variables) for item in argv]
        _reject_objective_bypass(resolved, task_dir=task_dir)
        result.append(resolved)
    for path in task_dir.rglob("*"):
        if path.is_symlink() and path.resolve().name.lower() in REPORT_NAMES:
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: candidate contains a report-file symlink alias"
            )
    return result


def _run_objective_checks(
    checks: list[list[str]],
    *,
    cwd: Path,
    artifact_dir: Path,
    timeout_seconds: float,
    environment: dict[str, str],
) -> list[ObjectiveCheckOutcome]:
    outcomes: list[ObjectiveCheckOutcome] = []
    for index, argv in enumerate(checks, start=1):
        log_path = artifact_dir / f"objective-check-{index:03d}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        _ACTIVE_PROCESSES.add(process)
        output = ""
        try:
            try:
                output, _ = process.communicate(timeout=timeout_seconds)
                returncode = int(process.returncode)
            except subprocess.TimeoutExpired as error:
                output = error.stdout or ""
                _kill_process(process)
                returncode = 124
                output += "\nobjective check timed out\n"
        except BaseException:
            _kill_process(process)
            raise
        finally:
            _ACTIVE_PROCESSES.discard(process)
        log_path.write_text(output or "", encoding="utf-8")
        outcome = ObjectiveCheckOutcome(
            index=index,
            argv=argv,
            status="pass" if returncode == 0 else "fail",
            returncode=returncode,
            duration_seconds=round(time.monotonic() - started, 3),
            log_path=str(log_path),
        )
        outcomes.append(outcome)
        if returncode != 0:
            break
    return outcomes


class _AttemptEventWriter:
    def __init__(self, delegate: legacy.EventWriter, *, task_key: str, attempt: int) -> None:
        self.delegate = delegate
        self.task_key = task_key
        self.attempt = attempt

    def emit(self, event_type: str, **payload: Any) -> None:
        if "task_id" in payload:
            payload["task_id"] = self.task_key
        if "attempt" in payload:
            payload["attempt"] = self.attempt
        self.delegate.emit(event_type, **payload)


def _head(repo: Path) -> str | None:
    result = legacy._git(repo, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def _source_snapshot(repo: Path) -> str:
    result = legacy._git(repo, "status", "--porcelain=v1", "-z")
    if result.returncode != 0:
        raise HardenedSupervisorError(
            f"PREFLIGHT_FAILURE: could not snapshot source worktree: {repo}"
        )
    return result.stdout


def _cleanup_attempt(repository: Path, worktree: Path) -> None:
    try:
        lifecycle.cleanup_owned_worktree(repository, worktree)
    except Exception as error:
        raise HardenedSupervisorError(
            f"CLEANUP_FAILURE: could not remove attempt worktree {worktree}"
        ) from error
    if worktree.exists():
        raise HardenedSupervisorError(
            f"CLEANUP_FAILURE: attempt worktree remains after cleanup: {worktree}"
        )


def supervise_task(
    source: dict[str, Any],
    task: dict[str, Any],
    *,
    args: argparse.Namespace,
    supervisor: dict[str, Any],
    event_writer: legacy.EventWriter,
    provider_health: dict[str, dict[str, Any]],
    baseline_sha: str,
    environment: dict[str, str],
    source_snapshot: str,
) -> HardenedTaskOutcome:
    key = str(task["key"])
    run_dir = Path(str(source["workdir"])).expanduser().resolve()
    artifact_root = (args.artifact_dir or (run_dir / "artifacts")).expanduser().resolve()
    artifact_dir = artifact_root / key
    repository = _repo_root(source["repo"])
    routes = legacy._routes_for_task(task, supervisor)
    _route_policy(supervisor, routes)
    fallback_on = set(
        supervisor.get("fallback_on")
        or {"CHECK_FAILURE", "PROVIDER_TIMEOUT", "NO_PROGRESS"}
    )
    objective_timeout = float(supervisor.get("objective_check_timeout_seconds", 900))
    attempts: list[HardenedAttemptOutcome] = []
    selected_engine: str | None = None
    selected_model: str | None = None
    selected_patch: str | None = None
    selected_patch_sha: str | None = None
    final_failure: str | None = None

    for attempt, route in enumerate(routes, start=1):
        effective_key = f"{key}--attempt-{attempt:03d}"
        effective_task = dict(task)
        effective_task["key"] = effective_key
        effective_task["routes"] = [
            {
                "engine": route.engine,
                "model": route.model,
                "timeout_seconds": route.timeout_seconds,
            }
        ]
        effective_args = argparse.Namespace(**vars(args))
        effective_args.cleanup = False
        attempt_events = _AttemptEventWriter(event_writer, task_key=key, attempt=attempt)
        worktree = (run_dir / effective_key).resolve()
        worker_outcome: legacy.TaskOutcome | None = None
        objective_outcomes: list[ObjectiveCheckOutcome] = []
        status = "fail"
        failure_class: str | None = None
        error_text: str | None = None
        worker_patch_path: str | None = None
        worker_patch_sha: str | None = None
        post_patch_path: str | None = None
        post_patch_sha: str | None = None
        candidate_head: str | None = None
        post_check_head: str | None = None
        try:
            if _head(repository) != baseline_sha:
                raise HardenedSupervisorError(
                    "MANIFEST_POLICY_FAILURE: source HEAD changed after baseline capture"
                )
            if _source_snapshot(repository) != source_snapshot:
                raise HardenedSupervisorError(
                    "MANIFEST_POLICY_FAILURE: source worktree changed after baseline capture"
                )
            worker_outcome = legacy.supervise_task(
                source,
                effective_task,
                args=effective_args,
                supervisor=supervisor,
                event_writer=attempt_events,
                provider_health=provider_health,
                worker_env=environment,
                base_ref=baseline_sha,
            )
            candidate_head = _head(worktree)
            worker_patch_path = worker_outcome.patch_path
            worker_patch_sha = worker_outcome.patch_sha256
            if candidate_head != baseline_sha:
                raise HardenedSupervisorError(
                    "MANIFEST_POLICY_FAILURE: worker moved HEAD or created a commit"
                )
            if worker_outcome.status != "pass":
                failure_class = worker_outcome.failure_class or "CHECK_FAILURE"
            else:
                checks = _objective_check_argvs(
                    task,
                    run_dir=run_dir,
                    task_dir=worktree,
                    artifact_dir=artifact_dir / f"attempt-{attempt:03d}",
                    repository=repository,
                    attempt=attempt,
                    baseline_sha=baseline_sha,
                )
                objective_outcomes = _run_objective_checks(
                    checks,
                    cwd=worktree,
                    artifact_dir=artifact_dir / f"attempt-{attempt:03d}",
                    timeout_seconds=objective_timeout,
                    environment=environment,
                )
                post_check_head = _head(worktree)
                if (
                    _head(repository) != baseline_sha
                    or _source_snapshot(repository) != source_snapshot
                    or post_check_head != baseline_sha
                ):
                    raise HardenedSupervisorError(
                        "MANIFEST_POLICY_FAILURE: objective check changed repository identity"
                    )
                if not objective_outcomes or any(item.status != "pass" for item in objective_outcomes):
                    failure_class = "CHECK_FAILURE"
                else:
                    post_patch = lifecycle.export_worktree_patch(
                        worktree,
                        artifact_dir / f"attempt-{attempt:03d}" / "post-objective.patch",
                        source_repo=repository,
                        base_sha=baseline_sha,
                    )
                    post_patch_path = str(post_patch) if post_patch else None
                    post_patch_sha = lifecycle.sha256_file(post_patch) if post_patch else None
                    status = "pass"
            if failure_class is not None:
                error_text = f"attempt failed with {failure_class}"
        except BaseException as error:
            if isinstance(error, SupervisorInterrupted):
                raise
            error_text = str(error)
            failure_class = lifecycle.classify_failure(error_text)
            if isinstance(error, HardenedSupervisorError) and "MANIFEST_POLICY_FAILURE" in error_text:
                failure_class = "MANIFEST_POLICY_FAILURE"
            if isinstance(error, HardenedSupervisorError) and "CLEANUP_FAILURE" in error_text:
                failure_class = "CLEANUP_FAILURE"
        provenance_path = artifact_dir / f"attempt-{attempt:03d}-provenance.json"
        provenance = {
            "schema_version": 2,
            "task_key": key,
            "effective_task_key": effective_key,
            "attempt": attempt,
            "actual_engine": route.engine,
            "actual_model": route.model,
            "source_baseline_sha": baseline_sha,
            "source_tree_status_sha256": hashlib.sha256(source_snapshot.encode()).hexdigest(),
            "candidate_head_sha": candidate_head,
            "post_check_head_sha": post_check_head,
            "attempt_worktree": str(worktree),
            "worker_patch_path": worker_patch_path,
            "worker_patch_sha256": worker_patch_sha,
            "selected_patch_path": post_patch_path,
            "selected_patch_sha256": post_patch_sha,
            "objective_checks": [asdict(item) for item in objective_outcomes],
            "status": status,
            "failure_class": failure_class,
            "error": error_text,
        }
        legacy._atomic_json(provenance_path, provenance)
        attempt_outcome = HardenedAttemptOutcome(
            attempt=attempt,
            engine=route.engine,
            model=route.model or "",
            status=status,
            failure_class=failure_class,
            source_baseline_sha=baseline_sha,
            candidate_head_sha=candidate_head,
            post_check_head_sha=post_check_head,
            worktree=str(worktree),
            provenance_path=str(provenance_path),
            worker_patch_path=worker_patch_path,
            worker_patch_sha256=worker_patch_sha,
            patch_path=post_patch_path,
            patch_sha256=post_patch_sha,
            objective_checks=objective_outcomes,
            worker=legacy._task_payload(worker_outcome) if worker_outcome else {},
            error=error_text,
        )
        attempts.append(attempt_outcome)
        if status == "pass":
            selected_engine = route.engine
            selected_model = route.model
            selected_patch = post_patch_path
            selected_patch_sha = post_patch_sha
            final_failure = None
            if args.cleanup:
                _cleanup_attempt(repository, worktree)
            break
        final_failure = failure_class or "CHECK_FAILURE"
        if args.cleanup:
            _cleanup_attempt(repository, worktree)
        if final_failure not in fallback_on:
            break

    return HardenedTaskOutcome(
        key=key,
        status="pass" if selected_engine is not None else "fail",
        attempts=attempts,
        selected_engine=selected_engine,
        selected_model=selected_model,
        failure_class=final_failure,
        patch_path=selected_patch,
        patch_sha256=selected_patch_sha,
        artifact_dir=str(artifact_dir),
    )


def _attempt_payload(outcome: HardenedAttemptOutcome) -> dict[str, Any]:
    payload = asdict(outcome)
    payload["objective_checks"] = [asdict(item) for item in outcome.objective_checks]
    return payload


def _task_payload(outcome: HardenedTaskOutcome) -> dict[str, Any]:
    payload = asdict(outcome)
    payload["attempts"] = [_attempt_payload(item) for item in outcome.attempts]
    return payload


def _failure_class(error: BaseException) -> str:
    if isinstance(error, SupervisorInterrupted):
        return "SUPERVISOR_SIGNAL"
    return lifecycle.classify_failure(str(error))


def _write_terminal_failure(
    *,
    artifact_root: Path,
    event_writer: legacy.EventWriter,
    run_id: str,
    run_name: Any,
    manifest_path: Path,
    error: BaseException,
    outcomes: list[HardenedTaskOutcome],
    preflight_report: legacy.PreflightReport | None,
) -> dict[str, Any]:
    result = {
        "schema_version": 2,
        "run_id": run_id,
        "run_name": run_name,
        "source_manifest": str(manifest_path),
        "status": "fail",
        "failure_class": _failure_class(error),
        "signal": error.signum if isinstance(error, SupervisorInterrupted) else None,
        "error": str(error),
        "preflight": asdict(preflight_report) if preflight_report else None,
        "tasks": [_task_payload(item) for item in outcomes],
    }
    legacy._atomic_json(artifact_root / "supervisor-outcome.json", result)
    if preflight_report is None and not event_writer._terminal_written:
        event_writer.emit(
            "PREFLIGHT_FAILED",
            failure_class=result["failure_class"],
            error=str(error),
        )
    if not event_writer._terminal_written:
        event_writer.emit(
            "RUN_FAILED",
            failure_class=result["failure_class"],
            error=str(error),
            outcome_path=str(artifact_root / "supervisor-outcome.json"),
        )
    return result


def command_run(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.expanduser().resolve()
    source = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise HardenedSupervisorError("manifest root must be an object")
    tasks = source.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise HardenedSupervisorError("manifest tasks must be a non-empty list")
    if any(not isinstance(task, dict) or not task.get("key") for task in tasks):
        raise HardenedSupervisorError("every task must be an object with a key")
    supervisor = source.get("supervisor") or {}
    if not isinstance(supervisor, dict):
        raise HardenedSupervisorError("manifest supervisor must be an object")
    all_routes: list[legacy.Route] = []
    for task in tasks:
        all_routes.extend(legacy._routes_for_task(task, supervisor))
    for task in tasks:
        _route_policy(supervisor, legacy._routes_for_task(task, supervisor))

    raw_artifact_root = args.artifact_dir or (
        Path(str(source.get("workdir") or ".")) / "artifacts"
    )
    repository, run_dir, artifact_root = _validate_runtime_paths(
        source,
        artifact_root=raw_artifact_root.expanduser().resolve(),
    )
    source = dict(source)
    source["repo"] = str(repository)
    source["workdir"] = str(run_dir)
    environment = _runtime_environment(run_dir, source)
    source_snapshot = _source_snapshot(repository)
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    run_id = str(source.get("run_id") or f"{source.get('run_name', 'ringer')}-{uuid.uuid4().hex[:12]}")
    event_writer = legacy.EventWriter(artifact_root / "supervisor-events.jsonl", run_id)
    previous_handlers: dict[int, Any] = {}
    shutting_down = False

    def interrupt(signum: int, _frame: Any) -> None:
        nonlocal shutting_down
        if shutting_down:
            _kill_active_processes()
            return
        shutting_down = True
        _kill_active_processes()
        raise SupervisorInterrupted(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)

    outcomes: list[HardenedTaskOutcome] = []
    report: legacy.PreflightReport | None = None
    try:
        event_writer.emit("RUN_STARTED", manifest=str(manifest_path), tasks_total=len(tasks))
        report = preflight(
            source,
            supervisor=supervisor,
            routes=all_routes,
            artifact_root=artifact_root,
            environment=environment,
        )
        if _source_snapshot(repository) != source_snapshot:
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: source worktree changed during provider preflight"
            )
        baseline_sha = report.head_sha
        if not baseline_sha:
            raise HardenedSupervisorError("PREFLIGHT_FAILURE: baseline SHA is unavailable")
        legacy._atomic_json(artifact_root / "supervisor-preflight.json", asdict(report))
        event_writer.emit("PREFLIGHT_PASSED", **asdict(report))
        for task in tasks:
            outcome = supervise_task(
                source,
                task,
                args=args,
                supervisor=supervisor,
                event_writer=event_writer,
                provider_health=report.provider_health,
                baseline_sha=baseline_sha,
                environment=environment,
                source_snapshot=source_snapshot,
            )
            outcomes.append(outcome)
            if outcome.status != "pass":
                break
        passed = bool(outcomes) and all(item.status == "pass" for item in outcomes)
        failure_class = next(
            (item.failure_class for item in reversed(outcomes) if item.failure_class),
            None,
        )
        result = {
            "schema_version": 2,
            "run_id": run_id,
            "run_name": source.get("run_name"),
            "source_manifest": str(manifest_path),
            "status": "pass" if passed else "fail",
            "failure_class": failure_class,
            "preflight": asdict(report),
            "tasks": [_task_payload(item) for item in outcomes],
        }
        legacy._atomic_json(artifact_root / "supervisor-outcome.json", result)
        event_writer.emit(
            "RUN_COMPLETED" if passed else "RUN_FAILED",
            failure_class=failure_class,
            tasks_completed=len(outcomes),
            outcome_path=str(artifact_root / "supervisor-outcome.json"),
        )
        print(json.dumps(result, indent=2))
        return 0 if passed else 1
    except BaseException as error:
        _kill_active_processes()
        result = _write_terminal_failure(
            artifact_root=artifact_root,
            event_writer=event_writer,
            run_id=run_id,
            run_name=source.get("run_name"),
            manifest_path=manifest_path,
            error=error,
            outcomes=outcomes,
            preflight_report=report,
        )
        print(json.dumps(result, indent=2))
        if isinstance(error, SupervisorInterrupted):
            return 128 + error.signum
        return 2
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="supervise a hardened lifecycle manifest")
    run.add_argument("manifest", type=Path)
    run.add_argument(
        "--ringer",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "ringer.py",
    )
    run.add_argument("--config", type=Path)
    run.add_argument("--identity", default="ringer-supervisor-hardened")
    run.add_argument("--artifact-dir", type=Path)
    run.add_argument("--cleanup", action=argparse.BooleanOptionalAction, default=True)
    run.set_defaults(func=command_run)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except (OSError, ValueError, json.JSONDecodeError, HardenedSupervisorError) as error:
        print(f"ringer-supervisor-hardened: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
