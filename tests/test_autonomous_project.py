#!/usr/bin/env python3
"""Pin the checked-in autonomy contract used by the merge gate."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / ".claude" / "autonomous-project.json"


def _load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


class AutonomousProjectTests(unittest.TestCase):
    def test_contract_file_exists(self) -> None:
        self.assertTrue(CONTRACT.is_file())

    def test_schema_version_is_1(self) -> None:
        self.assertEqual(_load_contract()["schemaVersion"], 1)

    def test_default_branch_is_main(self) -> None:
        self.assertEqual(_load_contract()["defaultBranch"], "main")

    def test_merge_method_is_squash(self) -> None:
        self.assertEqual(_load_contract()["mergeMethod"], "squash")

    def test_required_check_is_ubuntu_full_suite(self) -> None:
        self.assertEqual(
            _load_contract()["requiredChecks"],
            ["full suite (ubuntu-latest)"],
        )

    def test_canonical_test_command_matches_ci(self) -> None:
        self.assertEqual(
            _load_contract()["commands"]["test"],
            ["python3 -m unittest discover -s tests -v"],
        )

    def test_codex_github_bot_is_disabled(self) -> None:
        self.assertEqual(
            _load_contract()["externalAgents"]["codex"]["review"]["githubBot"],
            "disabled",
        )

    def test_codex_collaboration_is_disabled(self) -> None:
        self.assertFalse(_load_contract()["externalAgents"]["codex"]["enabled"])


if __name__ == "__main__":
    unittest.main()
