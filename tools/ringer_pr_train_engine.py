#!/usr/bin/env python3
"""OpenCode capability probes, classification, and scrubbed evidence helpers."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any


ENGINE_RUNTIME_ERROR = "ENGINE_RUNTIME_ERROR"
CAPABILITY_KIND = "sandboxed_inference"
CAPABILITY_MARKER = "CPT_WORKER_CAPABILITY_OK"
CAPABILITY_TASK_PLACEHOLDER = "{{CAPABILITY_TASK_DIR}}"
ERROR_REF_RE = re.compile(r"\b(err_[A-Za-z0-9_-]+)\b")
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(
        r'(?i)(["\'](?:access_?token|refresh_?token|api_?key|password|secret|cookie)["\']\s*:\s*["\'])[^"\']+(["\'])'
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def scrub(text: str) -> str:
    result = SECRET_PATTERNS[0].sub(r"\1[REDACTED]", text)
    result = SECRET_PATTERNS[1].sub(r"\1[REDACTED]\2", result)
    return SECRET_PATTERNS[2].sub("[REDACTED]", result)


def engine_error(text: str) -> dict[str, Any] | None:
    lower = text.lower()
    if not (
        "unknownerror" in lower
        or "unexpected server error" in lower
        or ERROR_REF_RE.search(text)
    ):
        return None
    ref = ERROR_REF_RE.search(text)
    return {
        "engine": "opencode",
        "error_name": "UnknownError" if "unknownerror" in lower else "EngineRuntimeError",
        "error_ref": ref.group(1) if ref else None,
        "message": "Unexpected server error"
        if "unexpected server error" in lower
        else "OpenCode engine runtime error",
        "provider_stream_started": False,
        "provider_request_confirmed": False,
    }


def validate_probe(supervisor: dict[str, Any], route: Any, route_key: str, error_type: type[Exception]) -> dict[str, Any]:
    probes = supervisor.get("worker_capability_probes") or {}
    raw = probes.get(route_key)
    if not isinstance(raw, dict):
        raise error_type(
            f"PREFLIGHT_FAILURE: missing exact sandboxed worker capability probe for {route_key}"
        )
    argv = raw.get("argv")
    expected = raw.get("expected_output", CAPABILITY_MARKER)
    if (
        raw.get("kind") != CAPABILITY_KIND
        or not isinstance(argv, list)
        or len(argv) < 4
        or not all(isinstance(item, str) and item for item in argv)
        or not isinstance(expected, str)
        or not expected
    ):
        raise error_type(
            f"PREFLIGHT_FAILURE: invalid sandboxed worker capability probe for {route_key}"
        )
    if Path(argv[0]).name != "opencode-sandboxed.sh":
        raise error_type(
            f"PREFLIGHT_FAILURE: capability probe for {route_key} must use opencode-sandboxed.sh"
        )
    if CAPABILITY_TASK_PLACEHOLDER not in argv or "--no-sandbox" in [item.lower() for item in argv]:
        raise error_type(
            f"PREFLIGHT_FAILURE: capability probe for {route_key} must use a disposable sandboxed task"
        )
    model = route.model or ""
    exact = any(
        argv[index + 1] == model
        for index, item in enumerate(argv[:-1])
        if item in {"--model", "-m"}
    ) or any(item == f"--model={model}" for item in argv)
    if not exact:
        raise error_type(
            f"PREFLIGHT_FAILURE: capability probe for {route_key} does not select the exact model"
        )
    return {
        "argv": list(argv),
        "expected_output": expected,
        "timeout_seconds": float(raw.get("timeout_seconds", 120)),
    }


def run_probe(
    route: Any,
    route_key: str,
    probe: dict[str, Any],
    *,
    runtime_root: Path,
    artifact_root: Path,
    environment: dict[str, str],
    error_type: type[Exception],
) -> dict[str, Any]:
    task_dir = runtime_root / "capability-probes" / uuid.uuid4().hex
    task_dir.mkdir(parents=True, exist_ok=False)
    argv = [
        item.replace(CAPABILITY_TASK_PLACEHOLDER, str(task_dir))
        for item in probe["argv"]
    ]
    resolved = shutil.which(argv[0], path=environment.get("PATH"))
    if resolved is None or not os.access(resolved, os.X_OK):
        raise error_type(
            f"PREFLIGHT_FAILURE: capability wrapper cannot be resolved for {route_key}"
        )
    argv[0] = resolved
    process = subprocess.Popen(
        argv,
        cwd=task_dir,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    output = ""
    try:
        try:
            output, _ = process.communicate(timeout=probe["timeout_seconds"])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise error_type(
                f"PREFLIGHT_FAILURE: sandboxed worker capability probe timed out for {route_key}"
            )
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
    log_path = artifact_root / "worker-capability-probes" / (
        route_key.replace(":", "-").replace("/", "-") + ".log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(scrub(output), encoding="utf-8")
    if process.returncode != 0 or probe["expected_output"] not in output:
        raise error_type(
            f"PREFLIGHT_FAILURE: sandboxed worker capability probe failed for {route_key}"
        )
    return {
        "healthy": True,
        "kind": CAPABILITY_KIND,
        "engine": route.engine,
        "model": route.model,
        "log_path": str(log_path),
    }


def collect_logs(runtime_root: Path, artifact_root: Path) -> tuple[list[str], str]:
    target = artifact_root / "engine-logs"
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    combined: list[str] = []
    for index, source in enumerate(sorted(runtime_root.rglob("*.log")), start=1):
        if not source.is_file() or artifact_root in source.parents:
            continue
        try:
            text = scrub(source.read_bytes()[-1024 * 1024 :].decode("utf-8", errors="replace"))
        except OSError:
            continue
        destination = target / f"{index:03d}-{source.name}"
        destination.write_text(text, encoding="utf-8")
        copied.append(str(destination))
        combined.append(text)
    return copied, "\n".join(combined)


def enrich_outcome(artifact_root: Path, log_paths: list[str], combined: str, atomic_json: Any) -> None:
    path = artifact_root / "supervisor-outcome.json"
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    payload["engine_logs"] = log_paths
    parsed = engine_error(combined + "\n" + str(payload.get("error") or ""))
    for task in payload.get("tasks") or []:
        for attempt in task.get("attempts") or []:
            if attempt.get("failure_class") == ENGINE_RUNTIME_ERROR and parsed:
                attempt["engine_error"] = parsed
    atomic_json(path, payload)
