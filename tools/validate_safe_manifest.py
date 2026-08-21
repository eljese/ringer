#!/usr/bin/env python3
"""Fail-closed policy validator for bin/ringer-safe-run.

Rejects unsafe manifests before Ringer starts. Prints a stable classification
and a short reason. Does not dump the full manifest.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable


CLASS = "MANIFEST_POLICY_FAILURE"
DEFAULT_ALLOWED_ENGINES = ("agy", "mock")
DEFAULT_MAX_PARALLEL = 4
TASK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
DESTRUCTIVE_SHELL = (
    re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\bgit\s+clean\b", re.I),
    re.compile(r"\brm\s+-[^\s]*r[^\s]*f\b", re.I),
    re.compile(r"\brm\s+-[^\s]*f[^\s]*r\b", re.I),
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r"\bchmod\b", re.I),
    re.compile(r"\bchown\b", re.I),
    re.compile(r"\bln\s+-s\b", re.I),
    re.compile(r"curl[^\n]*\|", re.I),
    re.compile(r"wget[^\n]*\|", re.I),
    re.compile(r"\bpython(?:3)?\s+-c\b", re.I),
    re.compile(r"\bperl\s+-e\b", re.I),
    re.compile(r"\bruby\s+-e\b", re.I),
)
NOOP_SHELL = re.compile(r"^(true|:|exit\s+0)\s*$")
CONSERVATIVE_SHELL = re.compile(
    r"^(test|\[|grep|egrep|fgrep)\b",
    re.I,
)
SHELL_CHECK_METACHARACTERS = frozenset(
    {";", "|", "&", "`", "$", "(", ")", ">", "<", "*", "?", "\n", "\r"}
)
CONSERVATIVE_CHECK_EXES = frozenset({"test", "[", "grep", "egrep", "fgrep"})
SENSITIVE_HOME_DIRS = (
    ".ssh",
    ".gnupg",
    ".gemini",
    ".codex",
    ".claude",
    ".aws",
    ".config",
)
FORBIDDEN_FLAGS = (
    "--dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--no-sandbox",
)


class PolicyError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def fail(message: str) -> None:
    raise PolicyError(message)


def env_list(name: str, default: Iterable[str] = ()) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return [item for item in default if item]
    return [part for part in raw.split(os.pathsep) if part.strip()]


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        fail(f"{name} must be an integer")
    if value <= 0:
        fail(f"{name} must be positive")
    return value


def source_repo() -> Path:
    raw = os.environ.get("RINGER_SAFE_SOURCE_REPO", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def resolve_existing(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError as exc:
        fail(f"cannot resolve path {path}: {exc}")


def contained(path: Path, root: Path) -> bool:
    try:
        resolved = resolve_existing(path)
        root_resolved = resolve_existing(root)
    except PolicyError:
        return False
    return resolved == root_resolved or resolved.is_relative_to(root_resolved)


def contained_in_any(path: Path, roots: list[Path]) -> bool:
    return any(contained(path, root) for root in roots)


def parse_roots(name: str, default: Iterable[str]) -> list[Path]:
    roots: list[Path] = []
    for raw in env_list(name, default):
        roots.append(Path(raw).expanduser().resolve())
    return roots


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read manifest: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        fail("manifest is not valid JSON")
    if not isinstance(data, dict):
        fail("manifest root must be a JSON object")
    return data


def require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        fail(f"missing required field: {key}")
    return value.strip()


def inspect_check(task_key: str, check: Any) -> None:
    if isinstance(check, str):
        if not check.strip():
            fail(f"task {task_key}: check is required")
        if NOOP_SHELL.match(check.strip()):
            fail(f"task {task_key}: no-op shell check is forbidden")
        for pattern in DESTRUCTIVE_SHELL:
            if pattern.search(check):
                fail(f"task {task_key}: destructive shell check is forbidden")
        if not CONSERVATIVE_SHELL.match(check.strip()):
            fail(
                f"task {task_key}: shell checks must start with test, [, or grep; "
                "prefer a structured argv check"
            )
        if any(ch in check for ch in SHELL_CHECK_METACHARACTERS):
            fail(
                f"task {task_key}: shell check contains metacharacters; "
                "prefer a structured argv check"
            )
        for token in check.split():
            inspect_check_path(task_key, token)
        return
    if isinstance(check, dict):
        extra = set(check) - {"argv"}
        if extra:
            fail(f"task {task_key}: structured check may only contain argv")
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            fail(f"task {task_key}: check.argv must be a non-empty list of strings")
        joined = " ".join(argv)
        for pattern in DESTRUCTIVE_SHELL:
            if pattern.search(joined):
                fail(f"task {task_key}: destructive argv check is forbidden")
        exe = Path(str(argv[0])).name.lower()
        if exe not in CONSERVATIVE_CHECK_EXES:
            fail(
                f"task {task_key}: argv checks must use test, [, or grep; "
                "do not invoke a shell or interpreter"
            )
        if exe in {"bash", "sh", "dash", "zsh", "ksh"} and any(
            item in {"-c", "-lc"} for item in argv[1:]
        ):
            fail(f"task {task_key}: shell -c checks are forbidden")
        for item in argv:
            inspect_check_path(task_key, item)
        return
    fail(f"task {task_key}: check must be a string or an argv object")


def inspect_check_path(task_key: str, token: str) -> None:
    candidate = Path(token).expanduser()
    if ".." in Path(token).parts or ".." in candidate.parts:
        fail(f"task {task_key}: path traversal in check")
    if (token.startswith("~") or candidate.is_absolute()) and path_under_real_home(
        candidate
    ):
        fail(f"task {task_key}: check path must not sit under the real home")


def is_add_dir_flag(item: str) -> bool:
    lowered = item.lower()
    return lowered == "--add-dir" or lowered.startswith("--add-dir=")


def inspect_flags(task_key: str, values: Iterable[str]) -> None:
    items = list(values)
    for item, nxt in zip(items, items[1:] + [""]):
        lowered = item.lower()
        if lowered in {flag.lower() for flag in FORBIDDEN_FLAGS}:
            fail(f"task {task_key}: forbidden flag {item}")
        if is_add_dir_flag(item):
            fail(f"task {task_key}: extra --add-dir is forbidden")
        if lowered == "--sandbox" or lowered.startswith("--sandbox="):
            target = lowered.split("=", 1)[1] if "=" in lowered else nxt.lower()
            if target in {"off", "none", "disabled"}:
                fail(f"task {task_key}: sandbox disable is forbidden")


def is_truthy_full_access(value: Any) -> bool:
    return value is True or value in (1, "1", "true", "True", "yes", "on")


def real_home() -> Path:
    raw = os.environ.get("RINGER_SAFE_REAL_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home().resolve()


def workdir_is_sensitive(workdir: Path) -> bool:
    home = real_home()
    try:
        resolved = workdir.resolve()
    except OSError:
        resolved = workdir
    if resolved == home:
        return True
    if not resolved.is_relative_to(home):
        return False
    parts = set(resolved.parts)
    return any(name in parts for name in SENSITIVE_HOME_DIRS)


def path_under_real_home(path: Path) -> bool:
    home = real_home()
    try:
        resolved = path.expanduser()
        if resolved.exists():
            resolved = resolved.resolve()
    except OSError:
        return True
    return resolved == home or resolved.is_relative_to(home)


def validate_manifest(path: Path) -> None:
    resolved = resolve_existing(path)
    if resolved.is_symlink() or path.is_symlink():
        # resolve() already followed the link; still reject a link whose
        # final target is outside the allowlist via the root check below.
        pass
    ringer_root = source_repo()
    manifest_roots = parse_roots(
        "RINGER_SAFE_MANIFEST_ROOTS",
        (str(ringer_root / "templates"), str(ringer_root / "manifests")),
    )
    if not contained_in_any(resolved, manifest_roots):
        fail("manifest path is outside the configured safe roots")

    data = load_manifest(resolved)
    require_string(data, "run_name")
    workdir_raw = require_string(data, "workdir")
    workdir = Path(workdir_raw).expanduser()
    if not workdir.is_absolute():
        workdir = (resolved.parent / workdir).resolve()
    else:
        workdir = resolve_existing(workdir) if workdir.exists() else workdir.resolve()

    if contained(workdir, ringer_root):
        fail("workdir must not be inside the Ringer source repository")
    if workdir_is_sensitive(workdir):
        fail("workdir must not be a sensitive home directory")

    runtime_roots = parse_roots("RINGER_SAFE_RUNTIME_ROOTS", ())
    artifact_roots = parse_roots("RINGER_SAFE_ARTIFACT_ROOTS", runtime_roots)
    project_roots = parse_roots("RINGER_SAFE_PROJECT_ROOTS", ())
    if project_roots or runtime_roots:
        allowed_work = project_roots + runtime_roots
        if not contained_in_any(workdir, allowed_work):
            fail("workdir is outside the configured project/runtime roots")
    elif path_under_real_home(workdir):
        fail("workdir under the real home requires RINGER_SAFE_PROJECT_ROOTS")

    max_parallel = data.get("max_parallel", 1)
    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int):
        fail("max_parallel must be an integer")
    limit = env_int("RINGER_SAFE_MAX_PARALLEL", DEFAULT_MAX_PARALLEL)
    if max_parallel > limit:
        fail(f"max_parallel {max_parallel} exceeds limit {limit}")
    if max_parallel <= 0:
        fail("max_parallel must be positive")

    if "full_access" in data and not isinstance(data.get("full_access"), bool):
        fail("full_access must be a boolean")
    if is_truthy_full_access(data.get("full_access")):
        fail("full_access is forbidden")

    repo_raw = data.get("repo")
    if repo_raw is not None:
        if not isinstance(repo_raw, str) or not repo_raw.strip():
            fail("repo must be a non-empty string when set")
        repo = Path(repo_raw).expanduser()
        repo = resolve_existing(repo) if repo.exists() else repo
        if project_roots and not contained_in_any(repo, project_roots):
            fail("repository path is outside the configured project roots")
        if contained(repo, ringer_root) and not project_roots:
            # Reviewing Ringer itself is allowed only when the operator
            # explicitly allowlists the checkout as a project root.
            fail("repository path is the Ringer source checkout; set RINGER_SAFE_PROJECT_ROOTS to allow it")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        fail("tasks must be a non-empty list")

    allowed_engines = set(
        env_list("RINGER_SAFE_ALLOWED_ENGINES", DEFAULT_ALLOWED_ENGINES)
    )
    seen_keys: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            fail(f"task {index}: must be an object")
        key = task.get("key")
        if not isinstance(key, str) or not TASK_KEY_RE.fullmatch(key):
            fail(f"task {index}: key is missing or contains unsafe characters")
        if ".." in key or "/" in key or "\\" in key:
            fail(f"task {key}: path traversal in task key")
        if key in seen_keys:
            fail(f"duplicate task key: {key}")
        seen_keys.add(key)
        spec = task.get("spec")
        if not isinstance(spec, str) or not spec.strip():
            fail(f"task {key}: spec is required")
        inspect_check(key, task.get("check", ""))
        if "full_access" in task and not isinstance(task.get("full_access"), bool):
            fail(f"task {key}: full_access must be a boolean")
        if is_truthy_full_access(task.get("full_access")):
            fail(f"task {key}: full_access is forbidden")
        engine = str(task.get("engine", "codex")).strip()
        if engine not in allowed_engines:
            fail(f"task {key}: engine {engine!r} is not allowlisted")
        engine_args = task.get("engine_args", [])
        if engine_args:
            if not isinstance(engine_args, list) or not all(isinstance(item, str) for item in engine_args):
                fail(f"task {key}: engine_args must be a list of strings")
            inspect_flags(key, engine_args)
        expect_files = task.get("expect_files", [])
        if expect_files:
            if not isinstance(expect_files, list):
                fail(f"task {key}: expect_files must be a list")
            for item in expect_files:
                text = str(item)
                candidate = Path(text).expanduser()
                if candidate.is_absolute() or text.startswith("~"):
                    approved = artifact_roots + runtime_roots
                    if not approved or not contained_in_any(candidate, approved):
                        fail(f"task {key}: absolute output path is outside approved roots")
                if ".." in Path(text).parts:
                    fail(f"task {key}: path traversal in expect_files")

    for key in ("artifact_dir", "runtime", "outdir", "output"):
        raw = data.get(key)
        if isinstance(raw, str) and raw.startswith("/"):
            output = Path(raw)
            if runtime_roots and not contained_in_any(output, artifact_roots + runtime_roots):
                fail(f"{key} is outside approved runtime/artifact roots")
            if contained(output, ringer_root):
                fail(f"{key} must not be inside the Ringer source repository")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_safe_manifest.py",
        description="Fail-closed policy validator for isolated Ringer runs.",
    )
    parser.add_argument("--manifest", required=True, type=Path, help="path to the manifest JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_manifest(args.manifest)
    except PolicyError as exc:
        print(CLASS)
        print(exc.message)
        return 2
    print("MANIFEST_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
