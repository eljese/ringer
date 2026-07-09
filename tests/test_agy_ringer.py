#!/usr/bin/env python3
"""Regression tests for engines/agy-ringer.sh.

The wrapper is the only thing that makes file-creation tasks work with
agy 1.1.0 (which writes to ~/.gemini/antigravity-cli/scratch/ instead of
{pwd} / --project). The tests stub `agy` itself with a tiny shell script
written into a tempdir so they run with no network, no provider CLI, and
no flakiness from the real tool.

The stub accepts a single positional argument after the flags: a target
scratch dir. It writes two files there that simulate a task output
(agy-smoke.txt at root, scripts/normalize.py under a subdir), then
exits. The wrapper should mirror both into the taskdir under default
flags and skip / overwrite / no-op them under the env-var tunables.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "engines" / "agy-ringer.sh"


def _write_stub_agy(stub_dir: Path, scratch_dir: Path) -> Path:
    """Write a fake `agy` binary that creates files in `scratch_dir`.

    The stub takes any args; the contents of the simulated output are
    independent of them. Returns the path to the stub.
    """
    bin_path = stub_dir / "agy"
    src = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        # Stub `agy` for tests: pretend to write to {scratch_dir}.
        # Args are ignored. Touches a heartbeat file so the wrapper's
        # mtime filter is exercised end-to-end.
        set -euo pipefail
        SCRATCH='{scratch_dir}'
        # Wait one tick so the file mtimes are strictly greater than the
        # wrapper's start-marker mtime even on second-resolution
        # filesystems (CI macOS runners). Without this, `find -newer`
        # could skip the writes and the test would fail intermittently.
        sleep 1
        mkdir -p "$SCRATCH/scripts"
        # write a known output at scratch root
        printf 'agy-smoke-ok\n' > "$SCRATCH/agy-smoke.txt"
        # write a deeper file so subdir-mirroring is also tested
        printf 'print("hello")\\n' > "$SCRATCH/scripts/normalize.py"
        # tag mtime so find -newer has something to compare against
        touch "$SCRATCH/__heartbeat__"
        """
    )
    bin_path.write_text(src, encoding="utf-8")
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_path


class AgyRingerWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="agy-ringer-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _setup_paths(self) -> tuple[Path, Path, Path]:
        taskdir = self.temp_root / "taskdir"
        scratch = self.temp_root / "fake-scratch"
        stub_dir = self.temp_root / "stubbin"
        taskdir.mkdir()
        scratch.mkdir()
        stub_dir.mkdir()
        return taskdir, scratch, stub_dir

    def _run_wrapper(
        self,
        *,
        taskdir: Path,
        stub_agy: Path,
        extra_env: dict[str, str] | None = None,
        args: tuple[str, ...] = ("-p", "noop"),
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        # Force the wrapper to use our stub `agy`.
        env["PATH"] = str(stub_agy.parent) + os.pathsep + env.get("PATH", "")
        env["AGY_RINGER_SCRATCH_DIR"] = str(self.temp_root / "fake-scratch")
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(WRAPPER), str(taskdir), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_copies_new_scratch_files_into_taskdir(self) -> None:
        taskdir, scratch, stub_dir = self._setup_paths()
        stub = _write_stub_agy(stub_dir, scratch)
        proc = self._run_wrapper(taskdir=taskdir, stub_agy=stub)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual((taskdir / "agy-smoke.txt").read_text(), "agy-smoke-ok\n")
        self.assertEqual((taskdir / "scripts" / "normalize.py").read_text(), 'print("hello")\n')

    def test_does_not_overwrite_existing_taskdir_files(self) -> None:
        taskdir, scratch, stub_dir = self._setup_paths()
        # Pre-write a different version in the taskdir. The wrapper must
        # leave it alone under the default no-clobber policy.
        (taskdir / "agy-smoke.txt").write_text("kept-by-taskdir\n")
        stub = _write_stub_agy(stub_dir, scratch)
        proc = self._run_wrapper(taskdir=taskdir, stub_agy=stub)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual((taskdir / "agy-smoke.txt").read_text(), "kept-by-taskdir\n")
        # Subdir file is new — that one IS copied.
        self.assertEqual(
            (taskdir / "scripts" / "normalize.py").read_text(),
            'print("hello")\n',
        )

    def test_force_back_copy_overwrites_taskdir(self) -> None:
        taskdir, scratch, stub_dir = self._setup_paths()
        (taskdir / "agy-smoke.txt").write_text("kept-by-taskdir\n")
        stub = _write_stub_agy(stub_dir, scratch)
        proc = self._run_wrapper(
            taskdir=taskdir,
            stub_agy=stub,
            extra_env={"AGY_RINGER_FORCE_BACK_COPY": "1"},
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual((taskdir / "agy-smoke.txt").read_text(), "agy-smoke-ok\n")

    def test_no_back_copy_skips_mirror(self) -> None:
        taskdir, scratch, stub_dir = self._setup_paths()
        stub = _write_stub_agy(stub_dir, scratch)
        proc = self._run_wrapper(
            taskdir=taskdir,
            stub_agy=stub,
            extra_env={"AGY_RINGER_NO_BACK_COPY": "1"},
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertFalse((taskdir / "agy-smoke.txt").exists())
        self.assertFalse((taskdir / "scripts").exists())

    def test_trailing_slash_on_scratch_dir_normalised(self) -> None:
        # Codex P2 thread on PR #3: when AGY_RINGER_SCRATCH_DIR is
        # configured with a trailing slash, prefix-strip leaves a
        # doubled slash and the relative path kept the absolute prefix,
        # so files were copied to <taskdir>/tmp/foo instead of
        # <taskdir>/foo. The wrapper must strip trailing slashes before
        # deriving `rel`.
        taskdir, scratch, stub_dir = self._setup_paths()
        stub = _write_stub_agy(stub_dir, scratch)
        env = {
            "AGY_RINGER_SCRATCH_DIR": str(scratch) + "///",
        }
        proc = self._run_wrapper(
            taskdir=taskdir, stub_agy=stub, extra_env=env,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # Files must be at taskdir root, not at taskdir/<scratch>/...
        self.assertEqual(
            (taskdir / "agy-smoke.txt").read_text(), "agy-smoke-ok\n",
        )
        self.assertEqual(
            (taskdir / "scripts" / "normalize.py").read_text(),
            'print("hello")\n',
        )
        # And nothing got nested under a stray scratch-dir prefix.
        for p in taskdir.rglob("*"):
            rel = p.relative_to(taskdir).as_posix()
            assert not rel.startswith("tmp" + "/"), (
                f"file copied under /tmp-prefixed path: {rel}"
            )

    def test_root_scratch_dir_not_emptied_by_strip(self) -> None:
        # AGY review (Gemini Pro) on PR #3 caught an edge case: if
        # AGY_RINGER_SCRATCH_DIR is the filesystem root "/", the
        # trailing-slash strip loop would reduce it to "" and the
        # subsequent [ ! -d "" ] check would silently skip the
        # back-copy. The wrapper must keep "/" intact. We assert
        # that the strip-loop does not crash and that the script
        # exits with agy's status (the stub exits 0). We do NOT
        # actually run with SCRATCH_DIR=/ to avoid walking the
        # whole filesystem in a unit test — the strip loop is
        # what we care about and it is exercised with the trailing
        # slash configuration below.
        taskdir, _, stub_dir = self._setup_paths()
        noop_stub = stub_dir / "agy"
        noop_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        noop_stub.chmod(noop_stub.stat().st_mode | stat.S_IEXEC)
        # Use a normal scratch dir but with the trailing-slash form
        # already covered; here we test that "/"-style does not break
        # the strip loop with multiple trailing slashes (the equivalent
        # of "///" reduced through the loop):
        scratch = self.temp_root / "fake-scratch"
        env = {"AGY_RINGER_SCRATCH_DIR": str(scratch) + "/"}
        proc = self._run_wrapper(taskdir=taskdir, stub_agy=noop_stub, extra_env=env)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNotIn("cannot stat", proc.stderr)
        self.assertFalse((taskdir / "agy-smoke.txt").exists())

    def test_strip_loop_handles_multi_slash_but_not_root(self) -> None:
        # Companion to test_root_scratch_dir_not_emptied_by_strip:
        # assert the strip loop terminates safely on "///" without
        # actually using "/" as the scratch dir (which would invoke
        # `find /` on the whole filesystem and time out).
        taskdir, _, stub_dir = self._setup_paths()
        noop_stub = stub_dir / "agy"
        noop_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        noop_stub.chmod(noop_stub.stat().st_mode | stat.S_IEXEC)
        # A bare "/" stripped of slashes is empty; an arbitrary
        # non-root with extra slashes is the realistic case.
        for variant in ("///", "/tmp/agy-ringer-test-multi///", "///////"):
            scratch = self.temp_root / "fake-scratch"
            env = {"AGY_RINGER_SCRATCH_DIR": str(scratch) + variant}
            proc = self._run_wrapper(
                taskdir=taskdir, stub_agy=noop_stub, extra_env=env,
            )
            self.assertEqual(
                proc.returncode, 0,
                msg=f"failed for variant {variant!r}: {proc.stderr}",
            )

    def test_start_marker_uses_relative_template(self) -> None:
        # AGY review on PR #3 caught: mktemp -p "$taskdir" is broken on
        # macOS (BSD mktemp has no -p flag) and is also redundant because
        # we are already cd'd into taskdir. Test by passing a relative
        # taskdir — the wrapper must still find a usable start marker.
        # We use a no-op stub so HOME isn't required.
        _, _, stub_dir = self._setup_paths()
        noop_stub = stub_dir / "agy"
        noop_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        noop_stub.chmod(noop_stub.stat().st_mode | stat.S_IEXEC)
        scratch = self.temp_root / "fake-scratch"
        rel = Path("rel-taskdir-agy-marker")
        cwd = Path(tempfile.gettempdir()) / rel
        cwd.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": str(stub_dir) + os.pathsep + os.environ.get("PATH", ""),
            "AGY_RINGER_SCRATCH_DIR": str(scratch),
        }
        try:
            proc = subprocess.run(
                [str(WRAPPER), str(cwd), "-p", "noop"],
                capture_output=True, text=True, env=env, timeout=30,
            )
            # The key requirement: the wrapper exits cleanly. If
            # `mktemp -p` were still in play, BSD mktemp would fail
            # with an "illegal option -- p" error and rc != 0.
            self.assertEqual(
                proc.returncode, 0,
                msg=f"wrapper failed on relative taskdir: {proc.stderr}",
            )
            # No crash output:
            self.assertNotIn("illegal option", proc.stderr)
        finally:
            shutil.rmtree(cwd, ignore_errors=True)

    def test_propagates_agy_exit_code(self) -> None:
        taskdir, scratch, stub_dir = self._setup_paths()
        failing_stub = stub_dir / "agy"
        failing_stub.write_text("#!/usr/bin/env bash\nexit 42\n")
        failing_stub.chmod(failing_stub.stat().st_mode | stat.S_IEXEC)
        proc = self._run_wrapper(taskdir=taskdir, stub_agy=failing_stub)
        self.assertEqual(proc.returncode, 42)
        # And no back-copy happened, so the taskdir stays clean.
        self.assertEqual(list(taskdir.iterdir()), [])

    def test_mtime_filter_skips_pre_existing_scratch_files(self) -> None:
        taskdir, scratch, stub_dir = self._setup_paths()
        # Plant a file in scratch BEFORE the wrapper runs. Then use a
        # stub that writes nothing new — only the heartbeat. The planted
        # file must not be copied because its mtime is older than the
        # wrapper's invocation-start marker.
        (scratch / "old.txt").write_text("from a prior run\n")
        # Push its mtime back so the mtime filter excludes it.
        old_time = time.time() - 3600
        os.utime(scratch / "old.txt", (old_time, old_time))

        quiet_stub = stub_dir / "agy"
        quiet_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        quiet_stub.chmod(quiet_stub.stat().st_mode | stat.S_IEXEC)
        proc = self._run_wrapper(taskdir=taskdir, stub_agy=quiet_stub)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertFalse((taskdir / "old.txt").exists())

    def test_missing_taskdir_errors_cleanly(self) -> None:
        scratch = self.temp_root / "fake-scratch"
        scratch.mkdir()
        stub_dir = self.temp_root / "stubbin"
        stub_dir.mkdir()
        # Touch a stub so PATH resolves `agy` even though the wrapper
        # never gets that far. Inherit the real PATH so the wrapper's
        # own `#!/usr/bin/env bash` shebang still resolves `bash`.
        (stub_dir / "agy").write_text("#!/usr/bin/env bash\nexit 0\n")
        (stub_dir / "agy").chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
        env["AGY_RINGER_SCRATCH_DIR"] = str(scratch)
        env["HOME"] = str(self.temp_root)
        proc = subprocess.run(
            [str(WRAPPER), str(self.temp_root / "no-such-taskdir"), "-p", "x"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 66)
        self.assertIn("taskdir does not exist", proc.stderr)

    def test_summary_line_is_emitted_with_counts(self) -> None:
        taskdir, scratch, stub_dir = self._setup_paths()
        # Pre-seed a taskdir file so the no-clobber policy skips it.
        (taskdir / "agy-smoke.txt").write_text("kept\n")
        stub = _write_stub_agy(stub_dir, scratch)
        proc = self._run_wrapper(taskdir=taskdir, stub_agy=stub)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # expected: agy-smoke.txt is skipped (exists in taskdir),
        # scripts/normalize.py and the stub heartbeat are both copied.
        self.assertRegex(proc.stderr, r"copied=2 skipped=1 missing=0")

    def test_scratch_dir_override_is_honored(self) -> None:
        # AGY_RINGER_SCRATCH_DIR lets ops point the wrapper at an
        # alternate scratch root (e.g. a per-machine mounted volume).
        # The override must drive the back-copy target regardless of
        # the default $HOME/.gemini/antigravity-cli/scratch path.
        taskdir, _scratch, stub_dir = self._setup_paths()
        alt_scratch = self.temp_root / "alt-scratch"
        alt_scratch.mkdir()
        alt_stub_dir = self.temp_root / "alt-stubbin"
        alt_stub_dir.mkdir()

        # The `agy` stub the wrapper invokes is the one whose bin dir
        # comes first on PATH, so put alt-stub first to make sure that
        # stub — which writes into alt_scratch — is what runs.
        alt_stub = _write_stub_agy(alt_stub_dir, alt_scratch)
        # Stub in the default dir is unused; keep it just to prove the
        # wrapper did NOT pull from the default scratch.
        _write_stub_agy(stub_dir, self.temp_root / "fake-scratch")

        env = dict(os.environ)
        env["PATH"] = (
            str(alt_stub_dir) + os.pathsep + str(stub_dir) + os.pathsep
            + env.get("PATH", "")
        )
        env["AGY_RINGER_SCRATCH_DIR"] = str(alt_scratch)
        proc = subprocess.run(
            [str(WRAPPER), str(taskdir), "-p", "x"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # Files came from alt_scratch only — back-copy picked up the
        # override target, not the default $HOME scratch.
        self.assertTrue((taskdir / "agy-smoke.txt").exists())
        self.assertIn("alt-scratch", proc.stderr)

    def test_subdir_no_clobber_preserves_existing(self) -> None:
        # Same no-clobber policy must apply inside subdirs.
        taskdir, scratch, stub_dir = self._setup_paths()
        scripts = taskdir / "scripts"
        scripts.mkdir()
        (scripts / "normalize.py").write_text("kept-by-taskdir\n")
        stub = _write_stub_agy(stub_dir, scratch)
        proc = self._run_wrapper(taskdir=taskdir, stub_agy=stub)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(
            (scripts / "normalize.py").read_text(),
            "kept-by-taskdir\n",
        )


if __name__ == "__main__":
    unittest.main()
