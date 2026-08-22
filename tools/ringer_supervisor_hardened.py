#!/usr/bin/env python3
"""Fail-closed Ringer supervisor for PR-train implementation runs.

This entrypoint layers strict role routing, fresh attempt worktrees, supervisor-
stamped provenance, independent objective checks, disjoint runtime paths and
terminal outcome guarantees on top of :mod:`ringer_supervisor`.

It intentionally leaves the legacy supervisor available for compatibility.
PR-train implementation manifests should use this entrypoint.
"""
from __future__ import annotations

import argparse
import json
import os
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
    model: str | None
    status: str
    failure_class: str | None
    baseline_sha: str | None
    worktree: str
    provenance_path: str
    patch_path: str | None
    patch_sha256: str | None
    objective_checks: list[ObjectiveCheckOutcome]
    worker: dict[str, Any]


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


def _is_within(path: Path, parent: Path) -> bool:
    path = path.resolve()
    parent = parent.resolve()
    return path == parent or parent in path.parents


def _route_policy(supervisor: dict[str, Any], routes: list[legacy.Route]) -> None:
    allowed_engines = {
        str(item).strip().lower()
        for item in supervisor.get("allowed_implementation_engines", ["opencode"])
        if str(item).strip()
    }
    allowed_model_markers = [
        str(item).strip().lower()
        for item in supervisor.get("allowed_implementation_model_markers", ["minimax"])
        if str(item).strip()
    ]
    if not allowed_engines:
        raise HardenedSupervisorError(
            "MANIFEST_POLICY_FAILURE: allowed implementation engines cannot be empty"
        )
    for route in routes:
        engine = route.engine.strip().lower()
        model = (route.model or "").strip().lower()
        if engine not in allowed_engines:
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: implementation route "
                f"{route.engine!r} is not allowed; permitted={sorted(allowed_engines)}"
            )
        if allowed_model_markers and not any(marker in model for marker in allowed_model_markers):
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: implementation model "
                f"{route.model!r} does not match the allowed model markers"
            )


def _provider_probe_policy(
    supervisor: dict[str, Any], routes: list[legacy.Route]
) -> None:
    if not bool(supervisor.get("require_inference_probes", True)):
        return
    probes = supervisor.get("provider_probes") or {}
    for route in routes:
        raw = probes.get(route.engine)
        if not isinstance(raw, dict):
            raise HardenedSupervisorError(
                "PREFLIGHT_FAILURE: missing inference provider probe for "
                f"{route.engine}"
            )
        argv = raw.get("argv")
        kind = str(raw.get("kind") or "").lower()
        if kind != "inference" or not isinstance(argv, list) or not argv:
            raise HardenedSupervisorError(
                "PREFLIGHT_FAILURE: provider probe must declare kind=inference "
                f"and a non-empty argv list for {route.engine}"
            )
        lowered = {str(item).strip().lower() for item in argv}
        if lowered & {"--version", "version", "--help", "help"}:
            raise HardenedSupervisorError(
                "PREFLIGHT_FAILURE: version/help checks are not valid inference "
                f"probes for {route.engine}"
            )


def preflight(
    source: dict[str, Any],
    *,
    supervisor: dict[str, Any],
    routes: list[legacy.Route],
    artifact_root: Path,
) -> legacy.PreflightReport:
    _route_policy(supervisor, routes)
    _provider_probe_policy(supervisor, routes)
    report = legacy.preflight(source, supervisor=supervisor, routes=routes)
    if report.repository:
        repository = Path(report.repository).resolve()
        run_dir = Path(str(source.get("workdir") or ".")).expanduser().resolve()
        if _is_within(run_dir, repository):
            raise HardenedSupervisorError(
                "RUNTIME_PATH_ESCAPE: workdir must be outside the source repository"
            )
        if _is_within(artifact_root, repository):
            raise HardenedSupervisorError(
                "RUNTIME_PATH_ESCAPE: artifact root must be outside the source repository"
            )
    return report


def _objective_check_argvs(
    task: dict[str, Any],
    *,
    run_dir: Path,
    task_dir: Path,
    artifact_dir: Path,
    repository: Path | None,
    attempt: int,
) -> list[list[str]]:
    raw_checks = task.get("objective_checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise HardenedSupervisorError(
            "MANIFEST_POLICY_FAILURE: every implementation task requires a "
            "non-empty objective_checks list"
        )
    variables = lifecycle.canonical_values(
        run_dir=run_dir,
        task_dir=task_dir,
        artifact_dir=artifact_dir,
        source_repo=repository,
        attempt=attempt,
    )
    result: list[list[str]] = []
    report_names = {name.lower() for name in lifecycle.REPORT_NAMES}
    for index, raw in enumerate(raw_checks, start=1):
        if not isinstance(raw, dict) or set(raw) != {"argv"}:
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: objective_checks entries must contain "
                "only an argv list"
            )
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) and item for item in argv
        ):
            raise HardenedSupervisorError(
                f"MANIFEST_POLICY_FAILURE: objective check {index} has invalid argv"
            )
        resolved = [lifecycle.substitute(item, variables) for item in argv]
        referenced_names = {Path(item).name.lower() for item in resolved}
        if referenced_names & report_names:
            raise HardenedSupervisorError(
                "MANIFEST_POLICY_FAILURE: objective checks cannot derive PASS from "
                "worker-authored report files"
            )
        result.append(resolved)
    return result


def _run_objective_checks(
    checks: list[list[str]],
    *,
    cwd: Path,
    artifact_dir: Path,
    timeout_seconds: float,
) -> list[ObjectiveCheckOutcome]:
    outcomes: list[ObjectiveCheckOutcome] = []
    for index, argv in enumerate(checks, start=1):
        log_path = artifact_dir / f"objective-check-{index:03d}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout_seconds,
            )
            output = result.stdout or ""
            returncode = int(result.returncode)
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            output += "\nobjective check timed out\n"
            returncode = 124
        log_path.write_text(output, encoding="utf-8")
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
    def __init__(
        self,
        delegate: legacy.EventWriter,
        *,
        task_key: str,
        attempt: int,
    ) -> None:
        self.delegate = delegate
        self.task_key = task_key
        self.attempt = attempt

    def emit(self, event_type: str, **payload: Any) -> None:
        if "task_id" in payload:
            payload["task_id"] = self.task_key
        if "attempt" in payload:
            payload["attempt"] = self.attempt
        self.delegate.emit(event_type, **payload)


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


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
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            next_heartbeat = started + heartbeat_seconds
            while process.poll() is None:
                now = time.monotonic()
                if now - started >= timeout_seconds:
                    stop_reason = "timeout"
                    _terminate_process_group(process)
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
                        _terminate_process_group(process)
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
            _terminate_process_group(process)
        raise


legacy._run_worker = _run_worker_safe


def _attempt_payload(outcome: HardenedAttemptOutcome) -> dict[str, Any]:
    payload = asdict(outcome)
    payload["objective_checks"] = [asdict(item) for item in outcome.objective_checks]
    return payload


def _task_payload(outcome: HardenedTaskOutcome) -> dict[str, Any]:
    payload = asdict(outcome)
    payload["attempts"] = [_attempt_payload(item) for item in outcome.attempts]
    return payload


def supervise_task(
    source: dict[str, Any],
    task: dict[str, Any],
    *,
    args: argparse.Namespace,
    supervisor: dict[str, Any],
    event_writer: legacy.EventWriter,
    provider_health: dict[str, dict[str, Any]],
) -> HardenedTaskOutcome:
    key = str(task["key"])
    run_dir = Path(str(source.get("workdir") or ".")).expanduser().resolve()
    artifact_root = (args.artifact_dir or (run_dir / "artifacts")).expanduser().resolve()
    artifact_dir = artifact_root / key
    repository = (
        Path(str(source["repo"])).expanduser().resolve() if source.get("repo") else None
    )
    routes = legacy._routes_for_task(task, supervisor)
    _route_policy(supervisor, routes)
    fallback_on = set(supervisor.get("fallback_on") or legacy.DEFAULT_FALLBACK_ON)
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
        effective_args = replace(args, cleanup=False) if hasattr(args, "__dataclass_fields__") else args
        if effective_args is args:
            effective_args = argparse.Namespace(**vars(args))
            effective_args.cleanup = False
        attempt_events = _AttemptEventWriter(
            event_writer,
            task_key=key,
            attempt=attempt,
        )
        worker_outcome = legacy.supervise_task(
            source,
            effective_task,
            args=effective_args,
            supervisor=supervisor,
            event_writer=attempt_events,
            provider_health=provider_health,
        )
        worktree = (run_dir / effective_key).resolve()
        baseline_sha = None
        if repository and worktree.exists():
            head = legacy._git(worktree, "rev-parse", "HEAD")
            if head.returncode == 0:
                baseline_sha = head.stdout.strip()
        objective_outcomes: list[ObjectiveCheckOutcome] = []
        failure_class = worker_outcome.failure_class
        status = worker_outcome.status
        if worker_outcome.status == "pass":
            checks = _objective_check_argvs(
                task,
                run_dir=run_dir,
                task_dir=worktree,
                artifact_dir=artifact_dir,
                repository=repository,
                attempt=attempt,
            )
            objective_outcomes = _run_objective_checks(
                checks,
                cwd=worktree,
                artifact_dir=artifact_dir / f"attempt-{attempt:03d}",
                timeout_seconds=objective_timeout,
            )
            if not objective_outcomes or any(item.status != "pass" for item in objective_outcomes):
                status = "fail"
                failure_class = "CHECK_FAILURE"
        provenance_path = artifact_dir / f"attempt-{attempt:03d}-provenance.json"
        provenance = {
            "schema_version": 1,
            "task_key": key,
            "effective_task_key": effective_key,
            "attempt": attempt,
            "engine": route.engine,
            "model": route.model,
            "baseline_sha": baseline_sha,
            "worktree": str(worktree),
            "worker_status": worker_outcome.status,
            "objective_checks": [asdict(item) for item in objective_outcomes],
            "patch_path": worker_outcome.patch_path,
            "patch_sha256": worker_outcome.patch_sha256,
        }
        legacy._atomic_json(provenance_path, provenance)
        attempt_outcome = HardenedAttemptOutcome(
            attempt=attempt,
            engine=route.engine,
            model=route.model,
            status=status,
            failure_class=failure_class,
            baseline_sha=baseline_sha,
            worktree=str(worktree),
            provenance_path=str(provenance_path),
            patch_path=worker_outcome.patch_path,
            patch_sha256=worker_outcome.patch_sha256,
            objective_checks=objective_outcomes,
            worker=legacy._task_payload(worker_outcome),
        )
        attempts.append(attempt_outcome)
        if status == "pass":
            selected_engine = route.engine
            selected_model = route.model
            selected_patch = worker_outcome.patch_path
            selected_patch_sha = worker_outcome.patch_sha256
            final_failure = None
            if repository and args.cleanup:
                lifecycle.cleanup_owned_worktree(repository, worktree)
            break
        final_failure = failure_class
        if repository and worktree.exists():
            lifecycle.cleanup_owned_worktree(repository, worktree)
        if failure_class not in fallback_on:
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
    failure_class = lifecycle.classify_failure(str(error))
    result = {
        "schema_version": 2,
        "run_id": run_id,
        "run_name": run_name,
        "source_manifest": str(manifest_path),
        "status": "fail",
        "failure_class": failure_class,
        "error": str(error),
        "preflight": asdict(preflight_report) if preflight_report else None,
        "tasks": [_task_payload(item) for item in outcomes],
    }
    legacy._atomic_json(artifact_root / "supervisor-outcome.json", result)
    if not event_writer._terminal_written:
        event_writer.emit(
            "RUN_FAILED",
            failure_class=failure_class,
            error=str(error),
            tasks_completed=len(outcomes),
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

    run_dir = Path(str(source.get("workdir") or ".")).expanduser().resolve()
    artifact_root = (args.artifact_dir or (run_dir / "artifacts")).expanduser().resolve()
    run_id = str(
        source.get("run_id")
        or f"{source.get('run_name', 'ringer')}-{uuid.uuid4().hex[:12]}"
    )
    event_writer = legacy.EventWriter(artifact_root / "supervisor-events.jsonl", run_id)
    event_writer.emit("RUN_STARTED", manifest=str(manifest_path), tasks_total=len(tasks))
    outcomes: list[HardenedTaskOutcome] = []
    report: legacy.PreflightReport | None = None
    previous_handlers: dict[int, Any] = {}

    def interrupt(signum: int, _frame: Any) -> None:
        raise SupervisorInterrupted(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)

    try:
        report = preflight(
            source,
            supervisor=supervisor,
            routes=all_routes,
            artifact_root=artifact_root,
        )
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
        return 130 if isinstance(error, SupervisorInterrupted) else 2
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
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ringer-supervisor-hardened: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
