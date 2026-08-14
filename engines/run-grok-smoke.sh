#!/usr/bin/env bash
# One-shot launcher for the grok smoke test.
#
# Wipes the smoke workdir before invoking ringer so a previous successful
# run cannot mask a new failure. After the run, the per-task output lives
# under /tmp/ringer-grok-smoke/<task.key>/, fresh each invocation.
#
# Usage:
#   engines/run-grok-smoke.sh
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf /tmp/ringer-grok-smoke
exec ./ringer.py run templates/grok-smoke.json "$@"
