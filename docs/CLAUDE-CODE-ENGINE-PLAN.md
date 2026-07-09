# Plan — `claude` engine lane for Ringer

Status: **draft for review** (probes complete; questions Q1-Q4 resolved
by 2026-07-09 — see `docs/CLAUDE-CODE-PROBE-RESULTS.md`). Captures
the probe evidence, the proposed shape of the engine block, and the
work breakdown for adding `claude` (Anthropic's Claude Code CLI,
currently `claude 2.1.179`) as a Ringer worker.

Once approved, this becomes the spec for the actual implementation PR
(different branch / different commit set, this branch carries only the
plan + probe-results docs).

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

## Probe evidence (2026-07-09, claude 2.1.179 on Linux)

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
- `--add-dir <abs>` is a tool-path **allow-list**, not a write pin.
  Plan-side, Ringer sets cwd = taskdir and the model resolves
  relative paths against cwd — confirmed by probe case 1 (cwd
  outside `--add-dir`) and case 2 (cwd inside `--add-dir`); the
  file lands at cwd-relative in both. So `--add-dir {taskdir}` is
  sufficient; no `cd $taskdir` injection in `args_template`
  needed. **Required addition:** without `--allowedTools`, the
  agent still hits interactive permission prompts even with
  `--permission-mode acceptEdits`. Whitelist
  `"Read Edit Write Glob Grep Bash"` for unattended `-p` runs.
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
  `SessionEnd` hook and a broken one (a broken operator-installed script)
  prints a stack trace to stderr. Cosmetic — exit code stays 0,
  the JSON response is emitted first. `--bare` skips hooks entirely.
  Recommend `--bare` in `args_template` for swarm runs.

## Resolved by probes (2026-07-09)

The four questions below were resolved by live probes on
`claude 2.1.179` (this box). Full evidence in
`docs/CLAUDE-CODE-PROBE-RESULTS.md`. Summary:

1. **Model ID surface (Q1).** All 7 candidate IDs accepted
   (`claude-sonnet-4-5`, `claude-opus-4-7`, `claude-haiku-4-5`,
   `claude-sonnet-4-6`, `claude-opus-4-8`, plus the `*-latest`
   aliases `claude-3-5-sonnet-latest` / `claude-3-5-haiku-latest`).
   Recommended `model_default`: `claude-sonnet-4-6` (current-gen
   Sonnet). Avoid `*-latest` aliases in pinned config — they
   silently shift the underlying model on CLI upgrades.
2. **`--add-dir` + cwd (Q2).** `--add-dir` is a tool-path
   **allow-list**, NOT a write pin. cwd always wins for write
   resolution, regardless of whether cwd is inside `--add-dir` or
   outside it. Plan's `--add-dir {taskdir}` recipe is fine as-is —
   no `cd $taskdir` injection required in `args_template`.
   **Side finding:** `--allowedTools` is REQUIRED on this CLI build
   for unattended `-p` runs — without it, `--permission-mode
   acceptEdits` still hits interactive permission prompts and the
   session hangs.
3. **`--bare` quirks (Q3).** `--bare -p` still pin `--add-dir` and
   `--permission-mode`. With `--bare`, hook stderr noise is
   suppressed (a broken `SessionEnd` hook on the operator's box
   (path is operator-specific) otherwise prints a stack trace on
   every invocation). Recommend keeping `--bare` in
   `args_template`. **Side finding:** `--allowedTools` honoring is
   partial on this CLI build (probe agent did not always see
   `Write`); include `Bash` in the allowlist as a fallback.
4. **`token_regex` shape (Q4).** Plan's draft regex
   (`"\"output_tokens\"\\s*:\\s*([0-9]+)"`) was **broken** — the
   JSON serialization has no space between the closing quote of the
   key and the colon (`"output_tokens":27`, not `"output_tokens" :
   27`). The space-class `\s*` after the literal `output_tokens`
   matched nothing because there was no whitespace gap to absorb.
   Correct regex source (Python form):
   `"output_tokens"\s*:\s*([0-9]+)`
   — note the literal `"` before `output_tokens` so the regex lands
   on the actual JSON serialization. Captures `usage.output_tokens`
   (i.e. `27` on the probe response). Same fix applies to any
   future snake_case token key.

Note on the wrapper question (#5): confirmed there is no Seatbelt
wrapper needed for v1. `--permission-mode acceptEdits` +
`--allowedTools` provides sufficient tool gating for the
headless-CLI workflow. A `claude-ringer.sh` Seatbelt wrapper (parallel
to `engines/opencode-sandboxed.sh`) is deferred to a separate PR.

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
# model_default = "claude-sonnet-4-6"
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
#   "--allowedTools", "Read Edit Write Glob Grep Bash",
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
   one-line JSON with `usage.output_tokens` serialized as
   `"output_tokens":N` (no space after the closing quote — the
   actual JSON shape, see Q4 in
   `docs/CLAUDE-CODE-PROBE-RESULTS.md`). The
   configured regex `"\"output_tokens\"\\s*:\\s*([0-9]+)"`
   (Python source `"output_tokens"\s*:\s*([0-9]+)`) must capture
   the integer `N`.
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
      "model": "claude-sonnet-4-6",
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
   - URL: opened via `gh pr create --repo eljese/ringer`
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
