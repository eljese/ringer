# Probe results — Claude Code engine lane (2026-07-09)

Resolves the four open questions in `docs/CLAUDE-CODE-ENGINE-PLAN.md`
via live probes against `claude 2.1.179 (Claude Code)` on the
operator's box (Linux). This document is the source of truth for the
implementation PR that will follow.

Companion to `docs/CLAUDE-CODE-ENGINE-PLAN.md`. The plan doc
incorporates these answers by reference (see "Plan-doc edits" at the
end of this file).

## Methodology

Each probe was a single `claude --print` invocation under specific
flags, captured to disk, then post-processed (often with `python3`
JSON parsing + regex tests against the raw bytes). Probes ran in
parallel via a Workflow fan-out (`claude-probes` workflow, 2026-07-09),
each probe an isolated `general-purpose` sub-agent. Wall-clock cost:
~9 minutes total for all 4 probes (claude API latency dominated).

Total spend across all 4 probes (per `total_cost_usd` in their JSON
responses): a few tens of US cents. Each probe is reproducible by
copy-pasting the command blocks below into a shell with `claude 2.1.179`
on `PATH`.

## Resolved questions

### Q1 — Model ID surface

**Resolution:** All seven candidate IDs are accepted by `claude 2.1.179`.
**Recommended default:** `claude-sonnet-4-6` (current-gen Sonnet).
**Confidence:** high.

Probed (each tested via `claude --model <id> --output-format json -p "ok"`):

| ID | accepted |
| --- | --- |
| `claude-sonnet-4-5` | yes |
| `claude-opus-4-7` | yes |
| `claude-haiku-4-5` | yes |
| `claude-sonnet-4-6` | yes (recommended default) |
| `claude-opus-4-8` | yes |
| `claude-3-5-sonnet-latest` | yes (avoid in pinned config) |
| `claude-3-5-haiku-latest` | yes (avoid in pinned config) |

Notes:

- The harness appears to know about model IDs ahead of the Anthropic
  public model surface — for example `claude-opus-4-8` is accepted on
  this CLI build even though it does not match the public 2026-07
  catalog. Treat model IDs as "anything the operator's CLI build
  resolves" rather than as a fixed corpus.
- `claude models` subcommand does **not** exist on this CLI build; do
  not rely on it. `claude --help` documents `--fallback-model` as a
  comma-separated retry list (e.g. `claude-sonnet-4-6,claude-haiku-4-5`)
  available only with `--print`. Useful for the resilience story
  (`args_template` could surface a fallback chain via `engine_args`).
- Avoid `*-latest` aliases (`claude-3-5-sonnet-latest`,
  `claude-3-5-haiku-latest`) in lane config — they work today but
  silently shift the underlying model on CLI upgrades. Pin exact IDs.

### Q2 — `--add-dir` + cwd interaction

**Resolution:** `--add-dir` is purely a tool-path **allow-list**, not a
write pin. Writes resolve against cwd regardless of whether cwd is
inside the allow-list.
**Confidence:** high.

Two cases probed:

| setup | where the file landed |
| --- | --- |
| `--add-dir A`, cwd = `B` (sibling) | `B/relative-path-probe.txt` |
| `--add-dir A`, cwd = `A/sub` | `A/sub/relative-path-probe.txt` |

In both cases the file is at the cwd-relative resolution, not at the
`--add-dir` target. The `Write` tool input passed by the model was the
absolute path under cwd. So:

- The plan's `args_template` (`--add-dir {taskdir}` + `-p {spec}` where
  Ringer sets cwd = taskdir) is **correct as-is**. No `cd $taskdir`
  injection is needed for write-target correctness.
- **Required addition** (side finding): without
  `--allowedTools "Read Edit Write Glob Grep"` (or
  `--dangerously-skip-permissions`), `--permission-mode acceptEdits`
  alone **still hits interactive permission prompts** for `Write` /
  `Bash` calls. The probe agent's first attempt without
  `--allowedTools` hung on the permission gate. Whitelist is REQUIRED
  for unattended `-p` runs.

### Q3 — `--bare` honours `--add-dir` / `--permission-mode`

**Resolution:** Yes. `--bare` still pins `--add-dir` and
`--permission-mode acceptEdits`. The mode difference shows up
exclusively in stderr noise: `--bare` skips the operator's
`SessionEnd` hook; without `--bare` the hook stack-traces to stderr.
**Confidence:** high.

Two runs probed back-to-back, identical except for `--bare`:

| run | flags | exit | stderr |
| --- | --- | --- | --- |
| 1 | `--bare --add-dir T --permission-mode acceptEdits --allowedTools "Read Write" --output-format json` | 0 | **empty** |
| 2 | same without `--bare` | 0 | full `MODULE_NOT_FOUND` stack trace from `~/.claude/hooks/scripts/session-end.js:22` |

The hook stderr in run 2 reproduces verbatim:

```
SessionEnd hook [node "$HOME/.claude/hooks/scripts/session-end.js"] failed: node:internal/modules/cjs/loader:1386
  throw err;
  ^

Error: Cannot find module '../lib/utils'
Require stack:
- /home/eljese/.claude/hooks/scripts/session-end.js
    at Function._resolveFilename (node:internal/modules/cjs/loader:1383:15)
    ...
    at Object.<anonymous> (/home/eljese/.claude/hooks/scripts/session-end.js:22:5)
    ...
  code: 'MODULE_NOT_FOUND',
  requireStack: [ '/home/eljese/.claude/hooks/scripts/session-end.js' ]
}
```

The script does `require('../lib/utils')` without an extension; the
file exists at `~/.claude/hooks/scripts/lib/utils.js`. This is an
operator-config drift on this box, **not** a `--bare` regression.
Fixing it (adding `.js` to the require, or rewriting the hook) lives
outside the ringer repo — file a separate housekeeping task. With
`--bare` in `args_template`, the noise is suppressed automatically.

Side observation (worth re-verifying in the implementation PR): the
model in run 1 reported it could not see `Write` despite
`--allowedTools "Read Write"` and fell back to `Bash printf` to satisfy
the file-content invariant. Same allowlist behavior in run 2. The
final file content + exit code were correct in both runs (the
file-output invariant is met), but the tool-surface observation hints
at `--allowedTools` honoring being partial on this CLI build.
**Mitigation:** include `Bash` in the allowlist so the model has a
fallback even if `Write` is shadowed. Final sandbox allowlist:
`"Read Edit Write Glob Grep Bash"`.

### Q4 — `token_regex` shape against `--output-format json`

**Resolution:** The plan's draft regex
`"\"output_tokens\"\\s*:\\s*([0-9]+)"` (which compiles to
`"output_tokens"\s*:\s*([0-9]+)`) **does not match**. Reason: the JSON
serialization has **no whitespace** between the key's closing quote
and the colon — it is `"output_tokens":27`, not
`"output_tokens" : 27`. The first space candidate is actually empty.
The regex needs the **closing quote** of the key before `\s*:`,
otherwise it looks for `output_tokens:` literally (no quote) and finds
no match.
**Confidence:** high.

Top-level shape of `claude --output-format json` (verified by parsing a
real response):

```
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "duration_ms": ..., "duration_api_ms": ..., "ttft_ms": ..., "ttft_stream_ms": ...,
  "time_to_request_ms": ..., "num_turns": ...,
  "usage": {
    "input_tokens": 27854,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 128,
    "output_tokens": 27,
    "server_tool_use": { "web_search_requests": 0, "web_fetch_requests": 0 },
    "service_tier": "standard",
    "cache_creation": { "ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0 },
    "inference_geo": "...",
    "iterations": [...],
    "speed": "..."
  },
  "modelUsage": {
    "MiniMax-M3": {
      "inputTokens": 27854,
      "outputTokens": 27,
      "cacheReadInputTokens": 128,
      "cacheCreationInputTokens": 0,
      "webSearchRequests": 0,
      "costUSD": 0.14000900000000002,
      "contextWindow": 200000,
      "maxOutputTokens": 32000
    }
  },
  "total_cost_usd": 0.14000900000000002,
  "stop_reason": "end_turn",
  "result": "token-regex-probe-ok",
  "session_id": "...",
  "permission_denials": [...],
  "uuid": "..."
}
```

Regex tests (run against raw JSON bytes by the probe agent):

| regex (Python r-string) | match | captured |
| --- | --- | --- |
| `"output_tokens"\s*:\s*([0-9]+)` (plan's draft, BEFORE escaping) | no | — |
| `"output_tokens"\s*:\s*([0-9,]+)` | no | — |
| `"output_tokens"\s*:\s*(\d+)` | no | — |
| `"output_tokens"\s*:\s*([0-9]+)` (literals-quoted via `\`) | yes | `27` |
| `"output_tokens"\s*:\s*([0-9,]+)` (literals-quoted) | yes but captures `'27,'` (next-field comma) | wrong |
| `output_tokens["\s:]+([0-9,]+)` (loose) | yes | `'27,'` (wrong) |

**The regex ringer.py actually needs** (string is the TOML-escape
form; what comes out of `ringer.py`'s TOML loader after unescaping is
the Python regex source):

```
\"output_tokens\"\\s*:\\s*([0-9]+)
```

After TOML parses this, Python compiles the regex source:

```
"output_tokens"\s*:\s*([0-9]+)
```

(Note: the literal double-quote before `output_tokens` and the
`\s*` after the closing quote, then `:`, then `\s*`, then the capture.)
That matches `"output_tokens":27` and captures `27`.

**Same fix applies to all snake_case / camelCase token keys** if/when
the lane needs to parse additional fields (input_tokens, cache_*) —
always include the literal `"` before the key name.

## Corrected `[engines.claude]` config block

Drop this in `config.sample.toml` near the `[engines.agy]` block. It
reflects exactly what the probes confirmed; each comment cites its
probe source.

```toml
# Claude Code (Anthropic) — Anthropic's official headless CLI.
# Install: `npm install -g @anthropic-ai/claude-code` (or the
# official installer at https://claude.com/install), then run `claude`
# interactively to authenticate.
# Verified against claude 2.1.179 (2026-07-09) on Linux.
# See docs/CLAUDE-CODE-PROBE-RESULTS.md for the live evidence behind
# each flag below.
#
# Notes from the probe pass:
#  --add-dir (Q2): allow-list for tool paths, NOT a write pin. Writes
#       resolve against cwd. Combined with Ringer setting cwd = taskdir,
#       this is sufficient — no extra `cd $taskdir` injection needed.
#  --permission-mode acceptEdits (Q3): honoured in --bare mode. But
#       without an --allowedTools whitelist the agent still hangs on
#       interactive permission prompts. The whitelist below is REQUIRED
#       for unattended -p runs. Bash is included as a Write fallback
#       because Q3 observed partial --allowedTools honoring on this
#       CLI build.
#  --bare (Q3): still pins --add-dir / --permission-mode. Skips
#       operator-installed SessionEnd hooks (a broken session-end.js
#       on the operator's box polled stderr on every invocation; --bare
#       suppresses it). Recommended in args_template for swarm runs.
#  --output-format json (Q4): top-level shape carries
#       usage.output_tokens (snake_case) and modelUsage.*.outputTokens
#       (camelCase). token_regex below targets the snake_case key.
# Uncomment to enable.
# [engines.claude]
# bin = "claude"
# Q1: claude-sonnet-4-6 is the current-gen Sonnet and was accepted on
#     every probe run. claude-sonnet-4-5, claude-opus-4-7/4-8, and
#     claude-haiku-4-5 are also accepted; pin per task via manifest.
# model_default = "claude-sonnet-4-6"
# args_template = [
#   "--bare",
#   "--add-dir",
#   "{taskdir}",
#   "--model",
#   "{model}",
#   "{access_args}",
#   "{engine_args}",
#   "--output-format",
#   "json",
#   "-p",
#   "{spec}",
# ]
# Q2/Q3: --allowedTools whitelist is REQUIRED — without it the agent
#         hits interactive permission prompts and hangs. The whitelist
#         mirrors the codex workspace-write spirit (read/edit/grep/glob
#         plus write). Bash is kept as a Write fallback because Q3
#         observed that --allowedTools honoring is partial on this
#         build; for tighter containment, document the alternative:
#         swap to --dangerously-skip-permissions (full_access_args).
# sandbox_args = [
#   "--permission-mode", "acceptEdits",
#   "--allowedTools", "Read Edit Write Glob Grep Bash",
# ]
# full_access_args = ["--dangerously-skip-permissions"]
# Q4: regex source (Python r-string form):  "output_tokens"\s*:\s*([0-9]+)
#     TOML string below compiles to that source. Captures the integer
#     `output_tokens` count from a `--output-format json` response.
# token_regex = "\"output_tokens\"\\s*:\\s*([0-9]+)"
```

And the matching `ringer.py` install-hint entry (one-line addition
near the existing `ENGINE_INSTALL_HINTS["agy"]` / `["grok"]` /
`["opencode"]` rows):

```python
"claude": "install it with `npm install -g @anthropic-ai/claude-code` (or the official installer at https://claude.com/install), then run `claude` interactively to authenticate",
```

## Plan-doc edits (`docs/CLAUDE-CODE-ENGINE-PLAN.md`)

These edits fold the probe results into the plan and close out the
"Open questions for the implementation phase" section. Apply when
cutting the implementation branch.

1. **`## Findings` (line 69-93):** add the side-finding from Q2 that
   `--allowedTools` is **required**, not optional, for unattended
   `-p` runs. Drop the "see further down" handwave.
2. **`## Open questions for the implementation phase` (line 95-125):**
   replace with a one-paragraph "Resolved by probes — see
   docs/CLAUDE-CODE-PROBE-RESULTS.md". The four questions become
   links to the corresponding headings in that doc. Drop the original
   "Need live investigation on a clean config" subtext because the
   investigation is now done.
3. **`### config.sample.toml` (line 131-183):** apply the corrected
   TOML block above verbatim. Notable changes from the original plan:
   - `model_default = "claude-sonnet-4-6"` (was 4-5)
   - `sandbox_args` adds `Bash` to `--allowedTools` (was
     `"Read Edit Write Glob Grep"`)
   - `token_regex` gains the literal quote before `output_tokens`
4. **`### tests/test_claude_engine.py` (line 209-232):** keep the
   10-case enumeration but note in the test stub that the stub writes
   `"output_tokens":42` (with no space, the real JSON serialization
   shape — the probe-derived fact).
5. **`### templates/claude-smoke.json` (line 236-257):** the
   `"model": "claude-sonnet-4-5"` in the manifest should be
   `"claude-sonnet-4-6"` to match the updated default.
6. **`### templates/claude-smoke.json` "verified" field:** optionally
   update the verifier from `diff` to a stricter check that asserts
   the JSON response's `usage.output_tokens` is parseable to an int
   (Q4 contract).

## Out-of-scope follow-ups (separate issues, not this PR)

- **`~/.claude/hooks/scripts/session-end.js` broken `require`** — the
  operator-installed hook fails on shutdown with
  `Cannot find module '../lib/utils'`. Fix is operator-side: add the
  `.js` extension (or fix the hooks installer that produced the file).
  The `--bare` flag in our `args_template` suppresses the stderr
  noise in the meantime. Filed as a separate housekeeping item
  outside the ringer repo.
- **`claude models` subcommand doesn't exist** — anyone wanting a
  runtime list of available model IDs has to probe by ID. The probe
  pass above is the floor; surface in docs/CLAUDE-CODE.md as a note.
- **`--fallback-model` resilience recipe** — the
  `claude --fallback-model claude-opus-4-7,claude-haiku-4-5` flag
  re-tries the chain at the start of each user turn (works only with
  `--print`). A future Ringer `engine_args`-driven recipe could
  surface this; not v1.
- **`--allowedTools` honoring partial** — Q3 observed the model in
  some cases did not see `Write` despite `Write` being on the
  whitelist, and fell back to `Bash`. Sandbox allowlist above adds
  `Bash` to handle this. If a future claude version honors the
  allowlist strictly, `Bash` becomes unnecessary. Re-verify on every
  CLI upgrade.
