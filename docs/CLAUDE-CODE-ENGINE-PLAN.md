# Plan — `claude` engine lane for Ringer

Status: **draft for review**. Captures the probe evidence, the proposed
shape of the engine block, and the work breakdown for adding
`claude` (Anthropic's Claude Code CLI, currently `claude 2.1.179`)
as a Ringer worker.

Once approved, this becomes the spec for the actual implementation PR
(different branch / different commit set, this branch carries only the
plan doc).

## Why

Today Ringer ships engine lanes for `codex` (default), `agy`, and the
`opencode` cheap-intelligence harness via `engines/opencode-sandboxed.sh`.
Operators working inside the Anthropic ecosystem have to reach for one of
those harnesses even when they want a Claude model. Wiring
`claude --print` as a first-class engine means a single
`"engine": "claude"` task can run headless Claude Code against a
Ringer taskdir, with the same retry / check / token-cost gates the
other lanes get. Also useful as a third reviewer in a swarm
(`agy-implement / claude-review`, `claude-implement / codex-review`,
etc.).

## Probe evidence (2026-07-09, on this box)

```
$ claude --version
2.1.179 (Claude Code)

$ claude -p "Reply with exactly: claude-text-probe-ok" < /dev/null
claude-text-probe-ok
# EXIT 0, stdin-closed, no hang, response alone on stdout
```

```
$ claude --output-format json -p "Reply with exactly: claude-json-probe-ok" < /dev/null
{"type":"result","subtype":"success","is_error":false,
 "duration_ms":3438,"duration_api_ms":3298,
 "usage":{"input_tokens":27866,"cache_creation_input_tokens":0,
          "cache_read_input_tokens":114,"output_tokens":44,
          "server_tool_use":{...},"service_tier":"standard",...},
 "modelUsage":{"<model_id>":{"inputTokens":27866,"outputTokens":44,
                              "cacheReadInputTokens":114,
                              "contextWindow":200000,
                              "maxOutputTokens":32000,"costUSD":0.140487}},
 "total_cost_usd":0.140487,
 ...,"stop_reason":"end_turn","session_id":"...","uuid":"..."}
# JSON shape carries usage fields and total_cost_usd.
```

```
$ claude --add-dir /tmp/claude-adddir-test --permission-mode acceptEdits \
    --output-format json \
    -p "Create probe.txt with exactly 'claude-adddir-ok' and read it back" \
    < /dev/null
# 14s end-to-end. probe.txt landed at /tmp/claude-adddir-test/probe.txt
# with the exact spec-derived content. Read-back verified.
```

```
$ claude --bare -p "Reply with exactly: claude-bare-ok" < /dev/null
claude-bare-ok
# EXIT 0. Bare mode skips hooks, plugin sync, CLAUDE.md auto-discovery,
# attribution, auto-memory. Useful for swarm use where stability beats
# personalisation.
```

### Findings

- `claude -p "<prompt>"` is the headless mode analog of `codex exec`
  and `agy -p`. Verified: stdin can be closed (`< /dev/null`); the
  prompt is the positional `prompt` arg.
- `--add-dir <abs>` scopes the agent's write tools to that directory
  (confirmed by the `probe.txt` write landing at the target path).
  Same first-party answer to the cwd bug as agy.
- `--output-format json` emits a single-line JSON with `usage` and
  `total_cost_usd` — the canonical shape for the lane's `token_regex`.
- `--permission-mode` accepts `acceptEdits`, `auto`, `bypassPermissions`,
  `default`, `dontAsk`, `plan`. `acceptEdits` is the right
  Ringer-equivalent of "sandboxed workspace": tool edits allowed, bash
  asks. `--allowedTools` / `--disallowedTools` give finer-grained
  whitelisting.
- `--dangerously-skip-permissions` exists (analog of codex's
  `--dangerously-bypass-approvals-and-sandbox` and agy's
  `--dangerously-skip-permissions`). Pair with `allow_full_access=true`
  belt-and-suspenders.
- **Hook noise** observed: at process exit, claude tries to run a
  `SessionEnd` hook and a broken one (here
  `$HOME/.claude/hooks/scripts/session-end.js` with a missing module)
  prints a stack trace to stderr. Cosmetic — exit code stays 0,
  the JSON response is emitted first. `--bare` skips hooks entirely.
  Recommend `--bare` in `args_template` for swarm runs.

## Open questions for the implementation phase

These are blockers for the actual code, not the plan. Need live
investigation on a clean config (not this box's anthropic/auth state).

1. **Model ID surface.** The probe's `modelUsage` is keyed by the
   assistant this CLI session was configured with (`MiniMax-M3` here).
   Need to confirm what IDs a normal user's `claude --model <id>` accepts
   in `claude 2.1.179` (e.g. `claude-sonnet-4-5`, `claude-opus-4-7`,
   `claude-haiku-4-5`). Default model choice in
   `config.sample.toml`: latest stable Sonnet unless the user's account
   can't reach it.
2. **`--add-dir` + cwd interaction.** Confirmed `--add-dir {abs}` works
   with an abs path in the prompt. Need to confirm `--add-dir {abs}`
   with a `cwd` that's NOT under the added dir (does clause respect
   cwd for relative paths? `agy 1.1.0` doesn't, per issue #2).
3. **`--bare` quirks.** Need to confirm `--bare -p` still honours
   `--add-dir`, `--allowedTools`, `--permission-mode`. The help blurb
   says yes (it just skips hooks + memory), but verify on this
   version before locking it into `args_template`.
4. **`token_regex` shape.** Plan: `"output_tokens"\s*:\s*([0-9]+)`
   (Ringer wants one capture group). Output tokens are the
   cost-relevant count. Alternative: combine input + output via two
   pattern matches and let Ringer take the last hit; less surgical.
5. **Wrapper or no wrapper?** `opencode-sandboxed.sh` is a Seatbelt
   wrapper because OpenCode has no OS sandbox. `claude` does have
   `--permission-mode` + `--allowedTools` for tool gating, so a
   Seatbelt wrapper is *optional* in v1. If we ship without one, the
   v2 work (post-MVP) is adding a `claude-ringer.sh` that wraps the
   engine in Seatbelt for additional OS-level containment. Decision
   flagged in `#work-breakdown` below.

## Proposed shape

### `config.sample.toml` (commented addition, near `[engines.grok]` / `[engines.agy]`)

```toml
# Claude Code (Anthropic) — Anthropic's official headless CLI.
# Install: `npm install -g @anthropic-ai/claude-code` (or the official
# installer); then `claude` interactively to authenticate.
# Ringer closes stdin, so `--print` (`-p`) is the only headless mode.
# Verified against claude 2.1.179 (2026-07-09) on Linux.
#
# Tool gating is the Claude Code equivalent of the codex/agy sandbox:
#   --permission-mode acceptEdits  allows Edit/Write/Bash for code work,
#                                  still asks for shell outside a small
#                                  safe set
#   --allowedTools "Read Edit Write Glob Grep"
#                                  locks to read/edit/grep/glob + write,
#                                  explicitly excludes Bash. Net: same
#                                  containment posture as codex
#                                  `--sandbox workspace-write`.
# Full access (`--dangerously-skip-permissions`) only fires when the
# task sets `"full_access": true` AND the config has
# `allow_full_access = true`. Same belt-and-suspenders as the other
# lanes.
#
# `--add-dir {taskdir}` scopes write tools to the taskdir (same
# first-party answer as agy's `--add-dir` — see docs/AGY.md). `--bare`
# skips CLAUDE.md auto-discovery + hook scripts (the latter can
# surface noisy stderr from broken hook files unrelated to the worker).
#
# Token output: claude --output-format json carries a `usage` block
# with `input_tokens` / `output_tokens` and `total_cost_usd`. The
# regex below captures output tokens (cost-relevant).
# Uncomment to enable.
# [engines.claude]
# bin = "claude"
# model_default = "claude-sonnet-4-5"
# args_template = [
#   "--bare",
#   "--add-dir",
#   "{taskdir}",
#   "--model",
#   "{model}",
#   "{access_args}",                 # sandbox or full_access_args
#   "{engine_args}",
#   "--output-format",
#   "json",
#   "-p",
#   "{spec}",
# ]
# sandbox_args = [
#   "--permission-mode", "acceptEdits",
#   "--allowedTools", "Read Edit Write Glob Grep",
# ]
# full_access_args = ["--dangerously-skip-permissions"]
# token_regex = "\"output_tokens\"\\s*:\\s*([0-9]+)"
```

### `ringer.py` (one-line addition)

```python
ENGINE_INSTALL_HINTS = {
    "codex": "install it with `npm install -g @openai/codex` (or `brew install --cask codex`), then run `codex login`",
    "opencode": "install it with `curl -fsSL https://opencode.ai/install | bash`, then run `opencode auth login`",
    "claude": "install it with `npm install -g @anthropic-ai/claude-code` (or the official installer at https://claude.com/install), then run `claude` interactively to authenticate",
}
```

### New doc: `docs/CLAUDE-CODE.md`

Mirror `docs/AGY.md` structure:

- Install + authenticate
- Enable the engine (config block)
- Headless mode (`-p`, `--output-format json`)
- Tool gating (`--permission-mode`, `--allowedTools`, `--disallowedTools`)
- File creation pattern (`--add-dir {taskdir}`, mirroring the AGY doc)
- Sandbox (the `acceptEdits + allowedTools` recipe)
- Smoke test (`./engines/run-claude-smoke.sh`)
- Token accounting (`--output-format json` and `usage.output_tokens`)
- Known caveats (hook scripts, `--bare` rationale, model-id surface)

### `tests/test_claude_engine.py`

Mirror `tests/test_agy_ringer.py` style: stdlib `unittest`, stub binary
in a tempdir. Cases to cover:

1. Default: `--add-dir {taskdir}` style + stub writes to taskdir.
2. `acceptEdits` permission mode flags are passed through `{access_args}`.
3. `--allowedTools` whitelist is forwarded.
4. `--output-format json` is the canonical mode; stub emits a
   one-line JSON with `usage.output_tokens`, regex captures the
   right number.
5. `--no-sandbox / bypassPermissions` mapped via `full_access_args`
   requires `--allow-full-access` (validated by ringer, not the
   wrapper).
6. `--bare -p` still writes to taskdir (no hook-script side effects).
7. Stdin-closed (`< /dev/null`) does not hang.
8. Missing claude binary exits 127 with a sharp preflight error.
9. Prompt with a newline / quotes doesn't break args parsing
   (Ringer's args_template injection handles this; the test just
   asserts exit 0 and content match).
10. Summary line — engine block has a single stderr convention in
    AGY.md; we mirror in CLAUDE-CODE.md and assert the ringer worker
    log captures the engine's natural stderr (no extra summary line
    needed since `--output-format json` is the contract).

### Smoke test fixtures

`templates/claude-smoke.json`:
```json
{
  "run_name": "claude-smoke",
  "workdir": "/tmp/ringer-claude-smoke",
  "max_parallel": 1,
  "worktrees": false,
  "tasks": [
    {
      "key": "claude-file-create",
      "engine": "claude",
      "model": "claude-sonnet-4-5",
      "spec": "Create a file named claude-smoke.txt in the current working directory. The file must contain exactly this text and nothing else: claude works with ringer",
      "expect_files": ["claude-smoke.txt"],
      "check": "printf 'claude works with ringer' > expected.txt && diff -u expected.txt claude-smoke.txt",
      "verified": "Smoke: 1-shot file write via Claude Code headless mode. Run via engines/run-claude-smoke.sh to wipe the workdir first.",
      "task_type": "probe",
      "timeout_s": 900
    }
  ]
}
```

`engines/run-claude-smoke.sh`:
```bash
#!/usr/bin/env bash
# One-shot launcher for the Claude Code smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf /tmp/ringer-claude-smoke
exec ./ringer.py run templates/claude-smoke.json "$@"
```

### v2 (post-MVP): `engines/claude-ringer.sh` Seatbelt wrapper

Mirror `engines/opencode-sandboxed.sh`. macOS Seatbelt profile:
`(allow default)` minus broad filesystem-write, plus allow-list for
{taskdir}, scratch, claude's own `~/.claude` state. The `--add-dir`
recipe above is sufficient for most users, so this is opt-in and
ships after the lane lands.

## Work breakdown (when user signs off)

Each item is a commit on `feat/claude-code-engine`. The PR body will
have a "Test plan" with all of these as checkboxes.

1. **Config + install hint**
   - `config.sample.toml`: commented `[engines.claude]` block
   - `ringer.py`: `ENGINE_INSTALL_HINTS["claude"]`
   - Run `python3 -m pytest tests/ -q` (deselects as usual). No new tests
     yet.
2. **Stub-based tests** (no live provider needed)
   - `tests/test_claude_engine.py`: 10 cases, all stdlib `unittest`
   - `python3 tests/test_claude_engine.py -v` → 10/10
3. **Docs**
   - `docs/CLAUDE-CODE.md` first pass (no live evidence yet)
   - `README.md`: add Claude Code to the engines lane list
4. **Smoke test fixtures**
   - `templates/claude-smoke.json`
   - `engines/run-claude-smoke.sh` (chmod +x)
5. **Review loop**
   - Self-review the diff end-to-end (correctness, security, race,
     bash idiom)
   - Spawn codex + agy review sub-agents (parallel); apply real findings;
     re-run tests
   - Iterate up to twice more if critical/high findings land
6. **Live smoke** (commits 4 + 5 lift to draft; runs the actual CLI)
   - Wire the user's `~/.config/ringer/config.toml` with the lane
   - `./engines/run-claude-smoke.sh` from a clean state
   - Confirm: file lands at taskdir with exact content, JSON output
     has `usage.output_tokens`, token-regex parses correctly, exit 0
     first try
   - Add a "Live smoke" section to PR body with the verifier output
7. **Open PR** (this branch)
   - Body: TL;DR, design (cite probe evidence above), behaviour, tests,
     docs, smoke, "what's not in scope" (Seatbelt wrapper), test plan
     checklist
   - URL: `https://github.com/eljese/ringer/pull/4` (next slot on the
     fork)
8. **Iterate on review**
   - Watch for codex/agy review comments; address critical/high inline,
     medium inline if cheap, low/nit deferred

## What's deliberately out of scope (v1)

- **`engines/claude-ringer.sh` Seatbelt wrapper.** v1 relies on
  `--permission-mode acceptEdits` + `--allowedTools` for tool gating.
  The Seatbelt wrapper tracks `opencode-sandboxed.sh` and is a separate
  PR.
- **Token-cost gating beyond output_tokens.** `usage.input_tokens` is
  also in the JSON but Ringer's `token_regex` has a single capture
  group; summing input+output is a follow-up if needed.
- **Multi-model catalog.** A later PR can add a small registry of
  `claude-{opus,sonnet,haiku}-<version>` aliases mapped to verified
  model IDs, similar to the `MiniMax-M3` row in the existing
  capability registry (`docs/MODEL-NOTES.md`).
- **Updating the default engine.** Today `codex` is
  `DEFAULT_ENGINE_NAME`. Operators can opt into `claude` per task via
  `"engine": "claude"`. Promoting `claude` to the default is a
  separate decision (touches `ringer.py:52`).
