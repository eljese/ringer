#!/usr/bin/env python3
"""Offline regression tests for the cross-platform OpenCode wrapper."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "engines" / "opencode-sandboxed.sh"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class OpenCodeSandboxWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="opencode-wrapper-test-")
        self.root = Path(self.temp.name)
        self.taskdir = self.root / "taskdir"
        self.stubbin = self.root / "bin"
        self.taskdir.mkdir()
        self.stubbin.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def validate_account_opencode(self, home: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/bin/bash",
                "-c",
                'source "$1"; validate_account_opencode "$2" "$3"',
                "account-opencode-test",
                str(WRAPPER),
                str(home),
                str(os.getuid()),
            ],
            env={"PATH": str(self.stubbin)},
            capture_output=True,
            text=True,
            check=False,
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux account lookup path")
    def test_account_opencode_validation_ignores_path_and_accepts_owned_binary(self) -> None:
        home = self.root / "account-home"
        launcher = home / ".local" / "bin" / "opencode"
        launcher.parent.mkdir(parents=True)
        home.chmod(0o700)
        (home / ".local").chmod(0o755)
        launcher.parent.chmod(0o755)
        write_executable(launcher, "#!/bin/bash\nexit 0\n")
        hostile = self.stubbin / "opencode"
        write_executable(hostile, "#!/bin/bash\nexit 99\n")

        result = self.validate_account_opencode(home)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(launcher.resolve()), result.stdout.strip())
        self.assertNotEqual(str(hostile), result.stdout.strip())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux account lookup path")
    def test_account_opencode_validation_rejects_symlink_escape(self) -> None:
        home = self.root / "account-home"
        launcher = home / ".local" / "bin" / "opencode"
        launcher.parent.mkdir(parents=True)
        home.chmod(0o700)
        (home / ".local").chmod(0o755)
        launcher.parent.chmod(0o755)
        outside = self.root / "outside-opencode"
        write_executable(outside, "#!/bin/bash\nexit 0\n")
        launcher.symlink_to(outside)

        result = self.validate_account_opencode(home)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("escapes the login home", result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux account lookup path")
    def test_account_opencode_validation_rejects_writable_parent(self) -> None:
        home = self.root / "account-home"
        launcher = home / ".local" / "bin" / "opencode"
        launcher.parent.mkdir(parents=True)
        home.chmod(0o700)
        (home / ".local").chmod(0o755)
        launcher.parent.chmod(0o755)
        write_executable(launcher, "#!/bin/bash\nexit 0\n")
        launcher.parent.chmod(0o775)

        result = self.validate_account_opencode(home)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unsafe parent mode", result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux account lookup path")
    def test_account_opencode_validation_checks_symlink_launcher_ancestors(self) -> None:
        home = self.root / "account-home"
        launcher = home / ".local" / "bin" / "opencode"
        target = home / "opt" / "opencode"
        launcher.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        home.chmod(0o700)
        (home / ".local").chmod(0o755)
        launcher.parent.chmod(0o775)
        target.parent.chmod(0o755)
        write_executable(target, "#!/bin/bash\nexit 0\n")
        launcher.symlink_to(target)

        result = self.validate_account_opencode(home)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unsafe parent mode", result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux account lookup path")
    def test_account_mode_pins_path_and_bwrap(self) -> None:
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'source "$1"; activate_account_mode; '
                'printf "PATH=%s\\nBWRAP=%s\\n" "$PATH" "$(select_bwrap_bin 1)"',
                "account-mode-test",
                str(WRAPPER),
            ],
            env={
                "PATH": str(self.stubbin),
                "RINGER_OPENCODE_BWRAP_BIN": str(self.stubbin / "hostile-bwrap"),
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("PATH=/usr/bin:/bin\nBWRAP=/usr/bin/bwrap\n", result.stdout)

    def test_account_home_mode_rejects_non_boolean_value(self) -> None:
        write_executable(self.stubbin / "opencode", "#!/bin/bash\nexit 0\n")
        env = os.environ.copy()
        env["PATH"] = f"{self.stubbin}{os.pathsep}{env.get('PATH', '')}"
        env["RINGER_SAFE_USE_ACCOUNT_HOME"] = str(self.root)

        result = subprocess.run(
            [str(WRAPPER), str(self.taskdir), "--no-sandbox", "run", "noop"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("MANIFEST_POLICY_FAILURE", result.stderr)

    def test_account_home_mode_rejects_no_sandbox(self) -> None:
        env = os.environ.copy()
        env["RINGER_SAFE_USE_ACCOUNT_HOME"] = "1"

        result = subprocess.run(
            [str(WRAPPER), str(self.taskdir), "--no-sandbox", "run", "noop"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("account-home mode requires the OpenCode sandbox", result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux bubblewrap path")
    def test_linux_sandbox_uses_bwrap_and_binds_taskdir(self) -> None:
        args_file = self.root / "bwrap-args.txt"
        bwrap = self.stubbin / "bwrap"
        write_executable(
            bwrap,
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$@\" > '{args_file}'\n",
        )
        write_executable(
            self.stubbin / "opencode",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        env = os.environ.copy()
        env["PATH"] = f"{self.stubbin}{os.pathsep}{env.get('PATH', '')}"
        env["RINGER_OPENCODE_BWRAP_BIN"] = str(bwrap)

        result = subprocess.run(
            [str(WRAPPER), str(self.taskdir), "run", "--auto", "noop"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        args = args_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("--ro-bind", args)
        self.assertIn("--bind", args)
        self.assertIn(str(self.taskdir), args)
        self.assertIn("--chdir", args)
        self.assertIn("run", args)
        self.assertIn("--auto", args)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux bubblewrap path")
    def test_linux_sandbox_binds_extra_dirs_from_env(self) -> None:
        args_file = self.root / "bwrap-args.txt"
        extra = self.root / "repo"
        extra.mkdir()
        bwrap = self.stubbin / "bwrap"
        write_executable(
            bwrap,
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$@\" > '{args_file}'\n",
        )
        write_executable(
            self.stubbin / "opencode",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        env = os.environ.copy()
        env["PATH"] = f"{self.stubbin}{os.pathsep}{env.get('PATH', '')}"
        env["RINGER_OPENCODE_BWRAP_BIN"] = str(bwrap)
        env["RINGER_OPENCODE_EXTRA_BINDS"] = str(extra)

        result = subprocess.run(
            [str(WRAPPER), str(self.taskdir), "run", "--auto", "noop"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        args = args_file.read_text(encoding="utf-8").splitlines()
        resolved = str(extra.resolve())
        self.assertIn(resolved, args)
        self.assertEqual("--ro-bind", args[args.index(resolved) - 1])
        self.assertIn("run", args)
        self.assertIn("--auto", args)
        run_at = args.index("run")
        self.assertLess(args.index("--tmpfs"), args.index(resolved))
        self.assertNotIn(resolved, args[run_at:])

    def test_full_access_switch_skips_bwrap(self) -> None:
        marker = self.root / "opencode-ran.txt"
        write_executable(
            self.stubbin / "opencode",
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$PWD\" > '{marker}'\n",
        )
        env = os.environ.copy()
        env["PATH"] = f"{self.stubbin}{os.pathsep}{env.get('PATH', '')}"
        env["RINGER_OPENCODE_BWRAP_BIN"] = str(self.root / "missing-bwrap")

        result = subprocess.run(
            [str(WRAPPER), str(self.taskdir), "--no-sandbox", "run", "noop"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            str(self.taskdir.resolve()),
            str(Path(marker.read_text(encoding="utf-8").strip()).resolve()),
        )


if __name__ == "__main__":
    unittest.main()
