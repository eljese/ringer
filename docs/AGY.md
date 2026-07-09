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
If your installed `agy` ignores `--project`, swap `bin` for the optional wrapper
`engines/agy-ringer.sh` (see source comments) which `cd`s into the task dir.

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

After enabling the block:

```bash
./ringer.py lint templates/agy-smoke.json
./ringer.py run templates/agy-smoke.json
```

The smoke manifest creates `agy-smoke.txt` containing exactly
`agy works with ringer`. The check exits 0 only when the file matches verbatim.

## Token accounting

`agy` 1.1.0 token output format is not yet verified, so `token_regex` stays
empty in the sample config. Once you have a real `worker.log` from a smoke run
and can confirm a stable pattern, add the regex.

## Known caveats

- `agy -p` can hang if permission prompts are unsatisfied; use `--mode accept-edits`.
- `--project` behavior varies by installed version; wrapper fallback exists.
- `--sandbox` may not be accepted on all versions — verify locally first.
- Full-access bypass flag is `agy`-version-specific; do not claim support
  without verifying against your installed CLI.