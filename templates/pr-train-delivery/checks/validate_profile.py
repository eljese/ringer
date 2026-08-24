#!/usr/bin/env python3
"""Validate the static safety properties of a PR-train delivery manifest."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

PROFILE_NAME = "pr-train-delivery"
PROFILE_VERSION = 1
FORBIDDEN_WORKER_ACTIONS = re.compile(
    r"\b(?:git\s+(?:add|commit|push)|gh\s+pr\s+create|create\s+(?:a\s+)?pull request)\b",
    re.IGNORECASE,
)
WEAK_CHECKS = {"true", "exit 0", "echo done", ":"}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate(profile_path: Path, manifest_path: Path) -> list[str]:
    errors: list[str] = []
    profile = _load(profile_path)
    manifest = _load(manifest_path)

    if profile.get("name") != PROFILE_NAME or profile.get("version") != PROFILE_VERSION:
        errors.append("profile identity/version mismatch")
    if profile.get("promotion_owner") != "codex-pr-train-controller":
        errors.append("promotion owner must remain the PR-train controller")
    if profile.get("entrypoint") != "tools/ringer_supervisor_pr_train.py":
        errors.append("profile must use the authoritative PR-train entrypoint")

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1:
        errors.append("manifest must contain exactly one implementation task")
        return errors
    task = tasks[0]
    if not isinstance(task, dict):
        errors.append("implementation task must be an object")
        return errors

    if task.get("engine") != "opencode":
        errors.append("implementation engine must be opencode")
    if task.get("model") != "minimax-coding-plan/MiniMax-M3":
        errors.append("implementation model must be MiniMax-M3")
    spec = str(task.get("spec") or "")
    if FORBIDDEN_WORKER_ACTIONS.search(spec):
        errors.append("worker spec authorizes controller-owned Git/PR mutation")
    required_denials = ("Never stage", "commit", "push", "pull request", "uncommitted")
    for token in required_denials:
        if token.lower() not in spec.lower():
            errors.append(f"worker spec is missing boundary wording: {token}")

    check = str(task.get("check") or "").strip().lower()
    if not check or check in WEAK_CHECKS:
        errors.append("inner check is empty or cannot fail")
    objective = task.get("objective_checks")
    if not isinstance(objective, list) or not objective:
        errors.append("objective_checks must be a non-empty list")
    else:
        for index, item in enumerate(objective):
            argv = item.get("argv") if isinstance(item, dict) else None
            if not isinstance(argv, list) or not argv or not all(
                isinstance(arg, str) and arg for arg in argv
            ):
                errors.append(f"objective_checks[{index}].argv must be a non-empty string array")

    supervisor = manifest.get("supervisor")
    if not isinstance(supervisor, dict):
        errors.append("supervisor must be an object")
        return errors
    allowed = supervisor.get("allowed_changed_paths")
    if not isinstance(allowed, list) or not allowed or not all(
        isinstance(path, str) and path.strip() for path in allowed
    ):
        errors.append("allowed_changed_paths must be a non-empty string list")
    routes = supervisor.get("routes")
    if not isinstance(routes, list) or len(routes) != 1:
        errors.append("supervisor must declare exactly one implementation route")
    elif routes[0].get("engine") != task.get("engine") or routes[0].get("model") != task.get("model"):
        errors.append("task engine/model must match the sole supervisor route")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    errors = validate(args.profile, args.manifest)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"PASS: {PROFILE_NAME} v{PROFILE_VERSION} static contract is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
