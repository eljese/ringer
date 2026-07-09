# Using `agy` (Antigravity CLI) with Ringer

`agy` is a Google Antigravity headless CLI. Ringer ships a commented `[engines.agy]`
block in `config.sample.toml` that runs `agy` as a Ringer worker.

## Install + authenticate

1. Install `agy` (see vendor docs).
2. Run `agy` interactively once to complete browser-based auth.
3. Verify what models your account can reach:
   ```bash
   agy models
   ```

## Enable the engine

Copy `config.sample.toml` to `~/.config/ringer/config.toml` and uncomment the
`[engines.agy]` block. Pick a `model_default` from your `agy models` output — the
shipped default `Gemini 3.5 Flash (High)` is verified against agy 1.1.0 (2026-07-08).

You can override the model per task via `"model": "..."` in the manifest.

## Headless mode

Ringer closes stdin for every worker. `agy -p` (alias of `--print`) is the only
mode that exits cleanly without a TTY. Verified with:

```bash
agy -p "Reply with exactly: agy-headless-ok" < /dev/null   # exits 0, prints agy-headless-ok
```

`--project {taskdir}` points `agy` at the task directory so it writes inside it.

> **Verified 2026-07-08 against agy 1.1.0:** even with `--project
> {taskdir}`, agy writes the file to
> `~/.gemini/antigravity-cli/scratch/`, not to the task directory. The
> wrapper fallback (`engines/agy-ringer.sh`) `cd`s into the task dir,
> but agy ignores the cwd for filesystem writes too. End result:
> `expect_files` and any `check` against files written by agy land in
> the scratch dir, not the taskdir.

That means the shipped smoke manifest will fail until one of these
becomes true:

- agy ships a real cwd / working-directory flag (currently it does
  not), or
- someone writes a wrapper that **also copies the scratch file** into
  the taskdir after agy exits. This is fragile because the scratch
  filename is global, so concurrent runs collide.

The cleanest fix is upstream: agy 1.2+ scope. Until then, do not rely
on agy writing into the taskdir; use it for read-only review tasks or
for work that produces plan-style output only.

## Permission prompts

For tasks that must create or modify files, prefer:

```json
"engine_args": ["--mode", "accept-edits"]
```

Avoid `plan` mode for write tasks — it only emits plans.

## Sandbox

`agy` accepts `--sandbox` directly, so `sandbox_args = []` is correct and
`sandbox_args` is wired through `{access_args}` in `args_template`. Full-access
mapping uses `--dangerously-skip-permissions` until a verified no-sandbox flag
is available.

## Smoke test

After enabling the block, use the wrapper launcher that wipes the workdir
before each invocation (otherwise a previous successful run can leave a stale
file that masks a new failure):

```bash
./engines/run-agy-smoke.sh
```

The wrapper deletes `/tmp/ringer-agy-smoke` and then runs the manifest. The
manifest creates `agy-smoke.txt` containing exactly `agy works with ringer`.
The check exits 0 only when the file matches verbatim in the freshly-wiped
workdir.

## Token accounting

**`agy` 1.1.0 does not expose per-call token counts** on stdout, stderr,
or in its `--log-file`. Verified by:

- A pure `agy -p "..."` invocation: no token count in the response or in
  the journal log.
- Inspecting `~/.gemini/antigravity-cli/conversations/<uuid>.db`
  (trajectory_meta / gen_metadata tables) — the data is a protobuf blob
  that holds the full system prompt + tools, not a per-prompt token
  total. Decoding would require the upstream `.proto` schema, which is
  not part of the public `agy` CLI.

So `token_regex = ""` in the sample config is a deliberate no-op, not
an oversight. Models-scoreboard on Ringside will show `API/PLAN:
unknown` for any agy task — informational only, not an error.

### Why no token count

`agy` is designed for long interactive sessions with many tool calls,
not single-shot prompts. The product team deliberately does not surface
a per-prompt count because it would be misleading — session cost
amortises across many calls. Ringer's scoreboard follows the same
philosophy: track what is worth scaling (decisions, attempts,
verdicts), not every token that flowed through.

### If you need token counts anyway

The portable workaround is a thin wrapper script that wraps `agy -p`
and appends `TOTAL_TOKENS=<int>` to its own stdout, where the count is
estimated externally (e.g. `tiktoken` for the prompt text, plus the
response length). The wrapper becomes `bin` in the `[engines.agy]`
config and the regex can match `TOTAL_TOKENS=(\d+)`. This is not
shipped with the engine block because the estimate is ~rough (not the
provider's own tokenizer) and not worth the complexity for an
in-band metric.

If `agy` ever ships a `--usage` / `--stats` flag that prints to stdout,
update the sample block with a real `token_regex` and document the
exact pattern.

## Known caveats

- `agy -p` can hang if permission prompts are unsatisfied; use `--mode accept-edits`.
- `--project` behavior varies by installed version; wrapper fallback exists.
- `--sandbox` may not be accepted on all versions — verify locally first.
- Full-access bypass flag is `agy`-version-specific; do not claim support
  without verifying against your installed CLI.
