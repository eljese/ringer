#!/bin/bash
# Ringer engine wrapper: run OpenCode under a host sandbox.
set -euo pipefail

TASKDIR="${1:?usage: opencode-sandboxed.sh <taskdir> [--no-sandbox] <args...>}"
shift
SANDBOX=1
if [ "${1:-}" = "--no-sandbox" ]; then SANDBOX=0; shift; fi

if ! OPENCODE_BIN="$(command -v opencode)" || [ -z "$OPENCODE_BIN" ]; then
  echo "opencode-sandboxed.sh: opencode not found on PATH" >&2
  exit 127
fi

if [ "$SANDBOX" = "0" ]; then
  exec "$OPENCODE_BIN" "$@" < /dev/null
fi

TASKDIR_REAL="$(cd "$TASKDIR" && pwd -P)"
if [ -n "${RINGER_RUNTIME_ROOT:-}" ]; then
  mkdir -p "$RINGER_RUNTIME_ROOT"
  RINGER_RUNTIME_ROOT_REAL="$(cd "$RINGER_RUNTIME_ROOT" && pwd -P)"
  case "$RINGER_RUNTIME_ROOT_REAL" in
    /|/home|/tmp)
      echo "opencode-sandboxed.sh: refusing broad RINGER_RUNTIME_ROOT $RINGER_RUNTIME_ROOT_REAL" >&2
      exit 1
      ;;
  esac
else
  RINGER_RUNTIME_ROOT_REAL="${TMPDIR:-/tmp}"
fi
SCRATCH="$(cd "$(mktemp -d "$RINGER_RUNTIME_ROOT_REAL/ringer-opencode-scratch.XXXXXX")" && pwd -P)"
PROFILE="$(mktemp "$RINGER_RUNTIME_ROOT_REAL/ringer-opencode-prof.XXXXXX")"
cleanup() { rm -rf "$SCRATCH" "$PROFILE"; }
trap cleanup EXIT

# Resolve OpenCode's writable directories from the active XDG contract. The
# supervisor and worker must use the same locations; falling back to HOME is
# only for callers that do not set XDG variables.
OC_SHARE_RAW="${XDG_DATA_HOME:-$HOME/.local/share}/opencode"
OC_STATE_RAW="${XDG_STATE_HOME:-$HOME/.local/state}/opencode"
OC_CONFIG_RAW="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"

resolve_writable_dir() {
  local raw="$1"
  mkdir -p "$raw"
  local resolved
  resolved="$(cd "$raw" && pwd -P)"
  case "$resolved" in
    /|/home|/tmp)
      echo "opencode-sandboxed.sh: refusing broad writable path $resolved" >&2
      exit 1
      ;;
  esac
  if [ -n "${RINGER_RUNTIME_ROOT:-}" ]; then
    case "$resolved/" in
      "$RINGER_RUNTIME_ROOT_REAL/"*) ;;
      *)
        echo "opencode-sandboxed.sh: writable OpenCode path escapes RINGER_RUNTIME_ROOT: $resolved" >&2
        exit 1
        ;;
    esac
  fi
  printf '%s\n' "$resolved"
}

OC_SHARE="$(resolve_writable_dir "$OC_SHARE_RAW")"
OC_STATE="$(resolve_writable_dir "$OC_STATE_RAW")"
OC_CONFIG="$(resolve_writable_dir "$OC_CONFIG_RAW")"

export TMPDIR="$SCRATCH"
export XDG_CACHE_HOME="$SCRATCH/cache"
mkdir -p "$XDG_CACHE_HOME"

case "$(uname -s)" in
  Darwin)
    if [ ! -x /usr/bin/sandbox-exec ]; then
      echo "opencode-sandboxed.sh: /usr/bin/sandbox-exec not available." >&2
      exit 1
    fi
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
(allow file-write-data
  (literal "/dev/null")
  (literal "/dev/dtracehelper")
  (literal "/dev/tty"))
SBEOF
    set +e
    /usr/bin/sandbox-exec \
      -D "TASKDIR=$TASKDIR_REAL" \
      -D "SCRATCH=$SCRATCH" \
      -D "OC_SHARE=$OC_SHARE" \
      -D "OC_STATE=$OC_STATE" \
      -D "OC_CONFIG=$OC_CONFIG" \
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

    bind_args=()
    seen_bind_targets=("$TASKDIR_REAL" "$SCRATCH")
    add_bind_once() {
      local target="$1"
      local existing
      for existing in "${seen_bind_targets[@]}"; do
        if [ "$existing" = "$target" ]; then
          return
        fi
      done
      seen_bind_targets+=("$target")
      bind_args+=(--bind "$target" "$target")
    }

    if [ -n "${RINGER_OPENCODE_EXTRA_BINDS:-}" ]; then
      oldifs="${IFS-}"
      IFS="${RINGER_OPENCODE_BIND_SEP:-:}"
      set -f
      # shellcheck disable=SC2086
      for extra in ${RINGER_OPENCODE_EXTRA_BINDS}; do
        [ -n "$extra" ] || continue
        case "$extra" in /|/home|/tmp) continue ;; esac
        [ -d "$extra" ] || continue
        extra_real="$(cd "$extra" && pwd -P)"
        case "$extra_real" in /|/home|/tmp) continue ;; esac
        add_bind_once "$extra_real"
      done
      set +f
      IFS="$oldifs"
    fi

    for writable in "$OC_SHARE" "$OC_STATE" "$OC_CONFIG"; do
      add_bind_once "$writable"
    done

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
      "${bind_args[@]}" \
      --setenv TMPDIR "$SCRATCH" \
      --setenv XDG_CACHE_HOME "$XDG_CACHE_HOME" \
      --setenv XDG_DATA_HOME "${XDG_DATA_HOME:-$HOME/.local/share}" \
      --setenv XDG_STATE_HOME "${XDG_STATE_HOME:-$HOME/.local/state}" \
      --setenv XDG_CONFIG_HOME "${XDG_CONFIG_HOME:-$HOME/.config}" \
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
