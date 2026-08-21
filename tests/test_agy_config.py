#!/usr/bin/env python3
"""Regression tests for the shipped AGY model default and command shape."""

from __future__ import annotations

from datetime import date
import json
import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.sample.toml"
REGISTRY = ROOT / "registry" / "model-identity.toml"
README = ROOT / "README.md"
AGY_DOCS = ROOT / "docs" / "AGY.md"
SUPERVISOR_DOCS = ROOT / "docs" / "deterministic-supervisor.md"
DEFAULT_MODEL = "gemini-3.7-flash-high"

_AGY_START = re.compile(r"^#\s*\[engines\.agy\]\s*$")
_ENGINE_END = re.compile(r"^#\s*\[engines\.[a-zA-Z_]+\]\s*$")
# Keep in lockstep with tests/test_claude_engine.py: a kept line is a
# sub-table, a key=value, or a TOML array continuation.
_TOML_LINE = re.compile(
    r"^\s*(\[.+]|"
    r"[A-Za-z_][A-Za-z0-9_.-]*\s*=|"
    r"\s*[#\"'\[\],]|"
    r"\s*[\[\]]\s*$)"
)


def _load_commented_agy_block() -> dict:
    """Parse the opt-in AGY block as a user would after uncommenting it."""
    lines: list[str] = []
    inside = False
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if not inside:
            if _AGY_START.match(line):
                inside = True
                lines.append("[engines.agy]")
            continue
        if _ENGINE_END.match(line):
            break
        if line.startswith("# "):
            stripped = line[2:]
        elif line.startswith("#"):
            stripped = line.lstrip("#").lstrip()
        else:
            stripped = line
        if _TOML_LINE.match(stripped):
            lines.append(stripped)
    return tomllib.loads("\n".join(lines) + "\n")["engines"]["agy"]


def _load_agy_registry() -> dict:
    with REGISTRY.open("rb") as handle:
        return tomllib.load(handle)["engines"]["agy"]


class AgyConfigTests(unittest.TestCase):
    def test_sample_default_is_gemini_3_7_flash_high(self) -> None:
        self.assertEqual(_load_commented_agy_block()["model_default"], DEFAULT_MODEL)

    def test_sample_command_uses_add_dir_taskdir(self) -> None:
        template = _load_commented_agy_block()["args_template"]
        pairs = set(zip(template, template[1:]))
        self.assertIn(("--add-dir", "{workdir}"), pairs)
        self.assertIn(("--add-dir", "{taskdir}"), pairs)

    def test_sample_command_uses_model_placeholder(self) -> None:
        template = _load_commented_agy_block()["args_template"]
        self.assertIn(("--model", "{model}"), set(zip(template, template[1:])))

    def test_sample_command_uses_accept_edits(self) -> None:
        template = _load_commented_agy_block()["args_template"]
        self.assertIn(("--mode", "accept-edits"), set(zip(template, template[1:])))

    def test_sample_command_includes_sandbox(self) -> None:
        self.assertIn("--sandbox", _load_commented_agy_block()["args_template"])

    def test_sample_agy_block_has_closing_engine_header(self) -> None:
        lines = CONFIG.read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if _AGY_START.match(line))
        self.assertTrue(
            any(_ENGINE_END.match(line) for line in lines[start + 1 :]),
            msg="no closing engine-table header after [engines.agy]; "
            "uncommenting helper would consume the rest of the file",
        )

    def test_registry_default_is_gemini_3_7_flash_high(self) -> None:
        self.assertEqual(_load_agy_registry()["default_model_key"], DEFAULT_MODEL)

    def test_registry_default_display_name(self) -> None:
        identity = _load_agy_registry()["models"][DEFAULT_MODEL]
        self.assertEqual(identity["display"], "Gemini 3.7 Flash High")

    def test_registry_default_lab_is_google(self) -> None:
        identity = _load_agy_registry()["models"][DEFAULT_MODEL]
        self.assertEqual(identity["lab"], "Google")

    def test_registry_default_confidence_is_verified(self) -> None:
        identity = _load_agy_registry()["models"][DEFAULT_MODEL]
        self.assertEqual(identity["confidence"], "verified")

    def test_registry_default_source_cites_agy_1_1_13(self) -> None:
        identity = _load_agy_registry()["models"][DEFAULT_MODEL]
        self.assertIn("agy 1.1.13", identity["source"])

    def test_registry_default_last_verified_is_2026_08_14(self) -> None:
        identity = _load_agy_registry()["models"][DEFAULT_MODEL]
        self.assertEqual(identity["last_verified"], date(2026, 8, 14))

    def test_registry_keeps_gemini_3_6_flash_high(self) -> None:
        self.assertIn("gemini-3.6-flash-high", _load_agy_registry()["models"])

    def test_registry_3_6_display_name(self) -> None:
        identity = _load_agy_registry()["models"]["gemini-3.6-flash-high"]
        self.assertEqual(identity["display"], "Gemini 3.6 Flash High")

    def test_registry_3_6_lab_is_google(self) -> None:
        identity = _load_agy_registry()["models"]["gemini-3.6-flash-high"]
        self.assertEqual(identity["lab"], "Google")

    def test_registry_keeps_gemini_3_1_pro_high(self) -> None:
        self.assertIn("gemini-3.1-pro-high", _load_agy_registry()["models"])

    def test_registry_3_1_display_name(self) -> None:
        identity = _load_agy_registry()["models"]["gemini-3.1-pro-high"]
        self.assertEqual(identity["display"], "Gemini 3.1 Pro High")

    def test_registry_3_1_lab_is_google(self) -> None:
        identity = _load_agy_registry()["models"]["gemini-3.1-pro-high"]
        self.assertEqual(identity["lab"], "Google")

    def test_smoke_template_uses_default_model(self) -> None:
        payload = json.loads(
            (ROOT / "templates" / "agy-smoke.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["tasks"][0]["model"], DEFAULT_MODEL)

    def test_readme_names_gemini_3_7_flash_high(self) -> None:
        self.assertIn(DEFAULT_MODEL, README.read_text(encoding="utf-8"))

    def test_readme_does_not_advertise_3_6_as_model_default(self) -> None:
        self.assertNotIn(
            'model_default` is `"gemini-3.6-flash-high"`',
            README.read_text(encoding="utf-8"),
        )

    def test_agy_docs_name_gemini_3_7_flash_high(self) -> None:
        self.assertIn(DEFAULT_MODEL, AGY_DOCS.read_text(encoding="utf-8"))

    def test_agy_docs_do_not_advertise_3_6_as_model_default(self) -> None:
        self.assertNotIn(
            "shipped default `gemini-3.6-flash-high`",
            AGY_DOCS.read_text(encoding="utf-8"),
        )

    def test_supervisor_example_uses_default_model(self) -> None:
        self.assertIn(
            f'"model": "{DEFAULT_MODEL}"',
            SUPERVISOR_DOCS.read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            '"model": "gemini-3.6-flash-high"',
            SUPERVISOR_DOCS.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
