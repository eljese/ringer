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
shipped default `gemini-3.7-flash-high` is verified against agy 1.1.13 on 2026-08-14 (Linux).

You can override the model per task via `"model": "..."` in the manifest.

## Headless mode

Ringer closes stdin for every worker. `agy -p` (alias of `--print`) is the only
mode that exits cleanly without a TTY. Verified with:

```bash
agy -p "Reply with exactly: agy-headless-ok" < /dev/null   # exits 0, prints agy-headless-ok
```

Use `--add-dir {taskdir}` to scope filesystem writes to that directory. The
flags break down as:

- `--add-dir <dir>` (repeatable): scopes the agent's write tools to `<dir>`
  and treats it as the workspace anchor — files end up under `<dir>`.
  **This is what you want for Ringer file-creation tasks.**
- `--project <id>`: a project-ID token in agy 1.1.13; does NOT pin
  filesystem writes. Naming the bug: the issue body recommended
  `--project {taskdir}`, which agy accepted but treated as a project
  identifier, so writes still landed in `~/.gemini/antigravity-cli/scratch/`.

> **Verified 2026-08-14 against agy 1.1.13 on Linux:**
> a one-task probe `agy --add-dir $td --sandbox --mode accept-edits -p
> 'Create hello.txt in current working directory …'` writes `hello.txt`
> to `$td/hello.txt` with the spec-derived content; nothing leaks to
> the scratch dir. Single-task `agy-smoke.json` passes first try under
> the corrected recipe.

If `--add-dir` is missing on your installed version or behaves
differently (re-test on every agy upgrade), fall back to
`engines/agy-ringer.sh` for that worker; the wrapper mirrors scratch
back to `{taskdir}` after agy exits.

## Engine wrapper

`engines/agy-ringer.sh` is the ringer wrapper that mitigates the cwd
gap above for the common single-file case. Use it as the
`[engines.agy].bin` when agy will create or modify files.

```toml
[engines.agy]
bin = "/absolute/path/to/ringer/engines/agy-ringer.sh"
args_template = [
  "{taskdir}",         # consumed as $1, used for cd
  "--model", "{model}",
  "--sandbox", "{access_args}",
  "{engine_args}",
  "-p", "{spec}",
]
```

Behaviour:

- `cd`s into `{taskdir}` before invoking agy (harmless on its own;
  agy ignores cwd for filesystem writes).
- After agy exits, mirrors any file in
  `~/.gemini/antigravity-cli/scratch/` whose mtime is newer than an
  invocation-start marker into `{taskdir}`, preserving subpaths
  (`scratch/scripts/foo.py` → `{taskdir}/scripts/foo.py`).
- Default policy is **no-clobber**: a file already in `{taskdir}` is
  never overwritten. Force-overwrite with `AGY_RINGER_FORCE_BACK_COPY=1`.
- Skip the mirror entirely for read-only review tasks with
  `AGY_RINGER_NO_BACK_COPY=1`.
- Override the scratch root (e.g. on a non-standard install) with
  `AGY_RINGER_SCRATCH_DIR=/path/to/scratch`.
- Emits a one-line summary on stderr:
  `agy-ringer: copied=N skipped=K missing=M from <scratch>`.
- Propagates agy's exit code; per-attempt Ringer retry is unchanged.

**Concurrent-run warning.** The scratch dir is global per user.
Two parallel agy tasks will both mirror their new files into their
own taskdirs; if both write the same basename, the no-clobber policy
keeps whichever the first invocation landed first. If you need
strict isolation, run agy tasks with `--max-parallel 1` until
upstream ships `--cwd`. The wrapper is regression-tested under
`tests/test_agy_ringer.py`.

## Permission prompts

For tasks that must create or modify files, prefer:

```json
"engine_args": ["--mode", "accept-edits"]
```

Avoid `plan` mode for write tasks — it only emits plans.

## Sandbox

**Sandbox is on by default and there is no off switch from the worker side.** The `args_template` for `[engines.agy]` always includes the literal `--sandbox` flag; `sandbox_args = []` because agy's own flag is what we want — no extra Ringer-side args needed. Every default task therefore runs sandboxed.

`full_access_args = ["--dangerously-skip-permissions"]` is wired through `{access_args}` and only fires when both:

- the task sets `"full_access": true`, AND
- the config has `allow_full_access = true` (the belt-and-suspenders gate).

Without that dual opt-in, the bypass is impossible regardless of what the worker prompt says. This matches the same model the codex block uses — sandbox is the floor, full-access is an explicit per-task opt-in you must request from a human.

## Runtime isolation

`RINGER_HOME` is not enough. Orchestrators that launch `agy` or `ringer.py`
from an outer Codex sandbox hit unwritable `HOME`/`XDG` and a blocked
loopback socket. The recovery is **not** `allow_full_access = true`.

Run AGY-backed Ringer tasks only through `bin/ringer-safe-run`.

Never launch `agy` or `ringer.py` directly from the outer Codex sandbox.
Never change `allow_full_access` to true to recover from HOME, XDG,
socket, dashboard, artifact, or state-path failures.

If the safe runner reports `NETWORK_SANDBOX`, `HOME_ISOLATION_FAILURE`,
`RUNTIME_PATH_ESCAPE`, `MANIFEST_POLICY_FAILURE`, or `PREFLIGHT_FAILURE`,
stop and report the infrastructure failure. Do not retry by weakening
permissions.

See `docs/runtime-isolation.md` for precedence, path containment, engine
env templates, and the safe-run contract.

If you want a sandbox-stricter mode than agy's default `--sandbox` provides, check `agy --help` for any `--sandbox=<profile>` form on your installed version and override the literal flag in `args_template`.

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

**`agy` 1.1.13 does not expose per-call token counts** on stdout, stderr,
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

- The shipped Ringer config includes `--mode accept-edits` by default. Override
  it with `engine_args` only when a task intentionally needs another mode.
- `--project` behavior varies by installed version; wrapper fallback exists.
- `--sandbox` may not be accepted on all versions — verify locally first.
- Full-access bypass flag is `agy`-version-specific; do not claim support
  without verifying against your installed CLI.
