#!/usr/bin/env bash
# ringer wrapper for `agy` (Antigravity CLI).
#
# Use this wrapper if your installed `agy` does not reliably set its
# working directory when invoked with `--project {taskdir}`. The wrapper
# `cd`s into the task directory before exec'ing `agy`, so the worker
# writes land in the right place.
#
# To enable, change the [engines.agy] block in your config.toml:
#
#   bin = "/absolute/path/to/ringer/engines/agy-ringer.sh"
#   args_template = [
#     "{taskdir}",      # consumed by the wrapper as $1, used for cd
#     "--model", "{model}",
#     "--sandbox", "{access_args}",
#     "{engine_args}",
#     "-p", "{spec}",
#   ]
#
# (i.e. drop "--project" from args_template — the wrapper handles
# directory isolation via cd instead.)
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: agy-ringer.sh <taskdir> <agy args...>" >&2
  exit 64
fi

taskdir="$1"
shift

cd "$taskdir"
exec agy "$@"