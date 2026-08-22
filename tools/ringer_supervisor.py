#!/usr/bin/env python3
"""Deterministic supervisor for Ringer lifecycle runs.

The supervisor owns preflight, provider routing, process waiting, timeout
handling, structured JSONL events, and canonical outcomes. It deliberately
keeps model judgment out of process control: an orchestrator launches this
command once and consumes ``supervisor-outcome.json`` when it exits.

The input is the JSON lifecycle manifest accepted by ``ringer_lifecycle.py``.
An optional top-level ``supervisor`` object may define routing and health
checks without changing Ringer's manifest schema::

    {
      "supervisor": {
        "base_ref": "origin/main",
        "minimum_free_bytes": 5368709120,
        "minimum_free_inodes": 100000,
        "heartbeat_seconds": 60,
        "fallback_on": ["PROVIDER_TIMEOUT", "CHECK_FAILURE", "MISSING_EXPORT"],
        "routes": [
          {"engine": "opencode", "model": "minimax-coding-plan/MiniMax-M3"},
          {"engine": "grok", "model": "grok-4.6"},
          {"engine": "agy", "model": "gemini-3.7-flash-high"}
        ],
        "provider_probes": {
          "grok": {"argv": ["grok", "--version"], "timeout_seconds": 60}
        }
      }
    }

Only the Python standard library is required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse Ringer's canonical lifecycle helpers instead of creating a second
# definition of worktree ownership, patch sealing, and failure classification.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ringer_lifecycle as lifecycle  # noqa: E402


DEFAULT_FALLBACK_ON = frozenset(
    {
        "PROVIDER_QUOTA",
        "PROVIDER_TIMEOUT",
        "NETWORK_SANDBOX",
        "CHECK_FAILURE",
        "MISSING_EXPORT",
        "COORDINATOR_ERROR",
        "NO_PROGRESS",
        "MALFORMED_OUTCOME",
    }
)
TERMINAL_EVENTS = frozenset({"RUN_COMPLETED", "RUN_FAILED"})
ALLOWED_EVENTS = frozenset(
    {
        "RUN_STARTED",
        "PREFLIGHT_PASSED",
        "PREFLIGHT_FAILED",
        "WORKER_STARTED",
        "WORKER_HEARTBEAT",
        "WORKER_SUCCEEDED",
        "WORKER_FAILED",
        "WORKER_TIMED_OUT",
        "EXPECTED_ARTIFACT_MISSING",
        "RUN_COMPLETED",
        "RUN_FAILED",
    }
)


class SupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class Route:
    engine: str
    model: str | None = None
    timeout_seconds: float = 900.0

    @classmethod
    def from_raw(cls, value: Any) -> "Route":
        if not isinstance(value, dict) or not value.get("engine"):
            raise SupervisorError("every supervisor route must contain an engine")
        timeout = float(value.get("timeout_seconds", 900))
        if timeout <= 0:
            raise SupervisorError("route timeout_seconds must be positive")
        model = value.get("model")
        return cls(
            engine=str(value["engine"]),
            model=str(model) if model is not None else None,
            timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class AttemptOutcome:
    task_key: str
    attempt: int
    engine: str
    model: str | None
    status: str
    failure_class: str | None
    duration_seconds: float
    returncode: int | None
    changed_paths: list[str]
    missing_artifacts: list[str]
    log_path: str


@dataclass(frozen=True)
class TaskOutcome:
    key: str
    status: str
    attempts: list[AttemptOutcome]
    selected_engine: str | None
    selected_model: str | None
    failure_class: str | None
    patch_path: str | None
    patch_sha256: str | None
    artifact_dir: str


class EventWriter:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._terminal_written = False
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, **payload: Any) -> None:
        if event_type not in ALLOWED_EVENTS:
            raise SupervisorError(f"unsupported supervisor event: {event_type}")
        if self._terminal_written:
            raise SupervisorError("cannot emit events after the terminal event")
        if event_type in TERMINAL_EVENTS:
            self._terminal_written = True
        event = {
            "schema_version": 1,
            "event_id": uuid.uuid4().hex,
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


@dataclass(frozen=True)
class PreflightReport:
    repository: str | None
    base_ref: str | None
    head_sha: str | None
    common_ancestor: str | None
    free_bytes: int
    free_inodes: int
    provider_health: dict[str, dict[str, Any]]


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(argv, 124, output + "\nprobe timed out")


def _git(
    repo: Path,
    *args: str,
    timeout_seconds: float = 60,
) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo), *args], timeout_seconds=timeout_seconds)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _free_inodes(path: Path) -> int:
    stats = os.statvfs(path)
    return int(stats.f_favail)


def _probe_provider(
    engine: str,
    raw: Any,
    *,
    cwd: Path,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SupervisorError(f"provider probe for {engine} must be an object")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise SupervisorError(f"provider probe for {engine} requires a non-empty argv list")
    timeout = float(raw.get("timeout_seconds", 60))
    result = _run(argv, cwd=cwd, timeout_seconds=timeout)
    return {
        "healthy": result.returncode == 0,
        "returncode": result.returncode,
        "output_tail": (result.stdout or "")[-1000:],
    }


def preflight(
    source: dict[str, Any],
    *,
    supervisor: dict[str, Any],
    routes: list[Route],
) -> PreflightReport:
    run_dir = Path(str(source.get("workdir") or ".")).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(run_dir)
    free_inodes = _free_inodes(run_dir)
    minimum_bytes = int(supervisor.get("minimum_free_bytes", 1024 * 1024 * 1024))
    minimum_inodes = int(supervisor.get("minimum_free_inodes", 10_000))
    if usage.free < minimum_bytes:
        raise SupervisorError(
            f"preflight free bytes {usage.free} is below required {minimum_bytes}"
        )
    if free_inodes < minimum_inodes:
        raise SupervisorError(
            f"preflight free inodes {free_inodes} is below required {minimum_inodes}"
        )

    repository: Path | None = None
    head_sha: str | None = None
    common_ancestor: str | None = None
    repo_raw = source.get("repo")
    base_ref_raw = supervisor.get("base_ref")
    base_ref = str(base_ref_raw) if base_ref_raw else None
    if repo_raw:
        repository = Path(str(repo_raw)).expanduser().resolve()
        probe = _git(repository, "rev-parse", "--show-toplevel")
        if probe.returncode != 0:
            raise SupervisorError(f"repo is not a git checkout: {repository}")
        head = _git(repository, "rev-parse", "HEAD")
        if head.returncode != 0:
            raise SupervisorError(f"could not resolve repository HEAD: {head.stdout.strip()}")
        head_sha = head.stdout.strip()
        if base_ref:
            merge_base = _git(repository, "merge-base", base_ref, "HEAD")
            if merge_base.returncode != 0 or not merge_base.stdout.strip():
                raise SupervisorError(
                    f"repository HEAD has no common ancestor with {base_ref}: "
                    f"{merge_base.stdout.strip()}"
                )
            common_ancestor = merge_base.stdout.strip()
        write_probe = repository / f".ringer-supervisor-write-{uuid.uuid4().hex}"
        try:
            write_probe.write_text("preflight\n", encoding="utf-8")
        except OSError as error:
            raise SupervisorError(f"repository is not writable: {repository}: {error}") from error
        finally:
            write_probe.unlink(missing_ok=True)

    provider_health: dict[str, dict[str, Any]] = {}
    probes = supervisor.get("provider_probes") or {}
    if not isinstance(probes, dict):
        raise SupervisorError("supervisor.provider_probes must be an object")
    for route in routes:
        if route.engine in provider_health or route.engine not in probes:
            continue
        health = _probe_provider(route.engine, probes[route.engine], cwd=run_dir)
        provider_health[route.engine] = health
    healthy_routes = [
        route
        for route in routes
        if route.engine not in provider_health or provider_health[route.engine]["healthy"]
    ]
    if not healthy_routes:
        raise SupervisorError("all configured worker routes failed provider health probes")

    return PreflightReport(
        repository=str(repository) if repository else None,
        base_ref=base_ref,
        head_sha=head_sha,
        common_ancestor=common_ancestor,
        free_bytes=usage.free,
        free_inodes=free_inodes,
        provider_health=provider_health,
    )


def _routes_for_task(task: dict[str, Any], supervisor: dict[str, Any]) -> list[Route]:
    raw_routes = task.get("routes") or supervisor.get("routes")
    routes: list[Route] = []
    if raw_routes:
        if not isinstance(raw_routes, list):
            raise SupervisorError("supervisor routes must be a list")
        routes.extend(Route.from_raw(item) for item in raw_routes)
    else:
        engine = task.get("engine")
        if not engine:
            raise SupervisorError(
                f"task {task.get('key')!r} needs engine or supervisor.routes"
            )
        routes.append(
            Route(
                engine=str(engine),
                model=str(task["model"]) if task.get("model") is not None else None,
                timeout_seconds=float(task.get("timeout_seconds", 900)),
            )
        )
    return routes


def _expected_paths(
    task: dict[str, Any],
    *,
    run_dir: Path,
    task_dir: Path,
    artifact_dir: Path,
    repository: Path | None,
    attempt: int,
) -> list[Path]:
    variables = lifecycle.canonical_values(
        run_dir=run_dir,
        task_dir=task_dir,
        artifact_dir=artifact_dir,
        source_repo=repository,
        attempt=attempt,
    )
    result: list[Path] = []
    for raw in task.get("expect_files") or []:
        value = lifecycle.substitute(str(raw), variables)
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = task_dir / path
        path = path.resolve()
        if task_dir != path and task_dir not in path.parents and artifact_dir not in path.parents:
            raise SupervisorError(f"expected artifact escapes task/artifact directories: {path}")
        result.append(path)
    return result


def _attempt_manifest(
    source: dict[str, Any],
    task: dict[str, Any],
    route: Route,
    *,
    run_dir: Path,
    task_dir: Path,
    artifact_dir: Path,
    repository: Path | None,
    attempt: int,
) -> dict[str, Any]:
    variables = lifecycle.canonical_values(
        run_dir=run_dir,
        task_dir=task_dir,
        artifact_dir=artifact_dir,
        source_repo=repository,
        attempt=attempt,
    )
    transformed = dict(task)
    transformed.pop("routes", None)
    transformed["engine"] = route.engine
    if route.model is None:
        transformed.pop("model", None)
    else:
        transformed["model"] = route.model
    transformed["max_attempts"] = 1
    transformed["check"] = lifecycle.normalize_check(task.get("check"), variables)
    transformed["expect_files"] = [
        lifecycle.substitute(str(item), variables)
        for item in task.get("expect_files") or []
    ]
    transformed["spec"] = lifecycle.substitute(str(task.get("spec", "")), variables)
    transformed.pop("durable_output", None)
    return {
        "run_name": str(source["run_name"]),
        "workdir": str(run_dir),
        "max_parallel": 1,
        "worktrees": False,
        "repo": None,
        "tasks": [transformed],
    }


def _changed_paths(task_dir: Path) -> list[str]:
    try:
        return lifecycle.dirty_paths(task_dir)
    except Exception:
        return []


def _run_worker(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    heartbeat_seconds: float,
    no_progress_seconds: float,
    event_writer: EventWriter,
    task_key: str,
    attempt: int,
    route: Route,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> tuple[int | None, float, str | None]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stop_reason: str | None = None
    no_progress_seconds = max(heartbeat_seconds, no_progress_seconds)
    last_progress = started
    last_signature: tuple[tuple[str, ...], int] | None = None
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=env,
        )
        next_heartbeat = started + heartbeat_seconds
        while process.poll() is None:
            now = time.monotonic()
            if now - started >= timeout_seconds:
                stop_reason = "timeout"
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                break
            if now >= next_heartbeat:
                changed_paths = tuple(_changed_paths(cwd))
                log_bytes = log_path.stat().st_size if log_path.exists() else 0
                signature = (changed_paths, log_bytes)
                if signature != last_signature:
                    last_signature = signature
                    last_progress = now
                if now - last_progress >= no_progress_seconds:
                    stop_reason = "no_progress"
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
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
    returncode = process.returncode
    return returncode, time.monotonic() - started, stop_reason


def _read_tail(path: Path, limit: int = 16_000) -> str:
    if not path.is_file():
        return ""
    raw = path.read_bytes()
    return raw[-limit:].decode("utf-8", errors="replace")


def supervise_task(
    source: dict[str, Any],
    task: dict[str, Any],
    *,
    args: argparse.Namespace,
    supervisor: dict[str, Any],
    event_writer: EventWriter,
    provider_health: dict[str, dict[str, Any]],
    worker_env: dict[str, str] | None = None,
    base_ref: str = "HEAD",
) -> TaskOutcome:
    key = str(task["key"])
    run_dir = Path(str(source.get("workdir") or ".")).expanduser().resolve()
    artifact_root = (args.artifact_dir or (run_dir / "artifacts")).expanduser().resolve()
    artifact_dir = artifact_root / key
    task_dir = (run_dir / key).resolve()
    repository = (
        Path(str(source["repo"])).expanduser().resolve() if source.get("repo") else None
    )
    if run_dir != task_dir and run_dir not in task_dir.parents:
        raise SupervisorError(f"task key escapes run directory: {key}")
    if repository:
        lifecycle.ensure_owned_worktree(
            repository,
            task_dir,
            artifact_dir,
            key,
            base_ref=base_ref,
        )
    else:
        task_dir.mkdir(parents=True, exist_ok=True)

    routes = _routes_for_task(task, supervisor)
    fallback_on = set(supervisor.get("fallback_on") or DEFAULT_FALLBACK_ON)
    heartbeat_seconds = float(supervisor.get("heartbeat_seconds", 60))
    attempts: list[AttemptOutcome] = []
    selected: Route | None = None
    final_failure: str | None = None

    for attempt, route in enumerate(routes, start=1):
        health = provider_health.get(route.engine)
        if health is not None and not health.get("healthy"):
            failure_class = "PROVIDER_UNHEALTHY"
            attempts.append(
                AttemptOutcome(
                    task_key=key,
                    attempt=attempt,
                    engine=route.engine,
                    model=route.model,
                    status="skipped",
                    failure_class=failure_class,
                    duration_seconds=0.0,
                    returncode=None,
                    changed_paths=_changed_paths(task_dir),
                    missing_artifacts=[],
                    log_path="",
                )
            )
            final_failure = failure_class
            continue

        if attempt > 1:
            lifecycle.rotate_attempt_artifacts(
                task_dir,
                artifact_dir,
                task.get("expect_files") or [],
                attempt,
            )
        manifest = _attempt_manifest(
            source,
            task,
            route,
            run_dir=run_dir,
            task_dir=task_dir,
            artifact_dir=artifact_dir,
            repository=repository,
            attempt=attempt,
        )
        manifest_path = artifact_dir / f"attempt-{attempt:03d}-manifest.json"
        _atomic_json(manifest_path, manifest)
        log_path = artifact_dir / f"attempt-{attempt:03d}" / "ringer-output.log"
        command = lifecycle.ringer_command(
            args.ringer.expanduser().resolve(),
            args.config.expanduser().resolve() if args.config else None,
            manifest_path,
            args.identity,
        )
        event_writer.emit(
            "WORKER_STARTED",
            task_id=key,
            attempt=attempt,
            engine=route.engine,
            model=route.model,
            timeout_seconds=route.timeout_seconds,
        )
        returncode, duration, stop_reason = _run_worker(
            command,
            cwd=task_dir,
            timeout_seconds=route.timeout_seconds,
            heartbeat_seconds=heartbeat_seconds,
            no_progress_seconds=float(supervisor.get("no_progress_seconds", 600)),
            event_writer=event_writer,
            task_key=key,
            attempt=attempt,
            route=route,
            log_path=log_path,
            env=worker_env,
        )
        lifecycle.copy_worker_log(task_dir, artifact_dir, attempt)
        expected = _expected_paths(
            task,
            run_dir=run_dir,
            task_dir=task_dir,
            artifact_dir=artifact_dir,
            repository=repository,
            attempt=attempt,
        )
        missing = [str(path) for path in expected if not path.is_file()]
        output_tail = _read_tail(log_path)
        if stop_reason == "timeout":
            failure_class = "PROVIDER_TIMEOUT"
            status = "timeout"
            event_type = "WORKER_TIMED_OUT"
        elif stop_reason == "no_progress":
            failure_class = "NO_PROGRESS"
            status = "failed"
            event_type = "WORKER_FAILED"
        elif returncode == 0 and missing:
            failure_class = "MISSING_EXPORT"
            status = "failed"
            event_type = "EXPECTED_ARTIFACT_MISSING"
        elif returncode == 0:
            failure_class = None
            status = "succeeded"
            event_type = "WORKER_SUCCEEDED"
        else:
            failure_class = lifecycle.classify_failure(
                output_tail, returncode=returncode
            )
            status = "failed"
            event_type = "WORKER_FAILED"
        changed = _changed_paths(task_dir)
        attempt_outcome = AttemptOutcome(
            task_key=key,
            attempt=attempt,
            engine=route.engine,
            model=route.model,
            status=status,
            failure_class=failure_class,
            duration_seconds=round(duration, 3),
            returncode=returncode,
            changed_paths=changed,
            missing_artifacts=missing,
            log_path=str(log_path),
        )
        attempts.append(attempt_outcome)
        event_writer.emit(
            event_type,
            task_id=key,
            attempt=attempt,
            engine=route.engine,
            model=route.model,
            failure_class=failure_class,
            returncode=returncode,
            duration_seconds=round(duration, 3),
            changed_paths=changed,
            missing_artifacts=missing,
            log_path=str(log_path),
        )
        if failure_class is None:
            selected = route
            final_failure = None
            break
        final_failure = failure_class
        if failure_class not in fallback_on:
            break

    patch: Path | None = None
    if selected is not None and repository:
        patch = lifecycle.export_worktree_patch(
            task_dir,
            artifact_dir / "worktree.patch",
            source_repo=repository,
            base_sha=base_ref if base_ref != "HEAD" else None,
        )
        if lifecycle.dirty_paths(task_dir) and patch is None:
            selected = None
            final_failure = "MISSING_EXPORT"

    status = "pass" if selected is not None else "fail"
    outcome = TaskOutcome(
        key=key,
        status=status,
        attempts=attempts,
        selected_engine=selected.engine if selected else None,
        selected_model=selected.model if selected else None,
        failure_class=final_failure,
        patch_path=str(patch) if patch else None,
        patch_sha256=_sha256(patch) if patch else None,
        artifact_dir=str(artifact_dir),
    )
    if status == "pass" and repository and args.cleanup:
        lifecycle.cleanup_owned_worktree(repository, task_dir)
    return outcome


def _task_payload(outcome: TaskOutcome) -> dict[str, Any]:
    payload = asdict(outcome)
    payload["attempts"] = [asdict(item) for item in outcome.attempts]
    return payload


def command_run(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.expanduser().resolve()
    source = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise SupervisorError("manifest root must be an object")
    tasks = source.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise SupervisorError("manifest tasks must be a non-empty list")
    if any(not isinstance(task, dict) or not task.get("key") for task in tasks):
        raise SupervisorError("every task must be an object with a key")
    supervisor = source.get("supervisor") or {}
    if not isinstance(supervisor, dict):
        raise SupervisorError("manifest supervisor must be an object")
    all_routes: list[Route] = []
    for task in tasks:
        all_routes.extend(_routes_for_task(task, supervisor))

    run_dir = Path(str(source.get("workdir") or ".")).expanduser().resolve()
    artifact_root = (args.artifact_dir or (run_dir / "artifacts")).expanduser().resolve()
    generated_run_id = f"{source.get('run_name', 'ringer')}-{uuid.uuid4().hex[:12]}"
    run_id = str(source.get("run_id") or generated_run_id)
    event_writer = EventWriter(artifact_root / "supervisor-events.jsonl", run_id)
    event_writer.emit(
        "RUN_STARTED",
        manifest=str(manifest_path),
        tasks_total=len(tasks),
    )
    try:
        report = preflight(source, supervisor=supervisor, routes=all_routes)
        _atomic_json(artifact_root / "supervisor-preflight.json", asdict(report))
        event_writer.emit("PREFLIGHT_PASSED", **asdict(report))
    except Exception as error:
        failure_class = lifecycle.classify_failure(str(error))
        event_writer.emit(
            "PREFLIGHT_FAILED",
            failure_class=failure_class,
            error=str(error),
        )
        result = {
            "schema_version": 1,
            "run_id": run_id,
            "run_name": source.get("run_name"),
            "status": "fail",
            "failure_class": failure_class,
            "error": str(error),
            "tasks": [],
        }
        _atomic_json(artifact_root / "supervisor-outcome.json", result)
        event_writer.emit("RUN_FAILED", failure_class=failure_class, error=str(error))
        print(json.dumps(result, indent=2))
        return 2

    outcomes: list[TaskOutcome] = []
    for task in tasks:
        outcome = supervise_task(
            source,
            task,
            args=args,
            supervisor=supervisor,
            event_writer=event_writer,
            provider_health=report.provider_health,
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
        "schema_version": 1,
        "run_id": run_id,
        "run_name": source.get("run_name"),
        "source_manifest": str(manifest_path),
        "status": "pass" if passed else "fail",
        "failure_class": failure_class,
        "preflight": asdict(report),
        "tasks": [_task_payload(item) for item in outcomes],
    }
    _atomic_json(artifact_root / "supervisor-outcome.json", result)
    event_writer.emit(
        "RUN_COMPLETED" if passed else "RUN_FAILED",
        failure_class=failure_class,
        tasks_completed=len(outcomes),
        outcome_path=str(artifact_root / "supervisor-outcome.json"),
    )
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="supervise a lifecycle manifest")
    run.add_argument("manifest", type=Path)
    run.add_argument(
        "--ringer",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "ringer.py",
    )
    run.add_argument("--config", type=Path)
    run.add_argument("--identity", default="ringer-supervisor")
    run.add_argument("--artifact-dir", type=Path)
    run.add_argument("--cleanup", action=argparse.BooleanOptionalAction, default=True)
    run.set_defaults(func=command_run)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except (
        SupervisorError,
        lifecycle.LifecycleError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"ringer-supervisor: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
