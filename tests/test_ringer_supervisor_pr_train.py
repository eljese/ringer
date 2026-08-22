from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).parents[1] / "tools" / "ringer_supervisor_pr_train.py"
spec = importlib.util.spec_from_file_location("ringer_supervisor_pr_train", MODULE_PATH)
assert spec and spec.loader
pr_train = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pr_train
spec.loader.exec_module(pr_train)


class PrTrainSupervisorTests(unittest.TestCase):
    def test_delegated_lifecycle_receives_canonical_home_and_xdg(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = root / "repo"
            repository.mkdir()
            workdir = root / "work"
            artifacts = root / "artifacts"
            auth = root / "host-auth.json"
            auth.write_text('{"token":"secret"}\n', encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "repo": str(repository),
                        "workdir": str(workdir),
                        "supervisor": {
                            "credential_seed": {
                                "source": str(auth),
                                "required": True,
                            }
                        },
                        "tasks": [
                            {
                                "key": "pr-01",
                                "spec": "test",
                                "expect_files": [],
                                "objective_checks": [
                                    {"argv": ["git", "diff", "--check"]}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                manifest=manifest,
                artifact_dir=artifacts,
                ringer=root / "ringer.py",
                config=None,
                identity="test",
                cleanup=True,
            )
            original_home = os.environ.get("HOME")

            def delegated(_args: argparse.Namespace) -> int:
                runtime = (artifacts / ".pr-train-runtime").resolve()
                self.assertEqual(os.environ["HOME"], str(runtime / "home"))
                self.assertEqual(
                    os.environ["XDG_DATA_HOME"], str(runtime / "xdg-data")
                )
                self.assertEqual(os.environ["RINGER_RUNTIME_ROOT"], str(runtime))
                self.assertEqual(os.environ["OPENCODE_AUTH_SOURCE"], str(auth))
                return 0

            with mock.patch.object(
                pr_train.integrated, "command_run", side_effect=delegated
            ):
                self.assertEqual(pr_train.command_run(args), 0)

            self.assertEqual(os.environ.get("HOME"), original_home)


if __name__ == "__main__":
    unittest.main()
