#!/usr/bin/env python3
"""Launch AGY with a Ringer-owned review profile in its isolated worker HOME."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

PROFILE_PATH = Path(__file__).resolve().parents[1] / "profiles" / "agy-review-settings.json"
EXPECTED_ALLOW = {
    "read_file(*)",
    "grep_search(*)",
    "list_dir(*)",
    "list_directory(*)",
    "write_file(*)",
}
REQUIRED_DENY = {
    "command(*)",
    "run_command(*)",
    "Bash(*)",
    "bash(*)",
    "mcp(*)",
    "search_web(*)",
    "web_search(*)",
    "read_url_content(*)",
}
FORBIDDEN_TOP_LEVEL = {
    "toolPermission",
    "artifactReviewPolicy",
    "trustedWorkspaces",
}


def fail(message: str) -> NoReturn:
    print(f"agy-safe-review: {message}", file=sys.stderr)
    raise SystemExit(2)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_profile(profile: object) -> dict[str, object]:
    if not isinstance(profile, dict):
        fail("review settings profile must be a JSON object")
    if FORBIDDEN_TOP_LEVEL.intersection(profile):
        fail("review settings profile contains broad approval state")
    if profile.get("enableTelemetry") is not False:
        fail("review settings must disable telemetry")
    if profile.get("allowNonWorkspaceAccess") is not False:
        fail("review settings must forbid non-workspace access")

    permissions = profile.get("permissions")
    if not isinstance(permissions, dict):
        fail("review settings permissions must be an object")
    allow = permissions.get("allow")
    deny = permissions.get("deny")
    if not isinstance(allow, list) or not all(isinstance(item, str) for item in allow):
        fail("review settings allow list is invalid")
    if not isinstance(deny, list) or not all(isinstance(item, str) for item in deny):
        fail("review settings deny list is invalid")
    if set(allow) != EXPECTED_ALLOW:
        fail("review settings allow list is broader or narrower than the approved profile")
    if not REQUIRED_DENY.issubset(set(deny)):
        fail("review settings do not deny command, web, and MCP capabilities")
    if any(item in {"*", "command", "command(*)"} for item in allow):
        fail("review settings grant broad command access")
    return profile


def _ensure_safe_home() -> tuple[Path, Path]:
    if os.environ.get("RINGER_SAFE_ENFORCE") != "1":
        fail("RINGER_SAFE_ENFORCE=1 is required")
    runtime_raw = os.environ.get("RINGER_RUNTIME_ROOT", "")
    home_raw = os.environ.get("HOME", "")
    if not runtime_raw or not home_raw:
        fail("isolated RINGER_RUNTIME_ROOT and HOME are required")

    runtime = Path(runtime_raw)
    home = Path(home_raw)
    if runtime.is_symlink() or home.is_symlink():
        fail("runtime and worker HOME must not be symlinks")
    try:
        runtime = runtime.resolve(strict=True)
        home = home.resolve(strict=True)
    except OSError as error:
        fail(f"runtime or worker HOME is unavailable: {error}")

    expected_root = runtime / "engine-homes" / "agy"
    if not _is_within(home, expected_root) or home == expected_root:
        fail("worker HOME is outside the isolated AGY engine-home tree")
    return runtime, home


def _load_profile() -> dict[str, object]:
    if PROFILE_PATH.is_symlink() or not PROFILE_PATH.is_file():
        fail("Ringer-owned review settings profile is missing or is a symlink")
    try:
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not load the review settings profile: {error}")
    return _validate_profile(profile)


def _write_settings(home: Path, profile: dict[str, object]) -> Path:
    settings_dir = home / ".gemini" / "antigravity-cli"
    settings_path = settings_dir / "settings.json"
    for candidate in (home / ".gemini", settings_dir, settings_path):
        if candidate.is_symlink():
            fail("refusing to write AGY settings through a symlink")
    settings_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(settings_dir, 0o700)
    if settings_path.exists():
        fail("isolated AGY settings already exist; refusing to overwrite them")

    payload = json.dumps(profile, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".settings.", dir=settings_dir)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, settings_path)
        os.chmod(settings_path, 0o600)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return settings_path


def main() -> NoReturn:
    _runtime, home = _ensure_safe_home()
    profile = _load_profile()
    settings_path = _write_settings(home, profile)

    agy = shutil.which("agy")
    if not agy:
        settings_path.unlink(missing_ok=True)
        fail("agy executable was not found on PATH")
    resolved = Path(agy).resolve()
    if resolved == Path(__file__).resolve():
        settings_path.unlink(missing_ok=True)
        fail("agy executable resolved to the review launcher itself")

    os.execvpe(str(resolved), [str(resolved), *sys.argv[1:]], os.environ.copy())
    raise AssertionError("os.execvpe returned unexpectedly")


if __name__ == "__main__":
    main()
