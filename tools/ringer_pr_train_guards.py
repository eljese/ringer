#!/usr/bin/env python3
"""Candidate-path and inner-artifact guards for the PR-train supervisor."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


INNER_ATTEMPT_RE = re.compile(r".+-\d{8}T\d{6}Z-p\d+-a\d{3}$")
RUNTIME_ARTIFACT_NAMES = frozenset(
    {
        "attempt.json",
        "heartbeat.json",
        "progress.json",
        "supervisor-events.jsonl",
        "supervisor-outcome.json",
        "supervisor-preflight.json",
        "worker.log",
    }
)
RUNTIME_ARTIFACT_DIRS = frozenset(
    {".ringer", ".codex-pr-train", ".pr-train-runtime", "ringer-lifecycle-artifacts"}
)


def normalize_owned_path(raw: str, error_type: type[Exception]) -> str:
    value = raw.strip().replace("\\", "/")
    subtree = value.endswith("/**")
    core = value[:-3] if subtree else value
    pure = PurePosixPath(core)
    if (
        not value
        or core in {"", ".", "/"}
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or ":" in core
        or "{{" in core
        or any(part.lower() in RUNTIME_ARTIFACT_DIRS for part in pure.parts)
        or pure.name.lower() in RUNTIME_ARTIFACT_NAMES
    ):
        raise error_type(
            f"MANIFEST_POLICY_FAILURE: invalid allowed_changed_paths entry {raw!r}"
        )
    return pure.as_posix() + ("/**" if subtree else "")


def policy_from_task(
    task: dict[str, Any], expected: list[str], error_type: type[Exception]
) -> list[str]:
    raw = task.pop("allowed_changed_paths", None)
    if raw is None:
        raw = [
            item.removeprefix("{{TASK_DIR}}/")
            for item in expected
            if item.startswith("{{TASK_DIR}}/")
        ]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise error_type(
            "MANIFEST_POLICY_FAILURE: allowed_changed_paths must be a list of strings"
        )
    normalized = [normalize_owned_path(item, error_type) for item in raw]
    if len(normalized) != len(set(normalized)):
        raise error_type("MANIFEST_POLICY_FAILURE: allowed_changed_paths contains duplicates")
    return normalized


def policies_by_worktree(source: dict[str, Any], hardened: Any) -> dict[Path, tuple[str, ...]]:
    workdir = Path(str(source["workdir"])).expanduser().resolve()
    supervisor = source.get("supervisor") or {}
    raw = supervisor.get("_pr_train_allowed_changed_paths") or {}
    result: dict[Path, tuple[str, ...]] = {}
    for task in source.get("tasks") or []:
        key = str(task["key"])
        policy = tuple(str(item) for item in raw.get(key, []))
        routes = hardened.legacy._routes_for_task(task, supervisor)
        for attempt in range(1, len(routes) + 1):
            result[(workdir / f"{key}--attempt-{attempt:03d}").resolve()] = policy
    return result


def policy_for(
    worktree: Path,
    policies: dict[Path, tuple[str, ...]],
    error_type: type[Exception],
) -> tuple[str, ...]:
    resolved = worktree.resolve()
    if resolved not in policies:
        raise error_type(
            f"MANIFEST_POLICY_FAILURE: no changed-path policy for attempt worktree {resolved}"
        )
    return policies[resolved]


def changed_paths(worktree: Path, error_type: type[Exception]) -> list[str]:
    ignored = {".ringer-lifecycle.json", "worker.log"}
    tracked = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-status", "-z", "HEAD", "--"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if tracked.returncode != 0:
        raise error_type("MANIFEST_POLICY_FAILURE: could not enumerate candidate changes")
    fields = tracked.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    changed: list[str] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        count = 2 if status.startswith(("R", "C")) else 1
        for _ in range(count):
            if index >= len(fields):
                break
            path = fields[index]
            index += 1
            if path and path not in ignored:
                changed.append(path)
    untracked = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if untracked.returncode != 0:
        raise error_type(
            "MANIFEST_POLICY_FAILURE: could not enumerate untracked candidate files"
        )
    changed.extend(
        path
        for path in untracked.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        if path and path not in ignored
    )
    return sorted(set(changed))


def runtime_artifact(path: str) -> bool:
    normalized = PurePosixPath(path.replace("\\", "/"))
    if normalized.name.lower() in RUNTIME_ARTIFACT_NAMES:
        return True
    return bool({part.lower() for part in normalized.parts} & RUNTIME_ARTIFACT_DIRS)


def path_allowed(path: str, policy: tuple[str, ...]) -> bool:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    for entry in policy:
        if entry.endswith("/**"):
            root = entry[:-3].rstrip("/")
            if normalized == root or normalized.startswith(root + "/"):
                return True
        elif normalized == entry:
            return True
    return False


def _was_tracked_symlink(worktree: Path, path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(worktree), "ls-tree", "-z", "HEAD", "--", path],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return False
    return any(record.startswith("120000 blob ") for record in result.stdout.decode(
        "utf-8", errors="surrogateescape"
    ).split("\0"))


def assert_candidate(
    worktree: Path,
    error_type: type[Exception],
    allowed: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    changed = changed_paths(worktree, error_type)
    contaminated = sorted(path for path in changed if runtime_artifact(path))
    if contaminated:
        raise error_type(
            "RUNTIME_ARTIFACT_CONTAMINATION: candidate contains harness-owned paths: "
            + ", ".join(contaminated)
        )
    symlinks = sorted(
        path
        for path in changed
        if (worktree / path).is_symlink() or _was_tracked_symlink(worktree, path)
    )
    if symlinks:
        raise error_type(
            "MANIFEST_POLICY_FAILURE: changed symlinks are not allowed: "
            + ", ".join(symlinks)
        )
    if allowed is not None:
        policy = tuple(allowed)
        unexpected = sorted(path for path in changed if not path_allowed(path, policy))
        if unexpected:
            raise error_type(
                "MANIFEST_POLICY_FAILURE: candidate changed paths outside allowed_changed_paths: "
                + ", ".join(unexpected)
            )
    return changed


def relocate_inner_artifacts(
    candidate: Path, destination_root: Path, error_type: type[Exception]
) -> list[str]:
    moved: list[str] = []
    if not candidate.is_dir():
        return moved
    destination_root.mkdir(parents=True, exist_ok=True)
    for path in sorted(candidate.iterdir()):
        if (
            not path.is_dir()
            or path.is_symlink()
            or not INNER_ATTEMPT_RE.fullmatch(path.name)
            or not (path / "attempt.json").is_file()
        ):
            continue
        destination = destination_root / path.name
        if destination.exists():
            raise error_type(
                f"CLEANUP_FAILURE: inner Ringer artifact destination exists: {destination}"
            )
        shutil.move(str(path), str(destination))
        moved.append(str(destination))
    return moved
