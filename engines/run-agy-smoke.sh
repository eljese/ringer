#!/usr/bin/env bash
# One-shot launcher for the agy smoke test.
#
# Wipes the smoke workdir before invoking ringer so a previous successful
# run cannot mask a new failure (Codex P2 on the smoke manifest). After
# the run, the per-task output lives under
# /tmp/ringer-agy-smoke/<task.key>/, fresh each invocation.
#
# Usage:
#   engines/run-agy-smoke.sh
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf /tmp/ringer-agy-smoke
exec ./ringer.py run templates/agy-smoke.json "$@"
