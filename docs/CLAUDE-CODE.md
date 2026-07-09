# Using `claude` (Claude Code CLI) with Ringer

`claude` is Anthropic's official headless CLI for Claude Code. Ringer ships a
commented `[engines.claude]` block in `config.sample.toml` that runs
`claude --print` as a Ringer worker. Verified against `claude 2.1.179` on
2026-07-09; live probe evidence in `docs/CLAUDE-CODE-PROBE-RESULTS.md`.

## Install + authenticate

1. Install `claude`:
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```
   (or the official installer at `https://claude.com/install`).
2. Run `claude` interactively once to complete browser-based auth.
3. Verify the install:
   ```bash
   claude --version
   # 2.1.179 (Claude Code)
   ```

There is no `claude models` subcommand on this CLI build (Q1), so model
discovery is by ID: try the candidate and read the response. The
recommended default is `claude-sonnet-4-6` (current-gen Sonnet, accepted on
every probe run); `claude-opus-4-7`, `claude-opus-4-8`, and
`claude-haiku-4-5` are also accepted.

## Enable the engine

Copy `config.sample.toml` to `~/.config/ringer/config.toml` and uncomment
the `[engines.claude]` block. Set `model_default = "claude-sonnet-4-6"`
(pinned ID — avoid `*-latest` aliases; they silently shift the underlying
model on CLI upgrades, per Q1). You can override the model per task via
`"model": "..."` in the manifest.

## Headless mode

Ringer closes stdin for every worker. `claude -p` (alias of `--print`) is
the only mode that exits cleanly without a TTY. Verified with:

```bash
claude -p "Reply with exactly: claude-text-probe-ok" < /dev/null
# exits 0, prints claude-text-probe-ok
```

For the canonical token-counting shape, combine `-p` with
`--output-format json`:

```bash
claude --output-format json -p "Reply with exactly: ok" < /dev/null
# single-line JSON with usage.{input_tokens,output_tokens} and total_cost_usd
```

`--bare` (Q3) still pins `--add-dir` and `--permission-mode`. The only
behavioural difference is that `--bare` skips `CLAUDE.md` auto-discovery,
plugin sync, hook scripts, attribution, and auto-memory. Recommended in
`args_template` for swarm runs: the operator-installed `SessionEnd` hook
on this box (`~/.claude/hooks/scripts/session-end.js`) prints a
`MODULE_NOT_FOUND` stack trace to stderr on every invocation; `--bare`
suppresses the noise. Exit code stays 0 in both modes.

## Tool gating

`--allowedTools` is **REQUIRED** for unattended `-p` runs, not optional.
Q2 side finding: without an `--allowedTools` whitelist,
`--permission-mode acceptEdits` still hits interactive permission prompts
for `Write` / `Bash` and the session hangs. Use the canonical whitelist:

```
"Read Edit Write Glob Grep Bash"
```

`Bash` is included deliberately as a `Write` fallback: Q3 observed that
`--allowedTools` honoring is partial on this CLI build — the model
sometimes did not see `Write` even when it was on the whitelist, and
relied on `Bash printf` to satisfy the file-output invariant. With `Bash`
in the allowlist the model has a working path either way.

`--permission-mode` accepts `acceptEdits`, `auto`, `bypassPermissions`,
`default`, `dontAsk`, `plan`. For Ringer tasks use `acceptEdits` — it
mirrors the codex `workspace-write` posture (tool edits allowed, shell
asks). `plan` mode only emits plans; do not use it for write tasks.

## File creation pattern

Use `--add-dir {taskdir}` to scope the agent's tool paths. Per Q2,
`--add-dir` is an **allow-list for tool paths, NOT a write pin** — writes
resolve against cwd regardless of whether cwd is inside the allow-list.
Two probe cases (both 14s end-to-end):

| setup | where the file landed |
| --- | --- |
| `--add-dir A`, cwd = `B` (sibling) | `B/relative-path-probe.txt` |
| `--add-dir A`, cwd = `A/sub` | `A/sub/relative-path-probe.txt` |

Ringer sets cwd = taskdir, so the file lands under taskdir. The
`args_template` recipe below is sufficient; no extra `cd $taskdir`
injection is needed.

```
--add-dir
{taskdir}
```

The `Write` tool input the model passes is the absolute path under cwd.
Keep `--add-dir` in `args_template` so the tool-path allow-list is
explicit, and pair it with the `Read Edit Write Glob Grep Bash`
`--allowedTools` whitelist from the previous section.

## Engine wrapper

No wrapper is needed for v1. `claude --print` + `--add-dir {taskdir}` +
`--permission-mode acceptEdits` + `--allowedTools "Read Edit Write Glob
Grep Bash"` is a complete first-party recipe. Contrast with
`engines/agy-ringer.sh` (see `docs/AGY.md` § Engine wrapper): agy 1.1.0
ignored cwd for filesystem writes, so the wrapper existed to mirror
`~/.gemini/antigravity-cli/scratch/` back into `{taskdir}` after agy
exited. `claude`'s `--add-dir` is a different, first-party answer to the
same problem and removes the wrapper requirement.

A `claude-ringer.sh` Seatbelt wrapper (parallel to
`engines/opencode-sandboxed.sh`) is deferred to a separate PR. The
`--permission-mode acceptEdits` + `--allowedTools` recipe above is
sufficient tool gating for the headless-CLI workflow on v1.

## Permission prompts

For tasks that must create or modify files, prefer:

```json
"engine_args": ["--permission-mode", "acceptEdits"]
```

This is the Ringer-equivalent of the codex/agy sandbox floor. Avoid `plan`
mode for write tasks — it only emits plans. The matching `sandbox_args` in
`config.sample.toml` is:

```toml
sandbox_args = [
  "--permission-mode", "acceptEdits",
  "--allowedTools", "Read Edit Write Glob Grep Bash",
]
```

`full_access_args = ["--dangerously-skip-permissions"]` is wired through
`{access_args}` and only fires when both:

- the task sets `"full_access": true`, AND
- the config has `allow_full_access = true` (the belt-and-suspenders gate).

Without that dual opt-in, the bypass is impossible regardless of what the
worker prompt says. This matches the model the codex and agy blocks use
— sandbox is the floor, full-access is an explicit per-task opt-in you
must request from a human.

## Sandbox

**Sandbox is on by default; the only opt-out is full access.** The
`args_template` for `[engines.claude]` always includes
`--permission-mode acceptEdits` and the `--allowedTools` whitelist via
`{access_args}`; `full_access_args` only fires when both the task sets
`"full_access": true` and the config has `allow_full_access = true`.

**Caveat:** `--permission-mode acceptEdits` alone is not enough. Without
`--allowedTools` the agent still hits interactive permission prompts and
the session hangs (Q2). The sandbox recipe below is the only fully
unattended form; do not split the two flags.

```bash
claude --bare \
       --add-dir /path/to/taskdir \
       --permission-mode acceptEdits \
       --allowedTools "Read Edit Write Glob Grep Bash" \
       --output-format json \
       -p "spec text" < /dev/null
```

The sandboxes are only as tight as the `--allowedTools` allowlist — a
sandbox with `Bash` on the list still lets the worker run arbitrary
shell. Tighten by removing `Bash` once `--allowedTools` honoring is
verified strict on the installed CLI build (Q3 partial-honour finding).

## Smoke test

After enabling the block, use the wrapper launcher that wipes the workdir
before each invocation (otherwise a previous successful run can leave a
stale file that masks a new failure):

```bash
./engines/run-claude-smoke.sh
```

The wrapper deletes `/tmp/ringer-claude-smoke` and then runs the manifest.
The manifest creates `claude-smoke.txt` containing exactly
`claude works with ringer`. The check exits 0 only when the file matches
verbatim in the freshly-wiped workdir.

## Token accounting

`claude --output-format json` emits a single-line JSON with a top-level
`usage` block. The lane's `token_regex` captures `usage.output_tokens`
from that block. Per Q4, the plan's draft regex
`"output_tokens"\s*:\s*([0-9]+)` was **broken** — the JSON serialization
has no space between the key's closing quote and the colon
(`"output_tokens":27`, not `"output_tokens" : 27`), so the `\s*` after the
literal `output_tokens` matched nothing.

The regex `ringer.py` actually needs, side-by-side:

```
Python regex source (what compiles):
  "output_tokens"\s*:\s*([0-9]+)
  ^                ^                 ^
  literal " before key      \s* absorbs the (zero) whitespace between
                            the key's closing quote and the colon.

TOML string in config.sample.toml (what you type):
  token_regex = "\"output_tokens\"\\s*:\\s*([0-9]+)"

After TOML parses the above, the Python source the regex engine sees is:
  "output_tokens"\s*:\s*([0-9]+)
```

Note the literal `"` before `output_tokens` — required so the regex lands
on the actual JSON serialization. Same fix applies to all snake_case
token keys (`input_tokens`, `cache_*`) if/when the lane needs to parse
additional fields.

For the camelCase counterpart (`modelUsage.<model>.outputTokens`),
`--output-format json` also serialises without a space between the key
and the colon; if the lane ever needs the per-model count, mirror the
same `"outputTokens"\s*:\s*([0-9]+)` form.

## Known caveats

- `--bare` suppresses operator-config-broken hook noise. The
  `SessionEnd` hook at `~/.claude/hooks/scripts/session-end.js` fails on
  shutdown with `Cannot find module '../lib/utils'` (operator-config
  drift, not a `claude` bug). `--bare` skips hooks; without it the stack
  trace lands on stderr. Filed as a separate housekeeping item outside
  the ringer repo.
- `Bash` in the allowlist is a `Write` fallback, not just a convenience.
  Q3 observed partial `--allowedTools` honoring on this CLI build —
  `Write` was sometimes shadowed. Remove `Bash` once `--allowedTools`
  honoring is verified strict on the installed build.
- `--allowedTools` is REQUIRED for unattended `-p` runs. Without it,
  `--permission-mode acceptEdits` still hits interactive permission
  prompts and the session hangs (Q2 side finding).
- `*-latest` aliases are unstable. `claude-3-5-sonnet-latest` and
  `claude-3-5-haiku-latest` work today but silently shift the underlying
  model on CLI upgrades. Pin exact IDs (`claude-sonnet-4-6`,
  `claude-opus-4-8`, `claude-haiku-4-5`).
- Install + auth are prerequisites, not the lane's concern. Operators
  must run `npm install -g @anthropic-ai/claude-code` (or the official
  installer) and complete browser-based auth interactively before the
  first Ringer task. The lane's preflight exits 127 with a sharp error
  if `claude` is missing from `PATH`.
- `claude models` subcommand does not exist on this CLI build. Probe
  model IDs by trial; there is no runtime list to read.
