from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "templates" / "pr-train-delivery"


def _validator_module():
    path = KIT / "checks" / "validate_profile.py"
    spec = importlib.util.spec_from_file_location("pr_train_delivery_validator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, name: str, value: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_checked_in_profile_and_manifest_are_statically_valid() -> None:
    validator = _validator_module()
    assert validator.validate(KIT / "profile.json", KIT / "manifest.json") == []


def test_rejects_worker_git_mutation(tmp_path: Path) -> None:
    validator = _validator_module()
    profile = json.loads((KIT / "profile.json").read_text(encoding="utf-8"))
    manifest = json.loads((KIT / "manifest.json").read_text(encoding="utf-8"))
    broken = copy.deepcopy(manifest)
    broken["tasks"][0]["spec"] += " Run git commit -am done."
    errors = validator.validate(
        _write(tmp_path, "profile.json", profile),
        _write(tmp_path, "manifest.json", broken),
    )
    assert "worker spec authorizes controller-owned Git/PR mutation" in errors


def test_rejects_weak_or_missing_objective_checks(tmp_path: Path) -> None:
    validator = _validator_module()
    profile = json.loads((KIT / "profile.json").read_text(encoding="utf-8"))
    manifest = json.loads((KIT / "manifest.json").read_text(encoding="utf-8"))
    broken = copy.deepcopy(manifest)
    broken["tasks"][0]["check"] = "true"
    broken["tasks"][0]["objective_checks"] = []
    errors = validator.validate(
        _write(tmp_path, "profile.json", profile),
        _write(tmp_path, "manifest.json", broken),
    )
    assert "inner check is empty or cannot fail" in errors
    assert "objective_checks must be a non-empty list" in errors
