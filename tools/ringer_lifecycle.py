#!/usr/bin/env python3
"""Safety-oriented lifecycle runner layered on top of ringer.py.

This command keeps Ringer's worker/reviewer execution unchanged while owning the
parts that are easy to get wrong in long PR lifecycles: worktree recovery,
attempt-scoped artifacts, durable patch export, shell-safe argv checks, canonical
path substitution, focused review packets, failure classification, and cleanup.

It intentionally uses only the Python standard library so it can travel with
Ringer's single-file runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


FAILURE_CLASSES = (
    "WORKER_FINDING",
    "CHECK_FAILURE",
    "PROVIDER_QUOTA",
    "PROVIDER_TIMEOUT",
    "NETWORK_SANDBOX",
    "STALE_WORKTREE",
    "STALE_ARTIFACT",
    "MISSING_EXPORT",
    "MANIFEST_PATH_ERROR",
    "SHELL_INTERPOLATION",
    "COORDINATOR_ERROR",
)

OWNERSHIP_MARKER = ".ringer-lifecycle.json"
REPORT_NAMES = {"report.md", "report.html", "fix-summary.md", "notes.md"}
DEFAULT_REVIEW_PACKET_BYTES = 96 * 1024


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskOutcome:
    key: str
    status: str
    attempts: int
    failure_class: str | None
    patch_path: str | None
    patch_sha256: str | None
    artifact_dir: str


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=check,
    )


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(repo), *args], check=check)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
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


def classify_failure(text: str, *, returncode: int | None = None) -> str:
    lower = text.lower()
    if any(token in lower for token in ("quota", "rate limit", "usage limit", "credits exhausted")):
        return "PROVIDER_QUOTA"
    if any(token in lower for token in ("timed out", "timeout", "deadline exceeded")):
        return "PROVIDER_TIMEOUT"
    if any(token in lower for token in (
        "network is unreachable", "could not resolve host", "dns", "sandbox", "permission denied"
    )):
        return "NETWORK_SANDBOX"
    if "worktree" in lower and any(token in lower for token in ("already exists", "stale", "left by")):
        return "STALE_WORKTREE"
    if "stale" in lower and any(token in lower for token in ("report", "artifact", "evidence")):
        return "STALE_ARTIFACT"
    if any(token in lower for token in ("missing expected files", "missing export", "patch was not exported")):
        return "MISSING_EXPORT"
    if any(token in lower for token in ("escapes workdir", "expect_files", "artifact path", "path mismatch")):
        return "MANIFEST_PATH_ERROR"
    if "bad substitution" in lower or "${{" in text and "/bin/sh" in lower:
        return "SHELL_INTERPOLATION"
    if returncode not in (None, 0):
        return "CHECK_FAILURE"
    return "COORDINATOR_ERROR"


def repo_head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def canonical_values(
    *,
    run_dir: Path,
    task_dir: Path,
    artifact_dir: Path,
    source_repo: Path | None,
    attempt: int,
) -> dict[str, str]:
    return {
        "{{RUN_DIR}}": str(run_dir),
        "{{TASK_DIR}}": str(task_dir),
        "{{ARTIFACT_DIR}}": str(artifact_dir),
        "{{SOURCE_REPO}}": str(source_repo or ""),
        "{{BASE_SHA}}": repo_head(source_repo) if source_repo else "",
        "{{ATTEMPT}}": str(attempt),
    }


def substitute(value: str, variables: dict[str, str]) -> str:
    result = value
    for key, replacement in variables.items():
        result = result.replace(key, replacement)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z][A-Z0-9_]*\}\}", result)))
    if unresolved:
        raise LifecycleError(f"unresolved lifecycle path variable(s): {', '.join(unresolved)}")
    return result


def normalize_check(raw: Any, variables: dict[str, str]) -> str:
    if isinstance(raw, str):
        return substitute(raw, variables)
    if not isinstance(raw, dict) or set(raw) - {"argv"}:
        raise LifecycleError("check must be a string or an object containing only an argv list")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise LifecycleError("check.argv must be a non-empty list of strings")
    # Ringer currently executes a shell string. shlex.join gives every argv
    # element literal shell quoting, so GitHub Actions expressions and other
    # dollar-containing source text cannot be expanded accidentally.
    return shlex.join([substitute(item, variables) for item in argv])


def ensure_owned_worktree(repo: Path, task_dir: Path, artifact_dir: Path, task_key: str) -> None:
    task_dir.parent.mkdir(parents=True, exist_ok=True)
    if task_dir.exists():
        marker = task_dir / OWNERSHIP_MARKER
        if not marker.exists():
            raise LifecycleError(f"refusing to reconcile unowned existing directory: {task_dir}")
        if (task_dir / ".git").is_file():
            recovery = export_worktree_patch(task_dir, artifact_dir / "recovery.patch")
            if recovery is not None:
                print(f"preserved stale worktree changes: {recovery}")
            git(repo, "worktree", "remove", "--force", str(task_dir), check=False)
            git(repo, "worktree", "prune", check=False)
        elif task_dir.exists():
            raise LifecycleError(
                f"refusing to remove lifecycle-marked non-worktree directory: {task_dir}"
            )
    result = git(repo, "worktree", "add", "--detach", str(task_dir), "HEAD", check=False)
    if result.returncode != 0:
        raise LifecycleError(f"git worktree add failed for {task_key}: {result.stdout.strip()}")
    atomic_json(
        task_dir / OWNERSHIP_MARKER,
        {"owner": "ringer-lifecycle", "task": task_key, "source_repo": str(repo)},
    )


def dirty_paths(worktree: Path) -> list[str]:
    result = git(worktree, "status", "--porcelain=v1", "-z")
    entries = result.stdout.split("\0")
    paths: list[str] = []
    rename_source = False
    for entry in entries:
        if not entry:
            continue
        if rename_source:
            raw = entry
            rename_source = False
        else:
            raw = entry[3:] if len(entry) >= 4 else entry
            status = entry[:2]
            rename_source = "R" in status or "C" in status
            if " -> " in raw:
                raw = raw.split(" -> ", 1)[1]
        if raw and raw != OWNERSHIP_MARKER and raw != "worker.log":
            paths.append(raw)
    return sorted(set(paths))


def export_worktree_patch(
    worktree: Path,
    target: Path,
    *,
    source_repo: Path | None = None,
) -> Path | None:
    changed = dirty_paths(worktree)
    pieces: list[bytes] = []
    base_sha = git(source_repo, "rev-parse", "HEAD", check=False).stdout.strip() if source_repo else ""
    worktree_head = git(worktree, "rev-parse", "HEAD", check=False).stdout.strip()
    if base_sha and worktree_head and base_sha != worktree_head:
        committed = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--binary", f"{base_sha}..{worktree_head}", "--"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if committed.returncode != 0:
            raise LifecycleError(
                "could not read committed worktree diff: "
                + committed.stdout.decode("utf-8", errors="replace").strip()
            )
        changed.append(f"committed HEAD {worktree_head}")
        if committed.stdout:
            pieces.append(committed.stdout)
    if not changed:
        return None
    tracked = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--binary", "HEAD", "--"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if tracked.returncode != 0:
        raise LifecycleError(
            "could not export tracked changes: "
            + tracked.stdout.decode("utf-8", errors="replace").strip()
        )
    if tracked.stdout:
        pieces.append(tracked.stdout)
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard", "-z").stdout
    for rel in [item for item in untracked.split("\0") if item and item not in {OWNERSHIP_MARKER, "worker.log"}]:
        candidate = worktree / rel
        if not candidate.is_file():
            continue
        diff = subprocess.run(
            ["git", "diff", "--binary", "--no-index", "--", "/dev/null", rel],
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if diff.returncode not in (0, 1):
            raise LifecycleError(
                f"could not export untracked file {rel}: "
                f"{diff.stdout.decode('utf-8', errors='replace').strip()}"
            )
        if diff.stdout:
            pieces.append(diff.stdout)
    payload = b"".join(pieces)
    if not payload.strip():
        raise LifecycleError(
            f"worktree has changes but no durable patch could be generated: {', '.join(changed)}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    return target


def rotate_attempt_artifacts(task_dir: Path, artifact_dir: Path, expect_files: Iterable[str], attempt: int) -> None:
    previous = artifact_dir / f"attempt-{attempt - 1:03d}"
    for rel in expect_files:
        candidate = Path(rel)
        if candidate.is_absolute():
            continue
        source = task_dir / candidate
        if not source.is_file():
            continue
        # Only rotate declared evidence-like outputs. Source files listed as
        # expect_files remain untouched.
        if source.name not in REPORT_NAMES:
            continue
        previous.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, previous / source.name)
        source.unlink()


def copy_worker_log(task_dir: Path, artifact_dir: Path, attempt: int) -> None:
    source = task_dir / "worker.log"
    if not source.is_file():
        return
    target = artifact_dir / f"attempt-{attempt:03d}" / "worker.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    source.unlink()


def ringer_command(ringer: Path, config: Path | None, manifest: Path, identity: str) -> list[str]:
    cmd = [sys.executable, str(ringer), "--no-self-update"]
    if config:
        cmd.extend(["--config", str(config)])
    cmd.extend(["run", str(manifest), "--identity", identity])
    return cmd


def run_task(
    *,
    ringer: Path,
    config: Path | None,
    source_manifest: dict[str, Any],
    task: dict[str, Any],
    run_dir: Path,
    task_dir: Path,
    artifact_dir: Path,
    repo: Path | None,
    identity: str,
) -> TaskOutcome:
    max_attempts = int(task.get("max_attempts", 2))
    last_class: str | None = None
    status = "fail"
    patch: Path | None = None
    for attempt in range(1, max_attempts + 1):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        expect_files = list(task.get("expect_files") or [])
        if attempt > 1:
            rotate_attempt_artifacts(task_dir, artifact_dir, expect_files, attempt)
        variables = canonical_values(
            run_dir=run_dir,
            task_dir=task_dir,
            artifact_dir=artifact_dir,
            source_repo=repo,
            attempt=attempt,
        )
        transformed = dict(task)
        transformed["max_attempts"] = 1
        transformed["check"] = normalize_check(task.get("check"), variables)
        transformed["expect_files"] = [substitute(str(item), variables) for item in expect_files]
        transformed["spec"] = substitute(str(task.get("spec", "")), variables)
        transformed.pop("durable_output", None)
        attempt_manifest = {
            "run_name": str(source_manifest["run_name"]),
            "workdir": str(run_dir),
            "max_parallel": 1,
            # Lifecycle owns the worktree. Ringer must not delete it before
            # the durable patch is sealed.
            "worktrees": False,
            "repo": None,
            "tasks": [transformed],
        }
        manifest_path = artifact_dir / f"attempt-{attempt:03d}-manifest.json"
        atomic_json(manifest_path, attempt_manifest)
        proc = run(ringer_command(ringer, config, manifest_path, identity), capture=True)
        output = proc.stdout or ""
        (artifact_dir / f"attempt-{attempt:03d}" / "ringer-output.log").parent.mkdir(
            parents=True, exist_ok=True
        )
        (artifact_dir / f"attempt-{attempt:03d}" / "ringer-output.log").write_text(
            output, encoding="utf-8"
        )
        copy_worker_log(task_dir, artifact_dir, attempt)
        if proc.returncode == 0:
            status = "pass"
            break
        last_class = classify_failure(output, returncode=proc.returncode)
        if last_class in {"PROVIDER_QUOTA", "NETWORK_SANDBOX", "STALE_WORKTREE", "MANIFEST_PATH_ERROR"}:
            break

    if repo and status == "pass":
        patch = export_worktree_patch(
            task_dir,
            artifact_dir / "worktree.patch",
            source_repo=repo,
        )
        if dirty_paths(task_dir) and patch is None:
            status = "fail"
            last_class = "MISSING_EXPORT"

    patch_sha = sha256_file(patch) if patch else None
    return TaskOutcome(
        key=str(task["key"]),
        status=status,
        attempts=attempt,
        failure_class=last_class,
        patch_path=str(patch) if patch else None,
        patch_sha256=patch_sha,
        artifact_dir=str(artifact_dir),
    )


def build_review_packet(repo: Path, base: str, head: str, tier: int, max_bytes: int) -> str:
    if tier not in {1, 2, 3}:
        raise LifecycleError("review tier must be 1, 2, or 3")
    diff = git(repo, "diff", "--find-renames", "--find-copies", f"{base}..{head}", "--", check=False).stdout
    names = [line for line in git(repo, "diff", "--name-only", f"{base}..{head}").stdout.splitlines() if line]
    chunks = [f"# Review packet tier {tier}\n\nBase: {base}\nHead: {head}\n\n## Exact diff\n\n{diff}"]
    if tier >= 2:
        for rel in names:
            path = repo / rel
            if not path.is_file() or path.stat().st_size > 64 * 1024:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            chunks.append(f"\n## Changed file: {rel}\n\n{text}")
    if tier >= 3:
        for rel in ("AGENTS.md", "README.md"):
            path = repo / rel
            if path.is_file() and path.stat().st_size <= 96 * 1024:
                chunks.append(f"\n## Repository context: {rel}\n\n{path.read_text(encoding='utf-8', errors='replace')}")
    packet = "".join(chunks)
    raw = packet.encode("utf-8")
    if len(raw) > max_bytes:
        packet = raw[:max_bytes].decode("utf-8", errors="ignore") + "\n\n[packet truncated to budget]\n"
    return packet


def cleanup_owned_worktree(repo: Path, task_dir: Path) -> None:
    if not task_dir.exists():
        return
    marker = task_dir / OWNERSHIP_MARKER
    if not marker.exists():
        raise LifecycleError(f"refusing to clean unowned task directory: {task_dir}")
    git(repo, "worktree", "remove", "--force", str(task_dir), check=False)
    git(repo, "worktree", "prune", check=False)


def command_run(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.expanduser().resolve()
    source = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise LifecycleError("manifest root must be an object")
    tasks = source.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise LifecycleError("manifest tasks must be a non-empty list")
    run_dir = Path(str(source.get("workdir") or "")).expanduser().resolve()
    repo_raw = source.get("repo")
    repo = Path(str(repo_raw)).expanduser().resolve() if repo_raw else None
    if repo and not (repo / ".git").exists():
        # .git can be a file when the source itself is a linked worktree.
        probe = git(repo, "rev-parse", "--show-toplevel", check=False)
        if probe.returncode != 0:
            raise LifecycleError(f"repo is not a git checkout: {repo}")
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_root = (args.artifact_dir or (run_dir / "artifacts")).expanduser().resolve()
    outcomes: list[TaskOutcome] = []
    for task in tasks:
        if not isinstance(task, dict) or not task.get("key"):
            raise LifecycleError("every task must be an object with a key")
        key = str(task["key"])
        task_dir = (run_dir / key).resolve()
        if run_dir != task_dir and run_dir not in task_dir.parents:
            raise LifecycleError(f"task key escapes run directory: {key}")
        task_artifacts = artifact_root / key
        if repo:
            ensure_owned_worktree(repo, task_dir, task_artifacts, key)
        else:
            task_dir.mkdir(parents=True, exist_ok=True)
        outcome = run_task(
            ringer=args.ringer.expanduser().resolve(),
            config=args.config.expanduser().resolve() if args.config else None,
            source_manifest=source,
            task=task,
            run_dir=run_dir,
            task_dir=task_dir,
            artifact_dir=task_artifacts,
            repo=repo,
            identity=args.identity,
        )
        outcomes.append(outcome)
        if outcome.status != "pass":
            break
        if repo and args.cleanup:
            cleanup_owned_worktree(repo, task_dir)
    result = {
        "schema_version": 1,
        "run_name": source.get("run_name"),
        "source_manifest": str(manifest_path),
        "outcome": "pass" if outcomes and all(item.status == "pass" for item in outcomes) else "fail",
        "tasks": [item.__dict__ for item in outcomes],
    }
    atomic_json(artifact_root / "lifecycle-result.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["outcome"] == "pass" else 1


def command_review_packet(args: argparse.Namespace) -> int:
    packet = build_review_packet(
        args.repo.expanduser().resolve(),
        args.base,
        args.head,
        args.tier,
        args.max_bytes,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(packet, encoding="utf-8")
    print(args.out)
    return 0


def command_gc(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    cutoff = time.time() - args.older_than_days * 86400
    removed: list[str] = []
    for marker in root.rglob(OWNERSHIP_MARKER):
        task_dir = marker.parent
        if marker.stat().st_mtime > cutoff:
            continue
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
            repo = Path(str(meta["source_repo"])).expanduser().resolve()
        except Exception:
            continue
        if dirty_paths(task_dir):
            continue
        if args.dry_run:
            print(task_dir)
        else:
            cleanup_owned_worktree(repo, task_dir)
        removed.append(str(task_dir))
    print(json.dumps({"removed": removed, "dry_run": args.dry_run}, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    lifecycle = sub.add_parser("run", help="run a manifest with durable lifecycle safety")
    lifecycle.add_argument("manifest", type=Path)
    lifecycle.add_argument("--ringer", type=Path, default=Path(__file__).resolve().parents[1] / "ringer.py")
    lifecycle.add_argument("--config", type=Path)
    lifecycle.add_argument("--identity", default="ringer-lifecycle")
    lifecycle.add_argument("--artifact-dir", type=Path)
    lifecycle.add_argument("--cleanup", action=argparse.BooleanOptionalAction, default=True)
    lifecycle.set_defaults(func=command_run)

    packet = sub.add_parser("review-packet", help="build a bounded exact-tree review packet")
    packet.add_argument("--repo", type=Path, required=True)
    packet.add_argument("--base", required=True)
    packet.add_argument("--head", required=True)
    packet.add_argument("--tier", type=int, choices=(1, 2, 3), default=1)
    packet.add_argument("--max-bytes", type=int, default=DEFAULT_REVIEW_PACKET_BYTES)
    packet.add_argument("--out", type=Path, required=True)
    packet.set_defaults(func=command_review_packet)

    gc = sub.add_parser("gc", help="prune clean lifecycle-owned stale worktrees")
    gc.add_argument("root", type=Path)
    gc.add_argument("--older-than-days", type=int, default=7)
    gc.add_argument("--dry-run", action="store_true")
    gc.set_defaults(func=command_gc)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except (LifecycleError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ringer-lifecycle: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
