#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _make_codex_binary(tmp: Path) -> None:
    """Stage a fake 'codex' binary inside tmp/bin so codex_cli_present() returns True."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True, parents=True)
    codex_path = bin_dir / "codex"
    # Use sys.executable directly in shebang to avoid PATH lookup issues.
    codex_path.write_text(
        f"#!{sys.executable}\n"
        "import sys, os\n"
        "log_path = os.environ.get('FAKE_CODEX_LOG')\n"
        "if log_path:\n"
        "    with open(log_path, 'a') as f:\n"
        "        f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('FAKE_CODEX_FAIL') == '1':\n"
        "    sys.stderr.write('Fake Codex Error\\n')\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n",
        encoding="utf-8"
    )
    codex_path.chmod(0o755)


class InstallAgentCodexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.ringer_home = Path(self.tmp.name) / "ringer-home"
        self.fake_bin_root = Path(self.tmp.name) / "fake-bin"
        self.fake_bin_root.mkdir()
        self.home.mkdir()
        self.fake_codex_log = Path(self.tmp.name) / "fake_codex.log"
        _make_codex_binary(self.fake_bin_root)

    def run_cli(self, *args: str, cwd: Path = ROOT, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["RINGER_HOME"] = str(self.ringer_home)
        # Replace PATH with a minimal one so the fake bin is the only place
        # `codex` can be discovered. We keep /usr/bin and /bin around so that
        # the python interpreter and other utilities remain resolvable. This
        # guards against a host with a real `codex` shim earlier on PATH
        # silently masking our test stub.
        minimal_path = os.pathsep.join(
            [str(self.fake_bin_root / "bin"), "/usr/bin", "/bin"]
        )
        env["PATH"] = minimal_path
        env["FAKE_CODEX_LOG"] = str(self.fake_codex_log)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "ringer.py", *args],
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def read_marketplace(self, project_path: Path | None = None) -> dict[str, object]:
        base = project_path if project_path is not None else self.home
        p = base / ".agents" / "plugins" / "marketplace.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def read_config(self, project_path: Path | None = None) -> dict[object, object]:
        base = project_path if project_path is not None else self.home
        p = base / ".codex" / "config.toml"
        if not p.exists():
            return {}
        with p.open("rb") as fh:
            return tomllib.load(fh)

    def test_fresh_user_install(self) -> None:
        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        staged_dir = self.home / "plugins" / "ringer"
        self.assertTrue((staged_dir / ".codex-plugin" / "plugin.json").exists())
        self.assertTrue((staged_dir / "skills" / "ringer" / "SKILL.md").exists())
        self.assertTrue((staged_dir / "hooks" / "hooks.json").exists())
        self.assertTrue((staged_dir / "hooks" / "ringer_nudge.py").exists())

        # Check version has cachebuster suffix matching our new format
        manifest = json.loads((staged_dir / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertTrue(
            re.match(r"^1\.0\.0\+codex\.local-\d{8}-\d{6}-\d{6}$", manifest["version"]),
            f"invalid version format: {manifest['version']}"
        )

        # Check marketplace array schema and interface displayName
        mp = self.read_marketplace()
        self.assertEqual("personal", mp["name"])
        self.assertEqual("Personal", mp["interface"]["displayName"])
        plugins = mp["plugins"]
        self.assertIsInstance(plugins, list)
        self.assertEqual(1, len(plugins))
        ringer_entry = plugins[0]
        self.assertEqual("ringer", ringer_entry["name"])
        self.assertEqual({"source": "local", "path": "./plugins/ringer"}, ringer_entry["source"])
        self.assertEqual("AVAILABLE", ringer_entry["policy"]["installation"])
        self.assertEqual("ON_INSTALL", ringer_entry["policy"]["authentication"])
        self.assertEqual("Developer Tools", ringer_entry["category"])

        # Check executed command
        log_lines = self.fake_codex_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertIn("plugin add ringer@personal", log_lines)

    def test_cachebuster_replacement_no_nesting(self) -> None:
        # First install
        result1 = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result1.returncode, result1.stderr)
        staged_dir = self.home / "plugins" / "ringer"
        v1 = json.loads((staged_dir / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]

        # Second install - no sleep needed since microsecond resolution guarantees difference
        result2 = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result2.returncode, result2.stderr)
        v2 = json.loads((staged_dir / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]

        self.assertNotEqual(v1, v2)
        # Verify only one + exists (no nesting)
        self.assertEqual(1, v2.count("+"))
        self.assertTrue(re.match(r"^1\.0\.0\+codex\.local-\d{8}-\d{6}-\d{6}$", v2), f"nested: {v2}")

    def test_preservation_of_unrelated_marketplace_and_config(self) -> None:
        # Pre-seed marketplace with unrelated metadata and list plugins array
        mp_path = self.home / ".agents" / "plugins" / "marketplace.json"
        mp_path.parent.mkdir(parents=True, exist_ok=True)
        mp_path.write_text(json.dumps({
            "name": "personal",
            "unrelated_key": "unrelated_val",
            "plugins": [
                {"name": "other_plugin", "category": "Testing"}
            ]
        }), encoding="utf-8")

        # Pre-seed config.toml
        cfg_path = self.home / ".codex" / "config.toml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("personality = 'friendly'\n", encoding="utf-8")

        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        # Verify marketplace kept other fields and preserved order
        mp = self.read_marketplace()
        self.assertEqual("unrelated_val", mp["unrelated_key"])
        plugins = mp["plugins"]
        self.assertIsInstance(plugins, list)
        self.assertEqual(2, len(plugins))
        self.assertEqual("other_plugin", plugins[0]["name"])
        self.assertEqual("ringer", plugins[1]["name"])

        # Verify config.toml kept personality
        cfg = self.read_config()
        self.assertEqual("friendly", cfg["personality"])

    def test_replacement_preserves_order(self) -> None:
        mp_path = self.home / ".agents" / "plugins" / "marketplace.json"
        mp_path.parent.mkdir(parents=True, exist_ok=True)
        mp_path.write_text(json.dumps({
            "name": "custom-name",
            "plugins": [
                {"name": "plugin-a", "category": "A"},
                {"name": "ringer", "source": {"source": "local", "path": "old-path"}},
                {"name": "plugin-b", "category": "B"}
            ]
        }), encoding="utf-8")

        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        mp = self.read_marketplace()
        self.assertEqual("custom-name", mp["name"])
        plugins = mp["plugins"]
        self.assertEqual(3, len(plugins))
        self.assertEqual("plugin-a", plugins[0]["name"])
        self.assertEqual("ringer", plugins[1]["name"])
        self.assertEqual("./plugins/ringer", plugins[1]["source"]["path"])
        self.assertEqual("plugin-b", plugins[2]["name"])

    def test_malformed_marketplace_does_not_overwrite(self) -> None:
        mp_path = self.home / ".agents" / "plugins" / "marketplace.json"
        mp_path.parent.mkdir(parents=True, exist_ok=True)
        bad_json = "{"  # invalid JSON
        mp_path.write_text(bad_json, encoding="utf-8")

        result = self.run_cli("install-agent", "--no-claude")
        self.assertNotEqual(0, result.returncode)

        # Verify the file was not overwritten
        self.assertEqual(bad_json, mp_path.read_text(encoding="utf-8"))

    def test_invalid_marketplace_shape_does_not_overwrite(self) -> None:
        mp_path = self.home / ".agents" / "plugins" / "marketplace.json"
        mp_path.parent.mkdir(parents=True, exist_ok=True)
        bad_json = '{"plugins": "not-an-array"}'
        mp_path.write_text(bad_json, encoding="utf-8")

        result = self.run_cli("install-agent", "--no-claude")
        self.assertNotEqual(0, result.returncode)

        # Verify the file was not overwritten
        self.assertEqual(bad_json, mp_path.read_text(encoding="utf-8"))

    def test_staged_payload_matches(self) -> None:
        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        staged_dir = self.home / "plugins" / "ringer"
        source_dir = ROOT / "plugins" / "ringer"

        staged_hooks = staged_dir / "hooks" / "hooks.json"
        source_hooks = source_dir / "hooks" / "hooks.json"
        self.assertEqual(source_hooks.read_bytes(), staged_hooks.read_bytes())

        # Hook command must use the ${PLUGIN_ROOT} literal that Codex
        # expands at runtime; a regression to __RINGER_NUDGE_PATH__ would
        # ship a plugin whose hook command Codex never resolves.
        staged_hooks_text = staged_hooks.read_text(encoding="utf-8")
        self.assertIn("${PLUGIN_ROOT}", staged_hooks_text)
        self.assertNotIn("__RINGER_NUDGE_PATH__", staged_hooks_text)
        hooks_payload = json.loads(staged_hooks_text)
        pretooluse = hooks_payload["hooks"]["PreToolUse"]
        self.assertEqual("Bash", pretooluse[0]["matcher"])
        self.assertEqual("pre-bash", pretooluse[0]["hooks"][0]["command"].rsplit(" ", 1)[-1])

        self.assertEqual(
            (ROOT / ".claude" / "skills" / "ringer" / "SKILL.md").read_bytes(),
            (staged_dir / "skills" / "ringer" / "SKILL.md").read_bytes(),
        )
        self.assertEqual(
            (ROOT / "hooks" / "ringer_nudge.py").read_bytes(),
            (staged_dir / "hooks" / "ringer_nudge.py").read_bytes(),
        )

    def test_exact_add_remove_commands(self) -> None:
        self.run_cli("install-agent", "--no-claude")
        self.run_cli("uninstall-agent", "--no-claude")

        log_lines = self.fake_codex_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(["plugin add ringer@personal", "plugin remove ringer@personal"], log_lines)

    def test_add_failure_behavior(self) -> None:
        result = self.run_cli("install-agent", "--no-claude", extra_env={"FAKE_CODEX_FAIL": "1"})
        self.assertNotEqual(0, result.returncode)

    def test_remove_failure_behavior_preserves_artifacts(self) -> None:
        # Install successfully first
        self.run_cli("install-agent", "--no-claude")

        staged_dir = self.home / "plugins" / "ringer"
        self.assertTrue(staged_dir.exists())
        mp = self.read_marketplace()
        self.assertTrue(any(p.get("name") == "ringer" for p in mp.get("plugins", [])))

        # Force uninstall to fail
        result = self.run_cli("uninstall-agent", "--no-claude", extra_env={"FAKE_CODEX_FAIL": "1"})
        self.assertNotEqual(0, result.returncode)

        # Artifacts must be preserved
        self.assertTrue(staged_dir.exists())
        mp_after = self.read_marketplace()
        self.assertTrue(any(p.get("name") == "ringer" for p in mp_after.get("plugins", [])))

    def test_legacy_cleanup(self) -> None:
        legacy_dir = self.home / ".codex" / "plugins" / "ringer"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "hooks.json").write_text("{}", encoding="utf-8")

        cfg_path = self.home / ".codex" / "config.toml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("[plugins.ringer]\nenabled = true\n", encoding="utf-8")

        self.run_cli("install-agent", "--no-claude")

        self.assertFalse(legacy_dir.exists())
        cfg = self.read_config()
        self.assertNotIn("plugins", cfg)

    @unittest.skipIf(sys.platform == "win32", "requires symlink privileges")
    def test_project_scope(self) -> None:
        project = Path(self.tmp.name) / "project"
        project.mkdir()
        # Create a mock/fake project directory that is NOT the real checkouts path
        os.symlink(ROOT / "ringer.py", project / "ringer.py")

        result = self.run_cli("install-agent", "--no-claude", "--project", cwd=project)
        self.assertEqual(0, result.returncode, result.stderr)

        # Stage path must go into <cwd>/.agents/plugins/ringer
        staged_dir = project / ".agents" / "plugins" / "ringer"
        self.assertTrue((staged_dir / ".codex-plugin" / "plugin.json").exists())

        # Check project marketplace and displayName
        mp = self.read_marketplace(project)
        self.assertEqual("ringer-project", mp["name"])
        self.assertEqual("Ringer Project", mp["interface"]["displayName"])
        plugins = mp["plugins"]
        self.assertIsInstance(plugins, list)
        self.assertEqual(1, len(plugins))
        self.assertEqual("./.agents/plugins/ringer", plugins[0]["source"]["path"])

        # Uninstall project
        un_result = self.run_cli("uninstall-agent", "--no-claude", "--project", cwd=project)
        self.assertEqual(0, un_result.returncode, un_result.stderr)
        self.assertFalse(staged_dir.exists())
        self.assertFalse((project / ".agents" / "plugins" / "marketplace.json").exists())
        self.assertEqual(
            ["plugin add ringer@ringer-project", "plugin remove ringer@ringer-project"],
            self.fake_codex_log.read_text(encoding="utf-8").strip().splitlines(),
        )

    def test_no_codex_flag(self) -> None:
        self.run_cli("install-agent", "--no-codex")
        self.assertFalse((self.home / ".agents").exists())

    def test_no_claude_flag(self) -> None:
        self.run_cli("install-agent", "--no-claude")
        self.assertFalse((self.home / ".claude").exists())
        self.assertTrue((self.home / ".agents").exists())

    def test_codex_missing_behavior(self) -> None:
        result = self.run_cli("install-agent", "--no-claude", extra_env={"PATH": ""})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Codex CLI not found on PATH", result.stdout)
        self.assertFalse((self.home / ".agents").exists())

    def test_uninstall_works_without_codex_cli(self) -> None:
        # Install with a working stub codex so we have state to clean up.
        self.run_cli("install-agent", "--no-claude")
        staged_dir = self.home / "plugins" / "ringer"
        mp_path = self.home / ".agents" / "plugins" / "marketplace.json"
        self.assertTrue(staged_dir.exists())
        self.assertTrue(mp_path.exists())

        # Drop the fake codex binary from PATH so the uninstall's CLI step
        # is skipped. Filesystem cleanup must still run.
        result = self.run_cli("uninstall-agent", "--no-claude", extra_env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(staged_dir.exists())
        self.assertFalse(mp_path.exists())


class TomlEmitterTests(unittest.TestCase):
    """Unit tests for the hand-rolled TOML emitter."""

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT))
        from ringer import write_toml_settings  # type: ignore[import-not-found]
        self.write = write_toml_settings

    def _roundtrip(self, payload: dict[object, object]) -> dict[object, object]:
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "config.toml"
            self.write(p, payload)
            with p.open("rb") as fh:
                return tomllib.load(fh)

    def test_round_trip_diverse_shapes(self) -> None:
        sample = {
            "personality": "pragmatic",
            "model": "gpt-5.6-terra",
            "model_reasoning_effort": "medium",
            "count": 42,
            "rate": 0.5,
            "enabled": True,
            "disabled": False,
            "features": {
                "terminal_resize_reflow": True,
                "memories": True,
            },
            "plugins": {
                "ringer": {"enabled": True},
                "github@openai-curated": {"enabled": False},
            },
            "projects": [
                {"/tmp/example-home": {"trust_level": "trusted"}},
                {"/tmp/foo": {"trust_level": "untrusted"}},
            ],
        }
        self.assertEqual(sample, self._roundtrip(sample))

    def test_escapes_special_chars_in_strings(self) -> None:
        payload = {
            "backslash": "a\\b",
            "quote": 'say "hi"',
            "newline": "line1\nline2",
            "tab": "col1\tcol2",
            "control": "\x01\x02",
        }
        self.assertEqual(payload, self._roundtrip(payload))

    def test_empty_dict_writes_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "config.toml"
            self.write(p, {})
            with p.open("rb") as fh:
                self.assertEqual({}, tomllib.load(fh))

    def test_quoted_key_with_dot_round_trips(self) -> None:
        payload = {"plugins": {"github@openai-curated": {"enabled": True}}}
        self.assertEqual(payload, self._roundtrip(payload))


if __name__ == "__main__":
    unittest.main(verbosity=2)
