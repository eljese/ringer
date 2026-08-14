#!/usr/bin/env python3
"""Guard against publishing this fork's operator home path or personal email."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# Built from parts so this file does not contain the needles it scans for.
OPERATOR_HOME = "/".join(("", "home", "eljese"))
PERSONAL_EMAILS = (
    "@".join(("jesse.salmi", "gmail.com")),
    "@".join(("jesse", "salmi.fi")),
)


def _tracked_text_files() -> list[Path]:
    listed = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        text=True,
    )
    return [
        ROOT / rel
        for rel in listed.split("\0")
        if rel and (ROOT / rel).is_file()
    ]


def _text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


class NoOperatorIdentityTests(unittest.TestCase):
    def test_tracked_file_scan_covers_the_tree(self) -> None:
        self.assertGreater(len(_tracked_text_files()), 50)

    def test_tracked_files_do_not_contain_operator_home_path(self) -> None:
        hits = [
            path.relative_to(ROOT).as_posix()
            for path in _tracked_text_files()
            if (text := _text_or_none(path)) is not None and OPERATOR_HOME in text
        ]
        self.assertEqual(hits, [])

    def test_tracked_files_do_not_contain_personal_emails(self) -> None:
        hits = [
            f"{path.relative_to(ROOT).as_posix()}:{email}"
            for path in _tracked_text_files()
            if (text := _text_or_none(path)) is not None
            for email in PERSONAL_EMAILS
            if email in text
        ]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
