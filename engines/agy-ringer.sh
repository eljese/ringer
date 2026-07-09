#!/usr/bin/env bash
# ringer wrapper for `agy` (Antigravity CLI).
#
# Mitigates two verified gaps in agy 1.1.0 as a Ringer worker:
#
#   1. `agy --project {taskdir}` does NOT pin filesystem writes to
#      that directory. The file lands in
#      `~/.gemini/antigravity-cli/scratch/` instead, so a Ringer
#      task's `expect_files` check against `{taskdir}` sees nothing
#      even though agy produced the file. (The wrapper `cd`s into
#      `{taskdir}` too, but agy ignores cwd for write tools.)
#
#   2. Ringer retries wrap the spec in a "missing expected files: ..."
#      preamble that pushes agy into a different execution mode; the
#      same spec is non-deterministic across attempts.
#
# This wrapper addresses (1) for the common single-file case. After
# agy exits, files written to the scratch dir DURING this invocation
# (mtime newer than a per-run marker) are mirrored into `{taskdir}`.
# The mirror uses `cp -n` by default so a file already in `{taskdir}`
# is never overwritten — correct outputs win over scratch spills from
# concurrent runs. See the concurrent-run warning in `docs/AGY.md`
# for the limit of this heuristic.
#
# (2) is not solvable in the wrapper. Use agy for read-only tasks
# (review/plan). For file-creation tasks that must succeed first try,
# use codex or another worker that respects cwd, until agy ships a
# real `--cwd` flag.
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
