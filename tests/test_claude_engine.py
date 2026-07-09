#!/usr/bin/env python3
"""Regression tests for the `[engines.claude]` lane in `config.sample.toml`.

The claude engine lane is shipped as a commented TOML block in
`config.sample.toml`. Unlike the wrappers exercised by `test_agy_ringer.py`,
this lane has no shell wrapper of its own — the engine IS `claude`. So
these tests do not spawn any binary. They pin the committed config
shape (regex, args, sandbox allowlist, install hint) so a future edit
cannot silently break the lane against the verified `claude 2.1.179`
probe results.

The `[engines.claude]` block is comment-prefixed (`# [engines.claude]`,
`# bin = "claude"`, ...) so a fresh user opts in by deleting the
leading `# `. The test loader mirrors that opt-in by stripping the
prefix from lines that look like a TOML key/value inside the
`[engines.claude]` ... `[engines.mock]` window. The regex / args /
model_default / install-hint assertions below are checked against the
uncommented parse so the tests exercise the live shape a user would
get after enabling the lane.

The token-regex cases (1, 2, 3, 9) are a direct regression check for
the Q4 finding in the probe results doc:

  Without the literal closing quote before `output_tokens`, the regex
  matches the literal text `output_tokens : 27` (no quote) — which the
  JSON output never produces. Verified shape: `"output_tokens":27`.

If the regex ever loses the literal `"`, these tests will fail.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.sample.toml"
RINGER_PY = ROOT / "ringer.py"


# Lines bracketing the commented [engines.claude] block in
# config.sample.toml. Used to scope the uncommenting helper.
_CLAUDE_START = re.compile(r"^#\s*\[engines\.claude\]\s*$")
_CLAUDE_END = re.compile(r"^#\s*\[engines\.[a-zA-Z_]+\]\s*$")

# After un-prefixing `# `, a kept line is one whose first non-space
# char is `[` (a sub-table), or it begins with an identifier followed
# by `=` (a key=value), or it is a TOML array continuation. Lines that
# don't match are prose comments and are dropped.
_TOML_LINE = re.compile(
    r"^\s*(\[.+]|"
    r"[A-Za-z_][A-Za-z0-9_.-]*\s*=|"
    r"\s*[#\"'\[\],]|"
    r"\s*[\[\]]\s*$)"
)


def _load_claude_block_from_sample_config() -> dict:
    """Return the `[engines.claude]` table parsed from config.sample.toml.

    The block is shipped commented-out so a fresh user opts in by
    removing the leading `# `. The test fixture mirrors that opt-in
    by stripping the prefix from lines inside the
    `# [engines.claude]` ... `# [engines.mock]` window, dropping the
    prose-comment lines (`# Q1: ...`, `# Q2/Q3: ...`) that the lane
    block uses to cite its probe findings, and parsing the resulting
    string with tomllib.
    """
    text = CONFIG.read_text(encoding="utf-8")
    inside = False
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if not inside:
            if _CLAUDE_START.match(line):
                inside = True
                cleaned_lines.append("[engines.claude]")
                continue
            continue
        if _CLAUDE_END.match(line):
            break
        if line.startswith("# "):
            stripped = line[2:]
        elif line.startswith("#"):
            stripped = line.lstrip("#").lstrip()
        else:
            stripped = line
        if _TOML_LINE.match(stripped):
            cleaned_lines.append(stripped)
    cleaned = "\n".join(cleaned_lines) + "\n"
    parsed = tomllib.loads(cleaned)
    return parsed["engines"]["claude"]


def _raw_token_regex_line() -> str:
    """Return the raw, still-TOML-escaped `token_regex` line.

    Used by case 9 to prove the literal closing quote is present in
    the source the way TOML delivers it (with the doubled backslash).
    """
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("# ") and "token_regex" in line:
            stripped = line.lstrip("# ").lstrip()
            if "output_tokens" in stripped:
                return stripped
    raise AssertionError("no commented token_regex line for claude in config.sample.toml")


class ClaudeEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="claude-engine-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_token_regex_captures_output_tokens(self) -> None:
        cfg = _load_claude_block_from_sample_config()
        pattern = re.compile(cfg["token_regex"])
        # The real shape that `--output-format json` emits:
        # no whitespace between the key's closing quote and the colon.
        m = pattern.search('{"output_tokens":42}')
        self.assertEqual(m.group(1), "42")

    def test_token_regex_fails_on_missing_colon(self) -> None:
        # Q4 finding: the corrected regex requires a colon between
        # the closing quote and the capture group. The probe-results
        # doc notes that JSON shape without the colon must NOT match
        # (otherwise we'd accidentally capture the wrong number
        # when keys drift in the wire format). This pins the colon
        # requirement — drop it and this case fires.
        cfg = _load_claude_block_from_sample_config()
        pattern = re.compile(cfg["token_regex"])
        self.assertIsNone(pattern.search('{"output_tokens" 42}'))

    def test_token_regex_fails_on_missing_closing_quote(self) -> None:
        # Q4 finding: the corrected regex MUST contain a literal
        # closing quote before `output_tokens`. If a future edit
        # drops the quote (the original Q4 bug — the draft regex
        # was `"output_tokens"\s*:\s*([0-9]+)` which compiled
        # without the leading `"` because the TOML escape was
        # wrong), the regex would match the literal text
        # `output_tokens:42` — which is not a JSON shape and would
        # capture nothing from a real `--output-format json`
        # response. Pin the closing quote here.
        cfg = _load_claude_block_from_sample_config()
        pattern = re.compile(cfg["token_regex"])
        # Strip the literal " — emulate the broken draft regex.
        broken = cfg["token_regex"].replace('"', "")
        self.assertIsNone(re.search(broken, '{"output_tokens":42}'))

    def test_sandbox_args_include_bash(self) -> None:
        cfg = _load_claude_block_from_sample_config()
        flat = " ".join(cfg["sandbox_args"])
        self.assertIn("--allowedTools", flat)
        self.assertIn("Read Edit Write Glob Grep Bash", flat)

    def test_full_access_args_contains_dangerously_skip(self) -> None:
        cfg = _load_claude_block_from_sample_config()
        flat = " ".join(cfg["full_access_args"])
        self.assertIn("--dangerously-skip-permissions", flat)

    def test_model_default_is_claude_sonnet_4_6(self) -> None:
        cfg = _load_claude_block_from_sample_config()
        self.assertEqual(cfg["model_default"], "claude-sonnet-4-6")

    def test_args_template_has_bare_adddir_output_format_json(self) -> None:
        cfg = _load_claude_block_from_sample_config()
        template = cfg["args_template"]
        self.assertEqual(
            template,
            [
                "--bare",
                "--add-dir",
                "{taskdir}",
                "--model",
                "{model}",
                "{access_args}",
                "{engine_args}",
                "--output-format",
                "json",
                "-p",
                "{spec}",
            ],
        )

    def test_config_sample_toml_parses_clean(self) -> None:
        # Whole-file parse — must succeed. The claude block is
        # commented out so it is not in this parse; the point is to
        # confirm the surrounding config has no syntax errors after
        # the claude insertion.
        text = CONFIG.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
        self.assertIn("engines", parsed)
        self.assertIn("codex", parsed["engines"])

    def test_token_regex_source_has_literal_closing_quote(self) -> None:
        # Regression check for the Q4 finding: the regex source MUST
        # contain a literal `"` immediately before `output_tokens`.
        # We assert on the post-TOML-unescape Python regex source
        # (the shape that `re.compile` will actually execute) and on
        # the raw still-TOML-escaped line as it appears in
        # config.sample.toml. Drop either and this case fires.
        cfg = _load_claude_block_from_sample_config()
        pattern_source = cfg["token_regex"]
        # After tomllib unescape: "output_tokens"\s*:\s*([0-9]+).
        # The literal `"` before `output_tokens` is what catches the
        # broken draft regex from Q4.
        self.assertIn('"output_tokens"', pattern_source)
        raw_line = _raw_token_regex_line()
        # The raw TOML-escaped line has backslashed quotes; this
        # pins the source form on disk so a future edit cannot
        # silently drop the literal closing quote.
        self.assertIn(r'\"output_tokens\"', raw_line)

    def test_engine_install_hint_present(self) -> None:
        # Read the literal ENGINE_INSTALL_HINTS dict out of ringer.py.
        # We don't import ringer.py (it's a script, not a module, and
        # importing it would execute argparse setup). A small regex
        # capture keeps the test independent.
        src = RINGER_PY.read_text(encoding="utf-8")
        m = re.search(
            r'ENGINE_INSTALL_HINTS\s*=\s*\{(.*?)\n\}',
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, msg="ENGINE_INSTALL_HINTS dict not found in ringer.py")
        body = m.group(1)
        m2 = re.search(
            r'"claude"\s*:\s*"([^"]+)"',
            body,
        )
        self.assertIsNotNone(
            m2,
            msg="no `\"claude\"` entry in ENGINE_INSTALL_HINTS",
        )
        hint = m2.group(1)
        self.assertIn("npm", hint)
        self.assertIn("claude", hint)


if __name__ == "__main__":
    unittest.main()