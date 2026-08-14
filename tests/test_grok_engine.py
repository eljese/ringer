#!/usr/bin/env python3
"""Regression tests for the shipped Grok model default and command shape.

The grok engine lane is shipped as a commented TOML block in
``config.sample.toml``. Unlike the AGY wrapper lane, this engine IS ``grok``
— no shell wrapper. These tests do not spawn any binary. They pin the
committed config shape (regex, args, sandbox profiles, registry identity,
install hint) so a future edit cannot silently break the lane against the
verified grok 1.0.3 probe results (2026-08-14, Linux).

The ``[engines.grok]`` block is comment-prefixed so a fresh user opts in by
deleting the leading ``# ``. The test loader mirrors that opt-in by
stripping the prefix from lines that look like a TOML key/value inside the
``[engines.grok]`` ... next ``[engines.*]`` window.
"""

from __future__ import annotations

from datetime import date
import json
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.sample.toml"
REGISTRY = ROOT / "registry" / "model-identity.toml"
README = ROOT / "README.md"
GROK_DOCS = ROOT / "docs" / "GROK.md"
RINGER_PY = ROOT / "ringer.py"
SMOKE_TEMPLATE = ROOT / "templates" / "grok-smoke.json"
DEFAULT_MODEL = "grok-4.6"

_GROK_START = re.compile(r"^#\s*\[engines\.grok\]\s*$")
_ENGINE_END = re.compile(r"^#\s*\[engines\.[a-zA-Z_]+\]\s*$")
# A kept line is a table header, a key=value, an indented array item,
# or a closing bracket. Do not keep flush-left quoted prose from the
# next engine's comment block (agy's `"gemini-3.7-flash-high". …` line
# sits between [engines.grok] and # [engines.agy]).
_TOML_LINE = re.compile(
    r"^(\[.+]|"
    r"[A-Za-z_][A-Za-z0-9_.-]*\s*=|"
    r"\s+\S|"
    r"[\[\]]\s*$)"
)


def _load_commented_grok_block() -> dict:
    """Parse the opt-in Grok block as a user would after uncommenting it."""
    lines: list[str] = []
    inside = False
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if not inside:
            if _GROK_START.match(line):
                inside = True
                lines.append("[engines.grok]")
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
    return tomllib.loads("\n".join(lines) + "\n")["engines"]["grok"]


def _load_grok_registry() -> dict:
    with REGISTRY.open("rb") as handle:
        return tomllib.load(handle)["engines"]["grok"]


def _args_pairs(template: list[str]) -> set[tuple[str, str]]:
    return set(zip(template, template[1:]))


class GrokEngineTests(unittest.TestCase):
    def test_model_default_is_grok_4_6(self) -> None:
        self.assertEqual(_load_commented_grok_block()["model_default"], DEFAULT_MODEL)

    def test_args_contain_cwd_taskdir(self) -> None:
        template = _load_commented_grok_block()["args_template"]
        self.assertIn(("--cwd", "{taskdir}"), _args_pairs(template))

    def test_args_contain_model_flag(self) -> None:
        template = _load_commented_grok_block()["args_template"]
        self.assertIn(("-m", "{model}"), _args_pairs(template))

    def test_args_contain_output_format_json(self) -> None:
        template = _load_commented_grok_block()["args_template"]
        self.assertIn(("--output-format", "json"), _args_pairs(template))

    def test_args_contain_always_approve(self) -> None:
        self.assertIn("--always-approve", _load_commented_grok_block()["args_template"])

    def test_args_contain_no_auto_update(self) -> None:
        self.assertIn("--no-auto-update", _load_commented_grok_block()["args_template"])

    def test_args_contain_prompt_flag(self) -> None:
        self.assertIn("-p", _load_commented_grok_block()["args_template"])

    def test_args_contain_spec_placeholder(self) -> None:
        self.assertIn("{spec}", _load_commented_grok_block()["args_template"])

    def test_args_contain_access_args_placeholder(self) -> None:
        self.assertIn("{access_args}", _load_commented_grok_block()["args_template"])

    def test_sandbox_args(self) -> None:
        self.assertEqual(
            _load_commented_grok_block()["sandbox_args"],
            ["--sandbox", "workspace"],
        )

    def test_full_access_args(self) -> None:
        self.assertEqual(
            _load_commented_grok_block()["full_access_args"],
            ["--sandbox", "off"],
        )

    def test_token_regex_captures_total_tokens(self) -> None:
        pattern = re.compile(_load_commented_grok_block()["token_regex"])
        match = pattern.search('{"usage":{"total_tokens":42}}')
        self.assertEqual(match.group(1), "42")

    def test_sample_omits_model_report_regex(self) -> None:
        self.assertNotIn("model_report_regex", _load_commented_grok_block())

    def test_registry_default_model_key(self) -> None:
        self.assertEqual(_load_grok_registry()["default_model_key"], DEFAULT_MODEL)

    def test_grok_4_6_display(self) -> None:
        identity = _load_grok_registry()["models"][DEFAULT_MODEL]
        self.assertEqual(identity["display"], "Grok 4.6")

    def test_grok_4_6_lab(self) -> None:
        identity = _load_grok_registry()["models"][DEFAULT_MODEL]
        self.assertEqual(identity["lab"], "xAI")

    def test_grok_4_6_confidence(self) -> None:
        identity = _load_grok_registry()["models"][DEFAULT_MODEL]
        self.assertEqual(identity["confidence"], "verified")

    def test_grok_4_6_last_verified(self) -> None:
        identity = _load_grok_registry()["models"][DEFAULT_MODEL]
        self.assertEqual(identity["last_verified"], date(2026, 8, 14))

    def test_grok_4_5_display(self) -> None:
        identity = _load_grok_registry()["models"]["grok-4.5"]
        self.assertEqual(identity["display"], "Grok 4.5")

    def test_grok_4_5_lab(self) -> None:
        identity = _load_grok_registry()["models"]["grok-4.5"]
        self.assertEqual(identity["lab"], "xAI")

    def test_grok_4_5_has_noncanonical_slug(self) -> None:
        identity = _load_grok_registry()["models"]["grok-4.5"]
        self.assertIn("opencode:openrouter/x-ai/grok-4.5", identity["noncanonical_slugs"])

    def test_grok_4_6_build_is_alias_of_grok_4_6(self) -> None:
        identity = _load_grok_registry()["models"]["grok-4.6-build"]
        self.assertIs(identity.get("alias"), True)

    def test_grok_4_6_build_display_is_grok_4_6(self) -> None:
        identity = _load_grok_registry()["models"]["grok-4.6-build"]
        self.assertEqual(identity["display"], "Grok 4.6")

    def test_registry_keeps_historical_grok_build(self) -> None:
        self.assertIn("grok-build", _load_grok_registry()["models"])

    def test_registry_keeps_historical_grok_composer(self) -> None:
        self.assertIn("grok-composer-2.5-fast", _load_grok_registry()["models"])

    def test_smoke_template_model_is_grok_4_6(self) -> None:
        payload = json.loads(SMOKE_TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(payload["tasks"][0]["model"], DEFAULT_MODEL)

    def test_smoke_check_does_not_use_assert(self) -> None:
        payload = json.loads(SMOKE_TEMPLATE.read_text(encoding="utf-8"))
        self.assertNotIn("assert ", payload["tasks"][0]["check"])

    def _run_smoke_check(self, contents: str) -> int:
        payload = json.loads(SMOKE_TEMPLATE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "grok-smoke.txt").write_text(contents, encoding="utf-8")
            return subprocess.run(
                ["bash", "-lc", payload["tasks"][0]["check"]],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
            ).returncode

    def test_smoke_check_accepts_trailing_newline(self) -> None:
        self.assertEqual(self._run_smoke_check("grok works with ringer\n"), 0)

    def test_smoke_check_rejects_wrong_text(self) -> None:
        self.assertEqual(self._run_smoke_check("wrong\n"), 1)

    def test_readme_contains_grok_4_6(self) -> None:
        self.assertIn(DEFAULT_MODEL, README.read_text(encoding="utf-8"))

    def test_grok_docs_contain_grok_4_6(self) -> None:
        self.assertIn(DEFAULT_MODEL, GROK_DOCS.read_text(encoding="utf-8"))

    def test_engine_install_hints_has_grok(self) -> None:
        src = RINGER_PY.read_text(encoding="utf-8")
        self.assertRegex(
            src,
            r'ENGINE_INSTALL_HINTS\s*=\s*\{[^}]*"grok"\s*:',
        )

    def test_sample_grok_block_has_closing_engine_header(self) -> None:
        lines = CONFIG.read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if _GROK_START.match(line))
        self.assertTrue(
            any(_ENGINE_END.match(line) for line in lines[start + 1 :]),
            msg="no closing engine-table header after [engines.grok]; "
            "uncommenting helper would consume the rest of the file",
        )

    def test_sample_file_parses_with_only_live_engine_codex(self) -> None:
        parsed = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(set(parsed["engines"].keys()), {"codex"})


if __name__ == "__main__":
    unittest.main()
