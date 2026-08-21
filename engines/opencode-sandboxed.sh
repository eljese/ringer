#!/bin/bash
# Ringer engine wrapper: run OpenCode under a host sandbox.
#
# OpenCode has no OS-level sandbox of its own. This wrapper supplies the real
# containment: network and reads remain available, while writes are confined
# to the task directory, a per-run scratch/cache directory, and OpenCode's
# state/config directories. macOS uses Seatbelt; Linux uses bubblewrap.
#
# Usage (as a Ringer engine bin):
#   opencode-sandboxed.sh <taskdir> [--no-sandbox] <opencode args...>
#
# The first argument is the task directory (pass "{taskdir}" first in
# args_template). "--no-sandbox" as the second argument skips host
# containment; wire it as full_access_args so Ringer's allow_full_access gate
# still applies.
set -euo pipefail

TASKDIR="${1:?usage: opencode-sandboxed.sh <taskdir> [--no-sandbox] <args...>}"
shift
SANDBOX=1
if [ "${1:-}" = "--no-sandbox" ]; then SANDBOX=0; shift; fi

# Resolve opencode without tripping `set -e` (command -v returns nonzero when absent).
if ! OPENCODE_BIN="$(command -v opencode)" || [ -z "$OPENCODE_BIN" ]; then
  echo "opencode-sandboxed.sh: opencode not found on PATH" >&2
  exit 127
fi

if [ "$SANDBOX" = "0" ]; then
  exec "$OPENCODE_BIN" "$@" < /dev/null
fi

TASKDIR_REAL="$(cd "$TASKDIR" && pwd -P)"

# Per-run scratch root — becomes both TMPDIR and XDG_CACHE_HOME for OpenCode.
SCRATCH="$(cd "$(mktemp -d -t ringer-opencode-scratch.XXXXXX)" && pwd -P)"
PROFILE="$(mktemp -t ringer-opencode-prof.XXXXXX)"
cleanup() { rm -rf "$SCRATCH" "$PROFILE"; }
trap cleanup EXIT

export TMPDIR="$SCRATCH"
export XDG_CACHE_HOME="$SCRATCH/cache"
mkdir -p "$XDG_CACHE_HOME"

case "$(uname -s)" in
  Darwin)
    if [ ! -x /usr/bin/sandbox-exec ]; then
      echo "opencode-sandboxed.sh: /usr/bin/sandbox-exec not available." >&2
      exit 1
    fi

    # Paths are passed to the profile via sandbox-exec -D parameters, NOT
    # interpolated into the profile — task paths cannot inject rules.
    cat > "$PROFILE" <<'SBEOF'
(version 1)
(allow default)
(deny file-write*)
(allow file-write*
  (subpath (param "TASKDIR"))
  (subpath (param "SCRATCH"))
  (subpath (param "OC_SHARE"))
  (subpath (param "OC_STATE"))
  (subpath (param "OC_CONFIG")))
; /dev is needed for /dev/null, /dev/urandom, etc.
(allow file-write-data
  (literal "/dev/null")
  (literal "/dev/dtracehelper")
  (literal "/dev/tty"))
SBEOF

    set +e
    /usr/bin/sandbox-exec \
      -D "TASKDIR=$TASKDIR_REAL" \
      -D "SCRATCH=$SCRATCH" \
      -D "OC_SHARE=$HOME/.local/share/opencode" \
      -D "OC_STATE=$HOME/.local/state/opencode" \
      -D "OC_CONFIG=$HOME/.config/opencode" \
      -f "$PROFILE" "$OPENCODE_BIN" "$@" < /dev/null
    status=$?
    set -e
    exit "$status"
    ;;
  Linux)
    BWRAP_BIN="${RINGER_OPENCODE_BWRAP_BIN:-/usr/bin/bwrap}"
    if [ ! -x "$BWRAP_BIN" ]; then
      echo "opencode-sandboxed.sh: bubblewrap not found at $BWRAP_BIN." >&2
      echo "Install bubblewrap or use the explicit full-access mode (--no-sandbox)." >&2
      exit 1
    fi

    # OpenCode writes logs, its database, and provider state under these
    # directories. They, the taskdir, scratch, and optional extra binds
    # (manifest repo after --tmpfs /tmp) are the writable paths in the Linux
    # mount namespace; the rest of the filesystem is read-only.
    OC_SHARE="$HOME/.local/share/opencode"
    OC_STATE="$HOME/.local/state/opencode"
    OC_CONFIG="$HOME/.config/opencode"
    mkdir -p "$OC_SHARE" "$OC_STATE" "$OC_CONFIG"

    extra_binds=()
    if [ -n "${RINGER_OPENCODE_EXTRA_BINDS:-}" ]; then
      oldifs="${IFS-}"
      IFS="${RINGER_OPENCODE_BIND_SEP:-:}"
      set -f
      # shellcheck disable=SC2086
      for extra in ${RINGER_OPENCODE_EXTRA_BINDS}; do
        [ -n "$extra" ] || continue
        case "$extra" in
          /|/home|/tmp) continue ;;
        esac
        if [ ! -d "$extra" ]; then
          continue
        fi
        extra_real="$(cd "$extra" && pwd -P)"
        case "$extra_real" in
          /|/home|/tmp) continue ;;
        esac
        extra_binds+=(--bind "$extra_real" "$extra_real")
      done
      set +f
      IFS="$oldifs"
    fi

    set +e
    "$BWRAP_BIN" \
      --die-with-parent \
      --new-session \
      --unshare-pid \
      --unshare-ipc \
      --ro-bind / / \
      --dev /dev \
      --proc /proc \
      --tmpfs /tmp \
      --dir "$SCRATCH" \
      --bind "$TASKDIR_REAL" "$TASKDIR_REAL" \
      --bind "$SCRATCH" "$SCRATCH" \
      --bind "$OC_SHARE" "$OC_SHARE" \
      --bind "$OC_STATE" "$OC_STATE" \
      --bind "$OC_CONFIG" "$OC_CONFIG" \
      "${extra_binds[@]}" \
      --setenv TMPDIR "$SCRATCH" \
      --setenv XDG_CACHE_HOME "$XDG_CACHE_HOME" \
      --chdir "$TASKDIR_REAL" \
      "$OPENCODE_BIN" "$@" < /dev/null
    status=$?
    set -e
    exit "$status"
    ;;
  *)
    echo "opencode-sandboxed.sh: unsupported host OS $(uname -s)." >&2
    exit 1
    ;;
esac
