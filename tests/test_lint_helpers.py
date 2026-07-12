#!/usr/bin/env python3
"""Direct unit tests for the lint helpers in ringer.py.

These helpers are pure functions used by ``lint_manifest``. They are exercised
indirectly in ``test_lint.py`` via full manifest fixtures, but a regression in
one helper is buried inside a manifest-shaped failure message. Covering them
directly gives a precise failure surface.

Conventions follow ``tests/test_lint.py``: one assertion per method, named
after the behaviour it proves.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ringer import (  # noqa: E402
    FILE_TEST_OPS,
    has_command_prefix,
    has_quiet_diff_probe,
    is_file_existence_test,
    is_quiet_grep,
    is_relative_expect_file,
    is_silent_probe,
    spec_is_file_pointer,
    strip_common_redirections,
)


class IsFileExistenceTestTests(unittest.TestCase):
    def test_test_command_with_dash_f_is_a_file_existence_test(self) -> None:
        self.assertTrue(is_file_existence_test("test -f /tmp/foo"))

    def test_test_command_with_dash_e_is_a_file_existence_test(self) -> None:
        self.assertTrue(is_file_existence_test("test -e ./missing"))

    def test_bracket_command_with_dash_f_is_a_file_existence_test(self) -> None:
        self.assertTrue(is_file_existence_test("[ -f /tmp/foo ]"))

    def test_bracket_command_with_dash_d_is_a_file_existence_test(self) -> None:
        self.assertTrue(is_file_existence_test("[ -d /etc ]"))

    def test_unknown_op_is_not_a_file_existence_test(self) -> None:
        self.assertFalse(is_file_existence_test("test -z /tmp/foo"))

    def test_unrelated_command_is_not_a_file_existence_test(self) -> None:
        self.assertFalse(is_file_existence_test("echo hello"))

    def test_shlex_parse_error_returns_false(self) -> None:
        self.assertFalse(is_file_existence_test("test 'unbalanced"))

    def test_redirected_test_command_still_recognised(self) -> None:
        self.assertTrue(is_file_existence_test("test -f /tmp/foo 2>/dev/null"))

    def test_token_after_close_bracket_is_not_a_pure_file_test(self) -> None:
        # Compound `[ -f /tmp/foo ] && echo ok` is not a "silent file test" —
        # the trailing `echo` means there is an output branch.
        self.assertFalse(is_file_existence_test("[ -f /tmp/foo ] && echo ok"))


class IsQuietGrepTests(unittest.TestCase):
    def test_grep_with_dash_q_is_quiet(self) -> None:
        self.assertTrue(is_quiet_grep("grep -q ready output.txt"))

    def test_grep_with_combined_short_flag_including_q_is_quiet(self) -> None:
        self.assertTrue(is_quiet_grep("grep -Fqr ready output.txt"))

    def test_grep_without_quiet_flag_is_not_quiet(self) -> None:
        self.assertFalse(is_quiet_grep("grep ready output.txt"))

    def test_non_grep_command_is_not_a_quiet_grep(self) -> None:
        self.assertFalse(is_quiet_grep("rg -q ready output.txt"))


class IsSilentProbeTests(unittest.TestCase):
    def test_silent_probe_true_when_part_is_a_file_test(self) -> None:
        self.assertTrue(is_silent_probe("test -f /tmp/foo"))

    def test_silent_probe_true_when_part_is_a_quiet_grep(self) -> None:
        self.assertTrue(is_silent_probe("grep -q ready output.txt"))

    def test_silent_probe_false_for_compound_command(self) -> None:
        self.assertFalse(is_silent_probe("grep ready output.txt"))

    def test_silent_probe_false_for_echo(self) -> None:
        self.assertFalse(is_silent_probe("echo hello"))


class HasQuietDiffProbeTests(unittest.TestCase):
    def test_quiet_diff_probe_recognises_diff_dash_q(self) -> None:
        self.assertTrue(has_quiet_diff_probe("diff -q a.txt b.txt"))

    def test_quiet_diff_probe_recognises_diff_dash_q_among_chained_parts(self) -> None:
        self.assertTrue(has_quiet_diff_probe("test -f a.txt && diff -q a.txt b.txt"))

    def test_quiet_diff_probe_rejects_loud_diff(self) -> None:
        self.assertFalse(has_quiet_diff_probe("diff a.txt b.txt"))

    def test_quiet_diff_probe_rejects_echo(self) -> None:
        self.assertFalse(has_quiet_diff_probe("echo hi"))


class IsRelativeExpectFileTests(unittest.TestCase):
    def test_relative_path_is_relative(self) -> None:
        self.assertTrue(is_relative_expect_file("expect.txt"))

    def test_absolute_posix_path_is_not_relative(self) -> None:
        self.assertFalse(is_relative_expect_file("/tmp/expect.txt"))

    def test_home_relative_path_is_not_relative(self) -> None:
        self.assertFalse(is_relative_expect_file("~/expect.txt"))

    def test_blank_path_is_not_relative(self) -> None:
        self.assertFalse(is_relative_expect_file(""))

    def test_whitespace_only_path_is_not_relative(self) -> None:
        self.assertFalse(is_relative_expect_file("   "))


class SpecIsFilePointerTests(unittest.TestCase):
    def test_short_spec_with_absolute_read_path_is_a_pointer(self) -> None:
        self.assertTrue(spec_is_file_pointer("Read /tmp/instructions.md"))

    def test_short_spec_with_home_read_path_is_a_pointer(self) -> None:
        self.assertTrue(spec_is_file_pointer("Read ~/spec.md"))

    def test_short_spec_with_read_followed_by_absolute_path_is_a_pointer(self) -> None:
        self.assertTrue(spec_is_file_pointer("Please read /tmp/spec.md and proceed"))

    def test_short_spec_with_open_followed_by_absolute_path_is_a_pointer(self) -> None:
        self.assertTrue(spec_is_file_pointer("Open /tmp/spec.md"))

    def test_short_spec_with_follow_followed_by_absolute_path_is_a_pointer(self) -> None:
        self.assertTrue(spec_is_file_pointer("Follow /tmp/spec.md"))

    def test_short_spec_with_see_followed_by_absolute_path_is_a_pointer(self) -> None:
        self.assertTrue(spec_is_file_pointer("See /tmp/spec.md"))

    def test_short_spec_with_full_phrase_is_a_pointer(self) -> None:
        self.assertTrue(spec_is_file_pointer("Do exactly what it says in spec.md"))

    def test_long_spec_with_inline_path_is_not_a_pointer(self) -> None:
        long = "First, read background.md for context. Then " + ("do something useful. " * 30)
        self.assertFalse(spec_is_file_pointer(long))

    def test_short_spec_without_path_is_not_a_pointer(self) -> None:
        self.assertFalse(spec_is_file_pointer("Create hello.txt in the workdir"))

    def test_dot_relative_path_does_not_match_pointer_regex(self) -> None:
        # The pointer regex disallows periods in the path preamble, so a
        # short spec like "Read ./instructions.md" is not classified as a
        # file-pointer spec.
        self.assertFalse(spec_is_file_pointer("Read ./instructions.md"))


class StripCommonRedirectionsTests(unittest.TestCase):
    def test_strips_trailing_stderr_redirect(self) -> None:
        self.assertEqual("test -f /tmp/foo", strip_common_redirections("test -f /tmp/foo 2>/dev/null"))

    def test_strips_trailing_stdout_redirect(self) -> None:
        self.assertEqual("echo ok", strip_common_redirections("echo ok >/dev/null"))

    def test_strips_ampersand_redirect(self) -> None:
        self.assertEqual("echo ok", strip_common_redirections("echo ok 2>&1"))

    def test_no_redirect_unchanged(self) -> None:
        self.assertEqual("grep -q ready out", strip_common_redirections("grep -q ready out"))


class HasCommandPrefixTests(unittest.TestCase):
    def test_command_with_matching_prefix(self) -> None:
        self.assertTrue(has_command_prefix("git status --short", ("git", "status")))

    def test_command_with_partial_prefix_does_not_match(self) -> None:
        self.assertFalse(has_command_prefix("git status --short", ("git", "log")))

    def test_command_without_prefix_does_not_match(self) -> None:
        self.assertFalse(has_command_prefix("echo ok", ("git", "status")))

    def test_empty_prefix_matches_any_command(self) -> None:
        self.assertTrue(has_command_prefix("anything", ()))


class FileTestOpsTests(unittest.TestCase):
    def test_file_test_ops_contains_common_flags(self) -> None:
        for op in ("-e", "-f", "-s", "-d", "-r", "-w", "-x", "-L"):
            self.assertIn(op, FILE_TEST_OPS)


if __name__ == "__main__":
    unittest.main()
