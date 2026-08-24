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

LAUNCHER_PATH = Path(__file__).resolve()
ENTRYPOINT_PATH = LAUNCHER_PATH.with_name("agy")
PROFILE_PATH = LAUNCHER_PATH.parents[1] / "profiles" / "agy-review-settings.json"
SUPPORTED_ACTIONS = {
    "read_file",
    "write_file",
    "read_url",
    "execute_url",
    "command",
    "unsandboxed",
    "mcp",
}
EXPECTED_ALLOW = {
    "read_file(*)",
    "write_file(*)",
}
REQUIRED_DENY = {
    "read_url(*)",
    "execute_url(*)",
    "command(*)",
    "unsandboxed(*)",
    "mcp(*)",
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


def _rule_action(rule: str) -> str | None:
    action, separator, target = rule.partition("(")
    if not separator or not action or not target.endswith(")") or target == ")":
        return None
    return action


def _validate_profile(
    profile: object,
    *,
    sparse_defaults: bool = False,
) -> dict[str, object]:
    if not isinstance(profile, dict):
        fail("review settings profile must be a JSON object")
    if FORBIDDEN_TOP_LEVEL.intersection(profile):
        fail("review settings profile contains broad approval state")

    for key, label in (
        ("enableTelemetry", "disable telemetry"),
        ("allowNonWorkspaceAccess", "forbid non-workspace access"),
    ):
        if key not in profile:
            if not sparse_defaults:
                fail(f"review settings must {label}")
        elif profile[key] is not False:
            fail(f"review settings must {label}")

    permissions = profile.get("permissions")
    if not isinstance(permissions, dict):
        fail("review settings permissions must be an object")
    unknown_permission_keys = sorted(set(permissions) - {"allow", "deny", "ask"})
    if unknown_permission_keys:
        fail(
            "review settings permissions contain unsupported sections: "
            + ", ".join(unknown_permission_keys)
        )
    allow = permissions.get("allow")
    deny = permissions.get("deny")
    ask = permissions.get("ask", [])
    for name, rules in (("allow", allow), ("deny", deny), ("ask", ask)):
        if not isinstance(rules, list) or not all(isinstance(item, str) for item in rules):
            fail(f"review settings {name} list is invalid")
        if len(rules) != len(set(rules)):
            fail(f"review settings {name} list contains duplicates")
        invalid = sorted(
            rule
            for rule in rules
            if _rule_action(rule) not in SUPPORTED_ACTIONS
        )
        if invalid:
            fail(
                f"review settings {name} list contains unsupported actions: "
                + ", ".join(invalid)
            )

    if set(allow) != EXPECTED_ALLOW:
        fail("review settings allow list is broader or narrower than file review")
    if ask:
        fail("review settings ask list must be empty")
    deny_set = set(deny)
    if sparse_defaults:
        if not REQUIRED_DENY.issubset(deny_set):
            fail("persisted review settings do not deny command, web, and MCP capabilities")
    elif deny_set != REQUIRED_DENY:
        fail("review settings deny list differs from the approved profile")
    if any(item in {"*", "command", "command(*)", "unsandboxed(*)"} for item in allow):
        fail("review settings grant broad command access")
    return profile


def validate_persisted_settings(path: Path) -> dict[str, object]:
    """Validate AGY's sparse persisted settings without requiring byte identity."""
    if path.is_symlink() or not path.is_file():
        fail("persisted AGY settings are missing or are a symlink")
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"could not load persisted AGY settings: {error}")
    return _validate_profile(profile, sparse_defaults=True)


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


def _load_profile() -> tuple[dict[str, object], bytes]:
    if PROFILE_PATH.is_symlink() or not PROFILE_PATH.is_file():
        fail("Ringer-owned review settings profile is missing or is a symlink")
    try:
        payload = PROFILE_PATH.read_bytes()
        profile = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"could not load the review settings profile: {error}")
    return _validate_profile(profile), payload


def _write_settings(home: Path, payload: bytes) -> Path:
    settings_dir = home / ".gemini" / "antigravity-cli"
    settings_path = settings_dir / "settings.json"
    for candidate in (home / ".gemini", settings_dir, settings_path):
        if candidate.is_symlink():
            fail("refusing to write AGY settings through a symlink")
    settings_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(settings_dir, 0o700)
    if settings_path.exists():
        fail("isolated AGY settings already exist; refusing to overwrite them")

    fd, temporary = tempfile.mkstemp(prefix=".settings.", dir=settings_dir)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, settings_path)
        os.chmod(settings_path, 0o600)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return settings_path


def _find_real_agy() -> Path | None:
    """Find the provider binary without rediscovering either Ringer launcher."""
    launcher_dir = LAUNCHER_PATH.parent
    filtered_entries: list[str] = []
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(raw_entry or ".").expanduser()
        try:
            resolved_entry = candidate.resolve()
        except OSError:
            resolved_entry = candidate.absolute()
        if resolved_entry == launcher_dir:
            continue
        filtered_entries.append(raw_entry)

    agy = shutil.which("agy", path=os.pathsep.join(filtered_entries))
    if not agy:
        return None
    resolved = Path(agy).resolve()
    if resolved in {LAUNCHER_PATH, ENTRYPOINT_PATH}:
        return None
    return resolved


def _exec_real_agy(resolved: Path) -> NoReturn:
    os.execvpe(str(resolved), [str(resolved), *sys.argv[1:]], os.environ.copy())
    raise AssertionError("os.execvpe returned unexpectedly")


def main() -> NoReturn:
    _runtime, home = _ensure_safe_home()
    resolved = _find_real_agy()
    if resolved is None:
        fail("agy executable was not found outside the Ringer review launcher")

    if sys.argv[1:] == ["--version"]:
        _exec_real_agy(resolved)

    _profile, payload = _load_profile()
    _write_settings(home, payload)
    _exec_real_agy(resolved)


if __name__ == "__main__":
    main()
