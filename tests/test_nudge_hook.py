#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "ringer_nudge.py"
NUDGE_TEXT = (
    "Ringer routing check: this looks like swarm-shaped work happening inline "
    "(model call/harness/edit loop outside a live Ringer run). Load the ringer "
    "skill and route it as a manifest — a single task is a one-task manifest. "
    "If the user explicitly asked for inline work, proceed."
)
WRAPPER_NUDGE_TEXT = (
    "Ringer isolation check: launch AGY-backed Ringer only through "
    "bin/ringer-safe-run. Direct agy or ringer.py from the outer Codex "
    "sandbox is blocked by policy. Do not set allow_full_access or pass "
    "--dangerously-skip-permissions. Isolation classes including "
    "CLEANUP_FAILURE are terminal; do not retry by weakening permissions "
    "or falling back to agy/ringer.py."
)


class NudgeHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.ringer_home = Path(self.temp.name) / "ringer"
        self.home.mkdir()
        self.ringer_home.mkdir()

    def run_hook(self, mode: str, payload: object | str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["RINGER_HOME"] = str(self.ringer_home)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, str(HOOK), mode],
            input=stdin,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def pre_bash_payload(self, command: str, session_id: str = "session-1") -> dict[str, object]:
        return {
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }

    def post_edit_payload(
        self,
        file_path: str,
        session_id: str = "session-1",
        tool_name: str = "Edit",
    ) -> dict[str, object]:
        return {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": {"file_path": file_path},
            "tool_response": {"success": True},
        }

    def assertNudged(
        self,
        proc: subprocess.CompletedProcess[str],
        event_name: str,
        text: str = NUDGE_TEXT,
    ) -> None:
        self.assertEqual(0, proc.returncode)
        data = json.loads(proc.stdout)
        self.assertEqual(
            {
                "hookEventName": event_name,
                "additionalContext": text,
            },
            data["hookSpecificOutput"],
        )

    def assertSilent(self, proc: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(0, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertEqual("", proc.stderr)

    def write_active_run(self, pid: int, workdir: str = "/tmp/live-ringer-work") -> None:
        path = self.ringer_home / "active-runs.json"
        payload = {
            "run-live": {
                "pid": pid,
                "identity": "test-agent",
                "run_name": "test-run",
                "workdir": workdir,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_pre_bash_nudges_on_harness_script(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("node probe-simulate.mjs"))
        self.assertNudged(proc, "PreToolUse")

    def test_pre_bash_nudges_on_provider_endpoint(self) -> None:
        payload = self.pre_bash_payload("curl https://api.anthropic.com/v1/messages", "session-2")
        proc = self.run_hook("pre-bash", payload)
        self.assertNudged(proc, "PreToolUse")

    def test_pre_bash_stays_silent_on_ordinary_command(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("ls -la"))
        self.assertSilent(proc)

    def test_pre_bash_emits_wrapper_nudge_for_ringer_py(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("python3 ringer.py run manifest.json"))
        self.assertEqual(
            WRAPPER_NUDGE_TEXT,
            json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"],
        )

    def test_pre_bash_stays_silent_for_test_ringer_py(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("python3 tests/test_ringer.py"))
        self.assertSilent(proc)

    def test_pre_bash_stays_silent_for_test_agy_ringer_py(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("python3 tests/test_agy_ringer.py"))
        self.assertSilent(proc)

    def test_pre_bash_stays_silent_when_command_contains_ringer_safe_run(self) -> None:
        proc = self.run_hook(
            "pre-bash",
            self.pre_bash_payload(
                "bin/ringer-safe-run --manifest x.json && python3 ringer.py "
                "&& agy --sandbox -p x && node probe-simulate.mjs && "
                "curl https://api.anthropic.com/v1/messages"
            ),
        )
        self.assertSilent(proc)

    def test_pre_bash_emits_wrapper_nudge_for_agy(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("agy --sandbox -p x"))
        self.assertNudged(proc, "PreToolUse", WRAPPER_NUDGE_TEXT)

    def test_pre_bash_emits_wrapper_nudge_for_agy_path(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("/usr/bin/agy -p x"))
        self.assertNudged(proc, "PreToolUse", WRAPPER_NUDGE_TEXT)

    def test_pre_bash_stays_silent_for_agy_hyphenated_filename(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("cat agy-smoke.json"))
        self.assertSilent(proc)

    def test_pre_bash_stays_silent_when_active_run_has_live_pid(self) -> None:
        self.write_active_run(os.getpid())
        proc = self.run_hook("pre-bash", self.pre_bash_payload("node probe-simulate.mjs"))
        self.assertSilent(proc)

    def test_pre_bash_dedupes_per_session(self) -> None:
        first = self.run_hook("pre-bash", self.pre_bash_payload("node probe-simulate.mjs"))
        second = self.run_hook("pre-bash", self.pre_bash_payload("curl https://api.openai.com/v1/chat/completions"))
        self.assertNudged(first, "PreToolUse")
        self.assertSilent(second)

    def test_post_edit_nudges_at_eight_edits_and_three_files_once(self) -> None:
        files = [
            "/tmp/a.py",
            "/tmp/a.py",
            "/tmp/b.py",
            "/tmp/b.py",
            "/tmp/a.py",
            "/tmp/b.py",
            "/tmp/a.py",
        ]
        for file_path in files:
            self.assertSilent(self.run_hook("post-edit", self.post_edit_payload(file_path)))

        nudged = self.run_hook("post-edit", self.post_edit_payload("/tmp/c.py"))
        self.assertNudged(nudged, "PostToolUse")

        again = self.run_hook("post-edit", self.post_edit_payload("/tmp/d.py"))
        self.assertSilent(again)

    def test_post_edit_stays_silent_for_seven_edits_and_two_files(self) -> None:
        files = ["/tmp/a.py", "/tmp/b.py", "/tmp/a.py", "/tmp/b.py", "/tmp/a.py", "/tmp/b.py", "/tmp/a.py"]
        for file_path in files:
            self.assertSilent(self.run_hook("post-edit", self.post_edit_payload(file_path, "session-2")))

    def test_malformed_stdin_exits_zero_silently(self) -> None:
        proc = self.run_hook("pre-bash", "{not json")
        self.assertSilent(proc)

    def test_hook_copies_are_byte_identical(self) -> None:
        self.assertEqual(
            (ROOT / "hooks" / "ringer_nudge.py").read_bytes(),
            (ROOT / "plugins" / "ringer" / "hooks" / "ringer_nudge.py").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
