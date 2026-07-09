#!/usr/bin/env bash
# ringer wrapper for `agy` (Antigravity CLI).
#
# FALLBACK ONLY. As of 2026-07-09 the verified-working recipe is just
# `agy --add-dir {taskdir} --sandbox ...`, with no wrapper. `--add-dir`
# scopes agy's write tools to {taskdir} in agy 1.1.0; `--project {taskdir}`
# does NOT (agy treats `--project` as a project-ID token, and writes
# still land in ~/.gemini/antigravity-cli/scratch/). The shipped
# config.sample.toml ships the corrected recipe.
#
# This wrapper exists for installs where `--add-dir` is unavailable or
# behaves differently (e.g. an older or newer agy where the
# workspace-binding flag regressed). If you find yourself reaching for
# it, first re-test `--add-dir` against your installed `agy --help`.
#
# What the wrapper does (legacy/compat fallback):
#
#   1. `cd`s into `{taskdir}` before invoking agy (so tool process cwd
#      is correct even though agy itself uses the scratch dir).
#
#   2. After agy exits, mirrors any file in the scratch dir whose
#      mtime is newer than an invocation-start marker into `{taskdir}`.
#      Default policy is `cp -n` (no-clobber): a file already in
#      `{taskdir}` is never overwritten. Tunables:
#        - AGY_RINGER_NO_BACK_COPY=1     skip the mirror entirely
#        - AGY_RINGER_FORCE_BACK_COPY=1  overwrite existing taskdir files
#        - AGY_RINGER_SCRATCH_DIR=/path  override the scratch root
#
#   3. Emits a summary line on stderr:
#        agy-ringer: copied=N skipped=K missing=M from <scratch>
#
#   4. Propagates agy's exit code; per-attempt Ringer retry is
#      unchanged.
#
# Limitations:
#
#   - Concurrent agy runs against the same scratch dir can collide on
#     filename; use `--max-parallel 1` for strict isolation.
#   - The retry-batch non-determinism noted in issue #2 still applies
#     to agy 1.1.0 and is not fixed here.
#
# Usage (as a Ringer engine bin):
#
#   [engines.agy]
#   bin = "/absolute/path/to/ringer/engines/agy-ringer.sh"
#   args_template = [
#     "{taskdir}",         # consumed as $1, used for cd
#     "--model", "{model}",
#     "--sandbox", "{access_args}",
#     "{engine_args}",
#     "-p", "{spec}",
#   ]
#
# Tunables (env vars, all optional):
#   AGY_RINGER_SCRATCH_DIR       override the scratch root (default:
#                                $HOME/.gemini/antigravity-cli/scratch)
#   AGY_RINGER_NO_BACK_COPY      set to 1 to skip the scratch->taskdir
#                                mirror entirely (e.g. for review-only
#                                tasks where the output lives in agy's
#                                stdout, not in files)
#   AGY_RINGER_FORCE_BACK_COPY   set to 1 to overwrite existing taskdir
#                                files with matching scratch paths
#                                (default: cp -n, no-clobber)
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: agy-ringer.sh <taskdir> [agy args...]" >&2
  exit 64
fi

taskdir="$1"
shift

if [ ! -d "$taskdir" ]; then
  echo "agy-ringer.sh: taskdir does not exist: $taskdir" >&2
  exit 66
fi

cd "$taskdir"

# Invocation-start marker. Anything in the scratch dir whose mtime is
# newer than this is treated as written by THIS agy invocation. Placed
# INSIDE the taskdir so concurrent agy runs do not all share the same
# marker (each wrapper instance touches its own file).
AGY_RINGER_START_FILE="$(mktemp -p "$taskdir" .agy-ringer-start.XXXXXX)"
cleanup_marker() { rm -f "$AGY_RINGER_START_FILE"; }
trap cleanup_marker EXIT

# Resolve `agy` so the error is sharp when it is not installed.
if ! AGY_BIN="$(command -v agy)"; then
  echo "agy-ringer.sh: agy not found on PATH" >&2
  rm -f "$AGY_RINGER_START_FILE"
  exit 127
fi

# Run agy with the remaining args. `set +e` lets us capture its exit
# code while still hitting the post-copy block.
set +e
"$AGY_BIN" "$@"
agy_status=$?
set -e

if [ "${AGY_RINGER_NO_BACK_COPY:-0}" = "1" ]; then
  exit "$agy_status"
fi

SCRATCH_DIR="${AGY_RINGER_SCRATCH_DIR:-$HOME/.gemini/antigravity-cli/scratch}"
if [ ! -d "$SCRATCH_DIR" ]; then
  exit "$agy_status"
fi

copied=0
skipped=0
missing=0
force="${AGY_RINGER_FORCE_BACK_COPY:-0}"

while IFS= read -r -d '' src; do
  rel="${src#"$SCRATCH_DIR"/}"
  dest="$taskdir/$rel"
  if [ -e "$dest" ] && [ "$force" != "1" ]; then
    skipped=$((skipped + 1))
    continue
  fi
  mkdir -p "$(dirname "$dest")"
  if [ "$force" = "1" ]; then
    if cp -f -- "$src" "$dest"; then
      copied=$((copied + 1))
    else
      missing=$((missing + 1))
    fi
  else
    # cp -n exits 1 when destination exists. Race window: another
    # process could create `dest` between the [ -e ] check and cp.
    # cp would still return 1, so re-check before counting.
    if cp -n -- "$src" "$dest" 2>/dev/null; then
      copied=$((copied + 1))
    elif [ -e "$dest" ]; then
      skipped=$((skipped + 1))
    else
      missing=$((missing + 1))
    fi
  fi
done < <(find "$SCRATCH_DIR" -type f -newer "$AGY_RINGER_START_FILE" \
  -not -path '*/__pycache__/*' \
  -not -name '*.pyc' \
  -print0 2>/dev/null || true)

echo "agy-ringer: copied=$copied skipped=$skipped missing=$missing from $SCRATCH_DIR" >&2

exit "$agy_status"
