#!/usr/bin/env bash
# One-shot launcher for the Claude Code smoke test.
# Wipes /tmp/ringer-claude-smoke so a previous successful run
# does not mask a fresh failure (claude engine writes a deterministic
# file when its spec succeeds).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf /tmp/ringer-claude-smoke
exec ./ringer.py run templates/claude-smoke.json "$@"
