#!/bin/bash
# Ringer engine wrapper: run OpenCode under a host sandbox.
set -euo pipefail

preflight_failure() {
  echo "PREFLIGHT_FAILURE" >&2
  echo "$1" >&2
  return 1
}

validate_account_opencode() {
  local account_home="$1"
  local account_uid="$2"
  local canonical_home launcher launcher_owner resolved owner mode mode_value directory

  case "$account_home" in
    /*) ;;
    *) preflight_failure "login account home is not absolute"; return 1 ;;
  esac
  if [ "$account_home" = "/" ] || [ -L "$account_home" ] || [ ! -d "$account_home" ]; then
    preflight_failure "login account home is unsafe or unavailable"
    return 1
  fi
  canonical_home="$(/usr/bin/realpath -- "$account_home")" || return 1
  if [ "$canonical_home" != "$account_home" ]; then
    preflight_failure "login account home is not canonical"
    return 1
  fi
  owner="$(/usr/bin/stat -c %u -- "$canonical_home")" || return 1
  mode="$(/usr/bin/stat -c %a -- "$canonical_home")" || return 1
  mode_value=$((8#$mode))
  if [ "$owner" != "$account_uid" ] || (( (mode_value & 0022) != 0 )); then
    preflight_failure "login account home has unsafe ownership or mode"
    return 1
  fi

  launcher="$canonical_home/.local/bin/opencode"
  if [ ! -x "$launcher" ]; then
    preflight_failure "account OpenCode launcher is unavailable"
    return 1
  fi
  launcher_owner="$(/usr/bin/stat -c %u -- "$launcher")" || return 1
  if [ "$launcher_owner" != "$account_uid" ]; then
    preflight_failure "account OpenCode launcher has an unsafe owner"
    return 1
  fi
  resolved="$(/usr/bin/realpath -- "$launcher")" || return 1
  case "$resolved" in
    "$canonical_home"/*) ;;
    *) preflight_failure "account OpenCode launcher escapes the login home"; return 1 ;;
  esac
  if [ -L "$resolved" ] || [ ! -f "$resolved" ] || [ ! -x "$resolved" ]; then
    preflight_failure "account OpenCode executable is unsafe or unavailable"
    return 1
  fi
  owner="$(/usr/bin/stat -c %u -- "$resolved")" || return 1
  mode="$(/usr/bin/stat -c %a -- "$resolved")" || return 1
  mode_value=$((8#$mode))
  if [ "$owner" != "$account_uid" ] || (( (mode_value & 0022) != 0 )); then
    preflight_failure "account OpenCode executable has unsafe ownership or mode"
    return 1
  fi

  directory="${resolved%/*}"
  while [ "$directory" != "$canonical_home" ]; do
    case "$directory" in
      "$canonical_home"/*) ;;
      *) preflight_failure "account OpenCode executable escaped the login home"; return 1 ;;
    esac
    if [ -L "$directory" ] || [ ! -d "$directory" ]; then
      preflight_failure "account OpenCode executable has an unsafe parent"
      return 1
    fi
    owner="$(/usr/bin/stat -c %u -- "$directory")" || return 1
    mode="$(/usr/bin/stat -c %a -- "$directory")" || return 1
    mode_value=$((8#$mode))
    if [ "$owner" != "$account_uid" ] || (( (mode_value & 0022) != 0 )); then
      preflight_failure "account OpenCode executable has an unsafe parent mode"
      return 1
    fi
    directory="${directory%/*}"
  done
  printf '%s\n' "$resolved"
}

resolve_account_opencode() {
  local account_uid account_record account_name record_uid account_home account_shell
  if [ ! -x /usr/bin/id ] || [ ! -x /usr/bin/getent ] || \
     [ ! -x /usr/bin/stat ] || [ ! -x /usr/bin/realpath ]; then
    preflight_failure "account OpenCode resolution tools are unavailable"
    return 1
  fi
  account_uid="$(/usr/bin/id -u)" || return 1
  account_record="$(/usr/bin/getent passwd "$account_uid")" || {
    preflight_failure "login account could not be resolved"
    return 1
  }
  IFS=: read -r account_name _ record_uid _ _ account_home account_shell \
    <<< "$account_record"
  if [ -z "$account_name" ] || [ "$record_uid" != "$account_uid" ]; then
    preflight_failure "login account record is malformed"
    return 1
  fi
  validate_account_opencode "$account_home" "$account_uid"
}

if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0
fi

TASKDIR="${1:?usage: opencode-sandboxed.sh <taskdir> [--no-sandbox] <args...>}"
shift
SANDBOX=1
if [ "${1:-}" = "--no-sandbox" ]; then SANDBOX=0; shift; fi

case "${RINGER_SAFE_USE_ACCOUNT_HOME:-}" in
  "")
    if ! OPENCODE_BIN="$(command -v opencode)" || [ -z "$OPENCODE_BIN" ]; then
      echo "opencode-sandboxed.sh: opencode not found on PATH" >&2
      exit 127
    fi
    ;;
  1)
    OPENCODE_BIN="$(resolve_account_opencode)" || exit 127
    ;;
  *)
    echo "MANIFEST_POLICY_FAILURE" >&2
    echo "RINGER_SAFE_USE_ACCOUNT_HOME must be empty or 1" >&2
    exit 2
    ;;
esac

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
