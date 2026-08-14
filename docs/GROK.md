# Using `grok` (Grok Build CLI) with Ringer

`grok` is xAI's Grok Build CLI. Ringer ships a commented `[engines.grok]`
block in `config.sample.toml` that runs `grok` as a Ringer worker. Verified
against grok 1.0.3 on 2026-08-14 (Linux, logged in). No wrapper is required.

## Install + authenticate

1. Install `grok` (pick one):
   ```bash
   curl -fsSL https://x.ai/cli/install.sh | bash
   # or: npm install -g @xai-official/grok
   ```
2. Sign in — OAuth on a SuperGrok or X Premium Plus plan:
   ```bash
   grok login
   ```
3. Verify what models your account can reach:
   ```bash
   grok models
   ```

Live 1.0.3 ids are `grok-4.6` (default) and `grok-4.5`. Legacy slugs
`grok-build` and `grok-composer-2.5-fast` are rejected by 1.0.3 as unknown
model ids.

## Enable the engine

Copy `config.sample.toml` to `~/.config/ringer/config.toml` and uncomment the
`[engines.grok]` block. The shipped default `grok-4.6` is verified against
grok 1.0.3 on 2026-08-14 (Linux). Override per task via `"model": "grok-4.5"`
in the manifest.

## Headless mode

Ringer closes stdin for every worker. `grok -p` is the mode that exits
cleanly without a TTY. Verified 2026-08-14 against grok 1.0.3:

```bash
grok --no-auto-update -p "Reply with exactly: grok-headless-ok" < /dev/null
# exits 0, stdout is grok-headless-ok
```

`--cwd {taskdir}` sets the process working directory to the task dir so
relative writes land there. Combined with `--sandbox workspace` and
`--always-approve`, the 2026-08-14 write probe created the file in
`{taskdir}`. Workspace still allows writes to temp and `~/.grok`.

```bash
grok --cwd "$td" --sandbox workspace --always-approve -m grok-4.6 \
  -p 'Create grok-write-probe.txt with exactly grok-write-ok'
# writes $td/grok-write-probe.txt with exact content
```

`--no-auto-update` is documented in 1.0.3 (no longer a hidden flag). It
stops the CLI self-updating mid-swarm.

## Sandbox

Grok brings its own OS sandbox (Seatbelt on macOS, equivalent on Linux).
Sandbox profiles:

- `workspace` (default floor): read everywhere, write CWD + temp + `~/.grok`,
  network allowed. This is `sandbox_args`.
- `off` for full_access: `full_access_args = ["--sandbox", "off"]`. This
  only takes effect when a task explicitly sets `"full_access": true` AND
  the config has `allow_full_access = true`.

Without that dual opt-in, the bypass is impossible regardless of what the
worker prompt says. This matches the same belt-and-suspenders model the
codex block uses.

## Smoke test

After enabling the block, use the launcher that wipes the workdir before
each invocation (otherwise a previous successful run can leave a stale
file that masks a new failure):

```bash
./engines/run-grok-smoke.sh
```

The wrapper deletes `/tmp/ringer-grok-smoke` and then runs the manifest.
The manifest creates `grok-smoke.txt` containing `grok works with ringer`.
The check exits 0 when that text matches after stripping trailing
newlines (a first-try write that adds `\\n` still passes).

## Token accounting

`grok` 1.0.3 `--output-format json` emits `usage.total_tokens`. Verified
2026-08-14: a headless JSON run reported `"total_tokens": 22757`. The
sample `token_regex` captures that integer.

The same JSON object reports a `modelUsage` key of `grok-4.6-build` when
the invoked slug is `-m grok-4.6`. That key is **not** a live
`grok models` id, so the sample block does **not** set
`model_report_regex` — Ringer stamps the manifest/config slug
`grok-4.6`. `grok-4.6-build` is registered as an alias so any historical
or future row that used the JSON key still displays as Grok 4.6.

## Known caveats

- No wrapper. `--cwd` plus `--sandbox workspace` is sufficient for
  file-creation tasks (verified 2026-08-14).
- Legacy slugs `grok-build` and `grok-composer-2.5-fast` are rejected by
  1.0.3. Route with `grok-4.6` or `grok-4.5`. Historical scoresheet rows
  that used those slugs still resolve in the identity registry.
- `--no-auto-update` is documented in 1.0.3; keep it in `args_template`
  so a swarm cannot self-update mid-run.
- Full-access is `--sandbox off` and still gated by `allow_full_access`.
