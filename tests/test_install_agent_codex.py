#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEX_DIRNAME = ".codex"
CODEX_PLUGINS_SUBDIR = "plugins/ringer"
CODEX_HOOKS_FILENAME = "hooks.json"
CODEX_SKILL_REL = "skills/ringer/SKILL.md"
CODEX_MANIFEST_REL = ".codex-plugin/plugin.json"


def _make_codex_binary(tmp: Path) -> None:
    """Stage a fake 'codex' binary inside tmp/bin so codex_cli_present() returns True."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    bin_dir.chmod(0o755)
    codex_path = bin_dir.joinpath("codex")
    codex_path.write_text("#!/bin/sh\n# ringer-test-stub\necho codex\n", encoding="utf-8")
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
        # Add a fake codex binary so codex_cli_present() returns True for most tests.
        _make_codex_binary(self.fake_bin_root)

    def run_cli(self, *args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["RINGER_HOME"] = str(self.ringer_home)
        # Override PATH entirely so the only `codex` discoverable by shutil.which
        # is the fake one in self.fake_bin_root/bin (or absent, if the test
        # removed it). This isolates the test from the host's real codex binary.
        env["PATH"] = str(self.fake_bin_root / "bin")
        return subprocess.run(
            [sys.executable, "ringer.py", *args],
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def read_config(self) -> dict[object, object]:
        with (self.home / CODEX_DIRNAME / "config.toml").open("rb") as fh:
            return tomllib.load(fh)

    def ringer_plugin_registered(self, cfg: dict[object, object]) -> bool:
        plugins = cfg.get("plugins")
        return isinstance(plugins, dict) and plugins.get("ringer", {}).get("enabled") is True

    def test_fresh_install_copies_plugin_scaffold_under_home_codex(self) -> None:
        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        plugin_root = self.home / CODEX_DIRNAME / CODEX_PLUGINS_SUBDIR
        self.assertTrue((plugin_root / CODEX_MANIFEST_REL).exists())
        self.assertTrue((plugin_root / CODEX_HOOKS_FILENAME).exists())
        self.assertTrue((plugin_root / CODEX_SKILL_REL).exists())

    def test_fresh_install_registers_plugin_in_config_toml(self) -> None:
        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        cfg = self.read_config()
        self.assertTrue(self.ringer_plugin_registered(cfg))

    def test_plugin_hooks_json_carries_absolute_ringer_nudge_path(self) -> None:
        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        hooks_path = self.home / CODEX_DIRNAME / CODEX_PLUGINS_SUBDIR / CODEX_HOOKS_FILENAME
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
        groups = payload["hooks"]["PreToolUse"]
        self.assertEqual("Bash", groups[0]["matcher"])
        handlers = groups[0]["hooks"]
        self.assertEqual("command", handlers[0]["type"])
        self.assertIn("ringer_nudge.py", handlers[0]["command"])
        self.assertTrue(handlers[0]["command"].endswith(" pre-bash"))

    def test_skill_payload_matches_canonical_source(self) -> None:
        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        installed = self.home / CODEX_DIRNAME / CODEX_PLUGINS_SUBDIR / CODEX_SKILL_REL
        canonical = ROOT / ".claude" / "skills" / "ringer" / "SKILL.md"
        self.assertEqual(canonical.read_bytes(), installed.read_bytes())

    def test_second_install_is_idempotent(self) -> None:
        first = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, first.returncode, first.stderr)
        cfg_before = self.read_config()
        skill_before = (self.home / CODEX_DIRNAME / CODEX_PLUGINS_SUBDIR / CODEX_SKILL_REL).read_bytes()

        second = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, second.returncode, second.stderr)

        cfg_after = self.read_config()
        skill_after = (self.home / CODEX_DIRNAME / CODEX_PLUGINS_SUBDIR / CODEX_SKILL_REL).read_bytes()
        self.assertEqual(cfg_before, cfg_after)
        self.assertEqual(skill_before, skill_after)
        # No spurious backup was created on the no-op second install.
        config_dir = self.home / CODEX_DIRNAME
        backups = list(config_dir.glob("config.toml.bak-*"))
        self.assertEqual([], backups)

    def test_install_preserves_unrelated_config_toml_keys(self) -> None:
        codex_dir = self.home / CODEX_DIRNAME
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'personality = "pragmatic"\n'
            'model = "gpt-5.6-terra"\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = false\n',
            encoding="utf-8",
        )

        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        cfg = self.read_config()
        self.assertEqual("pragmatic", cfg["personality"])
        self.assertEqual("gpt-5.6-terra", cfg["model"])
        self.assertFalse(cfg["plugins"]["github@openai-curated"]["enabled"])
        self.assertTrue(cfg["plugins"]["ringer"]["enabled"])

    def test_uninstall_removes_plugin_dir_and_config_entry(self) -> None:
        install = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, install.returncode, install.stderr)

        # Add an unrelated entry to config.toml so we can verify uninstall
        # only touches [plugins.ringer].
        cfg = self.read_config()
        cfg["plugins"]["github@openai-curated"] = {"enabled": False}
        with (self.home / CODEX_DIRNAME / "config.toml").open("w", encoding="utf-8") as fh:
            from ringer import write_toml_settings  # type: ignore[import-not-found]
            write_toml_settings(self.home / CODEX_DIRNAME / "config.toml", cfg)

        uninstall = self.run_cli("uninstall-agent", "--no-claude")
        self.assertEqual(0, uninstall.returncode, uninstall.stderr)

        self.assertFalse((self.home / CODEX_DIRNAME / CODEX_PLUGINS_SUBDIR).exists())
        cfg_after = self.read_config()
        self.assertNotIn("ringer", cfg_after["plugins"])
        self.assertFalse(cfg_after["plugins"]["github@openai-curated"]["enabled"])

    def test_no_codex_flag_skips_codex_path(self) -> None:
        result = self.run_cli("install-agent", "--no-codex")
        self.assertEqual(0, result.returncode, result.stderr)

        self.assertFalse((self.home / CODEX_DIRNAME).exists())

    def test_no_claude_flag_skips_claude_path(self) -> None:
        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        self.assertFalse((self.home / ".claude").exists())
        self.assertTrue((self.home / CODEX_DIRNAME / "config.toml").exists())

    def test_codex_skipped_when_cli_not_on_path(self) -> None:
        # Override the bin dir with one that does NOT contain a codex binary.
        shutil.rmtree(self.fake_bin_root)
        self.fake_bin_root.mkdir()

        result = self.run_cli("install-agent", "--no-claude")
        self.assertEqual(0, result.returncode, result.stderr)

        self.assertFalse((self.home / CODEX_DIRNAME).exists())
        self.assertIn("Codex CLI not found on PATH", result.stdout)

    def test_project_variant_writes_under_temp_cwd(self) -> None:
        project = Path(self.tmp.name) / "project"
        project.mkdir()
        os.symlink(ROOT / "ringer.py", project / "ringer.py")

        install = self.run_cli("install-agent", "--no-claude", "--project", cwd=project)
        self.assertEqual(0, install.returncode, install.stderr)

        self.assertTrue((project / CODEX_DIRNAME / CODEX_PLUGINS_SUBDIR / CODEX_HOOKS_FILENAME).exists())
        self.assertTrue((project / CODEX_DIRNAME / "config.toml").exists())
        self.assertFalse((self.home / CODEX_DIRNAME).exists())

        uninstall = self.run_cli("uninstall-agent", "--no-claude", "--project", cwd=project)
        self.assertEqual(0, uninstall.returncode, uninstall.stderr)

        self.assertFalse((project / CODEX_DIRNAME / CODEX_PLUGINS_SUBDIR).exists())


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
                {"/home/eljese": {"trust_level": "trusted"}},
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


class HookCommandSubstitutionTests(unittest.TestCase):
    """Unit test for the placeholder substitution used by _install_agent_codex."""

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT))

    def test_substitute_placeholder(self) -> None:
        from ringer import _rewrite_plugin_hook_command  # type: ignore[import-not-found]

        with tempfile.TemporaryDirectory() as t:
            hooks_path = Path(t) / "hooks.json"
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 __RINGER_NUDGE_PATH__ pre-bash",
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            _rewrite_plugin_hook_command(hooks_path, "python3 /tmp/custom/ringer_nudge.py pre-bash")
            payload = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "python3 /tmp/custom/ringer_nudge.py pre-bash",
                payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            )


class CodexPluginRegistrationTests(unittest.TestCase):
    """Unit tests for _register_ringer_plugin / _remove_ringer_plugin."""

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT))

    def test_register_when_missing(self) -> None:
        from ringer import _register_ringer_plugin  # type: ignore[import-not-found]

        settings: dict[object, object] = {}
        self.assertTrue(_register_ringer_plugin(settings))
        self.assertEqual({"plugins": {"ringer": {"enabled": True}}}, settings)

    def test_register_when_disabled(self) -> None:
        from ringer import _register_ringer_plugin  # type: ignore[import-not-found]

        settings = {"plugins": {"ringer": {"enabled": False}}}
        self.assertTrue(_register_ringer_plugin(settings))
        self.assertEqual({"ringer": {"enabled": True}}, settings["plugins"])

    def test_register_when_already_enabled_is_noop(self) -> None:
        from ringer import _register_ringer_plugin  # type: ignore[import-not-found]

        settings = {"plugins": {"ringer": {"enabled": True}, "other": {"enabled": True}}}
        self.assertFalse(_register_ringer_plugin(settings))
        self.assertEqual(
            {"ringer": {"enabled": True}, "other": {"enabled": True}},
            settings["plugins"],
        )

    def test_remove_when_present(self) -> None:
        from ringer import _remove_ringer_plugin  # type: ignore[import-not-found]

        settings = {"plugins": {"ringer": {"enabled": True}, "other": {"enabled": True}}}
        self.assertTrue(_remove_ringer_plugin(settings))
        self.assertEqual({"other": {"enabled": True}}, settings["plugins"])

    def test_remove_when_only_entry_clears_plugins_key(self) -> None:
        from ringer import _remove_ringer_plugin  # type: ignore[import-not-found]

        settings = {"plugins": {"ringer": {"enabled": True}}}
        self.assertTrue(_remove_ringer_plugin(settings))
        self.assertNotIn("plugins", settings)

    def test_remove_when_absent_is_noop(self) -> None:
        from ringer import _remove_ringer_plugin  # type: ignore[import-not-found]

        settings = {"plugins": {"other": {"enabled": True}}}
        self.assertFalse(_remove_ringer_plugin(settings))


if __name__ == "__main__":
    unittest.main(verbosity=2)