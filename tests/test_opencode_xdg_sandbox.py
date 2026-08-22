from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "engines" / "opencode-sandboxed.sh"


def executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux bubblewrap path")
class OpenCodeXdgSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.task = self.root / "task"
        self.runtime = self.root / "runtime"
        for path in (self.bin, self.task, self.runtime / "home"):
            path.mkdir(parents=True)
        self.args = self.root / "args.txt"
        executable(
            self.bin / "bwrap",
            "#!/bin/bash\nprintf '%s\\n' \"$@\" > '" + str(self.args) + "'\n",
        )
        executable(self.bin / "opencode", "#!/bin/bash\nexit 0\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                "HOME": str(self.runtime / "home"),
                "XDG_DATA_HOME": str(self.runtime / "data"),
                "XDG_STATE_HOME": str(self.runtime / "state"),
                "XDG_CONFIG_HOME": str(self.runtime / "config"),
                "RINGER_RUNTIME_ROOT": str(self.runtime),
                "RINGER_OPENCODE_BWRAP_BIN": str(self.bin / "bwrap"),
            }
        )
        return env

    def test_active_xdg_opencode_paths_are_rw_bound(self) -> None:
        env = self.env()
        result = subprocess.run(
            [str(WRAPPER), str(self.task), "run", "noop"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.args.read_text(encoding="utf-8").splitlines()
        for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
            expected = str((Path(env[name]) / "opencode").resolve())
            self.assertEqual(args.count(expected), 2)
        for option in ("--setenv",):
            self.assertIn(option, args)
        for name in ("TMPDIR", "XDG_CACHE_HOME"):
            value = args[args.index(name) + 1]
            self.assertTrue(Path(value).resolve().is_relative_to(self.runtime.resolve()))

    def test_duplicate_xdg_bind_targets_are_bound_once(self) -> None:
        env = self.env()
        shared = self.runtime / "shared"
        env["XDG_DATA_HOME"] = str(shared)
        env["XDG_STATE_HOME"] = str(shared)
        env["XDG_CONFIG_HOME"] = str(shared)
        result = subprocess.run(
            [str(WRAPPER), str(self.task), "run", "noop"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.args.read_text(encoding="utf-8").splitlines()
        target = str((shared / "opencode").resolve())
        self.assertEqual(args.count(target), 2)

    def test_xdg_path_outside_runtime_is_rejected(self) -> None:
        env = self.env()
        env["XDG_DATA_HOME"] = str(self.root / "outside")
        result = subprocess.run(
            [str(WRAPPER), str(self.task), "run", "noop"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("escapes RINGER_RUNTIME_ROOT", result.stderr)
        self.assertFalse(self.args.exists())


if __name__ == "__main__":
    unittest.main()
