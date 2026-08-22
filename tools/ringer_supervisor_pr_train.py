#!/usr/bin/env python3
"""Authoritative codex-pr-train entrypoint for the integrated supervisor.

The lower-level integrated wrapper owns manifest normalization, credential
seeding and candidate guards. This entrypoint additionally installs the exact
canonical HOME/XDG environment around the complete delegated lifecycle, so the
provider probe and implementation worker cannot resolve different auth trees.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ringer_supervisor_integrated as integrated  # noqa: E402


def command_run(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.expanduser().resolve()
    source = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_root = integrated._artifact_root(source, args)
    normalized = integrated.normalize_manifest(source, artifact_root=artifact_root)
    layout = integrated._runtime_layout(normalized, artifact_root)
    layout.create()

    credential_source, _required = integrated._credential_source(
        normalized.get("supervisor") or {}
    )
    previous = dict(os.environ)
    environment = layout.environment(previous)
    if credential_source is not None:
        environment["OPENCODE_AUTH_SOURCE"] = str(credential_source)
        seed_home = layout.root / "seed-home"
        seed_auth = seed_home / ".local" / "share" / "opencode" / "auth.json"
        seed_auth.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(credential_source, seed_auth)
        os.chmod(seed_auth, 0o600)
        environment["RINGER_SAFE_SEED_HOME"] = str(seed_home)
    os.environ.clear()
    os.environ.update(environment)
    try:
        return integrated.command_run(args)
    finally:
        for path in layout.root.rglob("auth.json"):
            path.unlink(missing_ok=True)
        os.environ.clear()
        os.environ.update(previous)


def parser() -> argparse.ArgumentParser:
    value = integrated.parser()
    for action in value._subparsers._group_actions:
        for subparser in action.choices.values():
            subparser.set_defaults(func=command_run)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        integrated.IntegratedSupervisorError,
    ) as error:
        print(f"ringer-supervisor-pr-train: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
