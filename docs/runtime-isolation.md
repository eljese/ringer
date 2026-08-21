# Runtime isolation

Ringer's default state still lives under `~/.ringer` so existing installs
keep working. That default is not enough for a sandboxed orchestrator that
must run AGY.

`RINGER_HOME` only moved the active-run registry, catalog snapshot, and
model DB. Config `state_dir`, eval JSONL, artifacts, HUD logs, and
self-update state could still expand to the real home. Workers inherited
the parent `HOME`/`XDG`/`TMPDIR`, so AGY tried to write
`~/.gemini/...` and failed under a Codex sandbox. The tempting "fix" was
`allow_full_access = true` or `--dangerously-skip-permissions`. That is
not a recovery path.

## Permanent orchestrator rule

Run AGY-backed Ringer tasks only through `bin/ringer-safe-run`.

Never launch `agy` or `ringer.py` directly from the outer Codex sandbox.
Never change `allow_full_access` to true to recover from HOME, XDG,
socket, dashboard, artifact, or state-path failures.

If the safe runner reports `NETWORK_SANDBOX`, `HOME_ISOLATION_FAILURE`,
`RUNTIME_PATH_ESCAPE`, `MANIFEST_POLICY_FAILURE`, or `PREFLIGHT_FAILURE`,
stop and report the infrastructure failure. Do not retry by weakening
permissions.

## Precedence

1. `./ringer.py --runtime-root /path`
2. `RINGER_RUNTIME_ROOT=/path`
3. Existing configuration and legacy defaults (`state_dir`, `RINGER_HOME`,
   `~/.ringer`)

When a runtime root is explicit it is authoritative. Built-in outputs that
would escape it are rewritten under the root. Operator inputs are not
moved: the config file, postgres `env_file`, steering profiles, engine
binaries, the manifest, the source repo, and the task workdir stay where
the operator pointed them.

## Paths guaranteed under the runtime root

When isolation is active:

```
<runtime-root>/
  active-runs.json
  ringer.db
  openrouter-catalog.json
  runs/
  runs.jsonl
  artifacts/
  logs/
  tmp/
  engine-homes/
  work/
  hud.log
  self-update.json
  steering/observations/   # only when the configured steering dir escapes
```

Policy: deterministic override of built-in runtime outputs. A sample
config that still says `state_dir = "~/.ringer"` does not leak state once
`--runtime-root` or `RINGER_RUNTIME_ROOT` is set.

The requested root is created and write-probed. If it cannot be created
or written, Ringer fails closed. The root must not sit inside the Ringer
source checkout.

## Engine environment

`[engines.<name>.env]` is an optional string-to-string table. Placeholders
are expanded with `str.replace` only:

- `{runtime_root}`
- `{run_id}`
- `{task_key}`
- `{taskdir}`

Shell interpolation (`$`, `` ` ``, `$(...)`) is rejected. Variable names
must be valid identifiers.

When isolation is active, every worker gets a per-task overlay even if
the engine has no `env` table:

```
HOME              = <runtime-root>/engine-homes/<engine>/<run_id>/<task_key>
XDG_CONFIG_HOME   = $HOME/.config
XDG_CACHE_HOME    = $HOME/.cache
XDG_STATE_HOME    = $HOME/.local/state
XDG_DATA_HOME     = $HOME/.local/share
TMPDIR            = <runtime-root>/tmp/<run_id>/<task_key>
AGY_RINGER_SCRATCH_DIR = $HOME/.gemini/antigravity-cli/scratch
```

An explicit `env` table may refine those paths. HOME/XDG/TMP templates
that escape the runtime root are rejected. The process environment is
copied first (`PATH` and auth/keyring variables stay available) and then
overlaid. Isolated workers drop process-injection variables
(`LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`, `BASH_ENV`, `ENV`,
`PYTHONSTARTUP`, `PERL5OPT`, `RUBYOPT`). `PYTHONPATH` is kept so local
Python workers still import. Secret-valued variables are never printed.

When `RINGER_SAFE_AGY_COPY_PATHS` is set, those relative files are
copied into each worker HOME from `RINGER_SAFE_SEED_HOME` (fallback:
the current process `HOME`). The wrapper exports `RINGER_SAFE_SEED_HOME`
to its isolated host-home after the copy. Sources must be relative,
must not contain `..`, must not be symlinks, and the destination must
stay inside the worker HOME. The whole `~/.gemini` tree is never copied.

With no runtime root and no `env` table, workers inherit the parent
environment exactly as before.

This is write/state containment, not a confidentiality sandbox. A
worker can still read whatever its engine sandbox allows. Do not treat
`--runtime-root` as a secret store.

## `ringer.py --runtime-root` is not the wrapper

`--runtime-root` / `RINGER_RUNTIME_ROOT` relocates built-in outputs and
overlays worker HOME/XDG/TMP. It does **not** run the safe-manifest
validator unless `RINGER_SAFE_ENFORCE=1`. Process `HOME` may remain the
real home. Engine allowlisting, workdir policy, and AGY copy seeding
are wrapper duties (`bin/ringer-safe-run`). Isolated mock tests may use
custom engines such as `probe` / `marker` / `mock` under `--runtime-root`
without the wrapper allowlist.

When the CLI sets a runtime root, Ringer also exports process
`TMPDIR=<runtime-root>/tmp` after creating the layout.

Native Ringside.app (`hud/src`) is a parked prototype and does not honor
`RINGER_RUNTIME_ROOT`. The Python HUD does.

## Safe runner

`bin/ringer-safe-run` is the Linux host wrapper for sandboxed AGY review.

```bash
bin/ringer-safe-run --manifest /path/to/manifest.json --identity grok-build
```

Pass an alternate config only via `RINGER_SAFE_CONFIG`. The wrapper
rejects `--config` so a prefix-allowed invocation cannot swap
`engines.agy.bin` from the command line. Isolated AGY argv0 must remain
`agy`. Safe-run checks cannot traverse `..`, redirect, or invoke a
shell/interpreter. Deliverable harvest refuses symlinks that escape the
task directory.

It:

1. permits manifests only from `RINGER_SAFE_MANIFEST_ROOTS` (default:
   `<checkout>/templates` and `<checkout>/manifests`);
2. runs `tools/validate_safe_manifest.py`;
3. creates a `0700` runtime with `mktemp` outside the checkout;
4. exports `RINGER_RUNTIME_ROOT`, `RINGER_SAFE_ENFORCE=1`, and an isolated
   host `HOME`/`XDG`/`TMPDIR`;
5. copies only paths listed in `RINGER_SAFE_AGY_COPY_PATHS` (default:
   nothing — never the whole `~/.gemini` tree) into the host-home, then
   exports `RINGER_SAFE_SEED_HOME` so each worker HOME is seeded from
   that host-home rather than the real home;
6. runs `ringer.py preflight` then `ringer.py run --no-dashboard`
   `--no-self-update` with `config.safe.toml`;
7. never sets `allow_full_access` and never adds
   `--dangerously-skip-permissions`;
8. keeps the runtime on failure;
9. returns Ringer's exit status.

The wrapper runs **outside** the outer Codex sandbox so AGY can bind a
local loopback socket. AGY itself still runs with `--sandbox` and
`--add-dir {taskdir}`. The two sandboxes are not the same layer: the
outer Codex sandbox blocks loopback and the real home; AGY's sandbox is
the worker filesystem floor.

Safe-run manifests may use structured `{"argv": [...]}` checks or a
conservative shell check that starts with `test`, `[`, or `grep` and
contains no shell metacharacters (`;|&\`$()` , `&&`, `||`, newlines).
No-op checks, extra `--add-dir` (any case or `--add-dir=` form),
absolute `expect_files` outside an approved root, and workdirs under
sensitive home directories are rejected. When `RINGER_SAFE_PROJECT_ROOTS`
or `RINGER_SAFE_RUNTIME_ROOTS` is set, workdir must sit under those
roots; there is no implicit `/tmp` workdir allowlist. This is a policy
gate for the host wrapper, not a hostile-worker jail.

## Authentication

Do not copy `~/.gemini` into the isolated home. Prefer credentials that
already exist in the process environment (provider tokens, keyring
sockets). If a file must be seeded, list a relative path under the real
home in `RINGER_SAFE_AGY_COPY_PATHS`. The wrapper copies those files
into the isolated host-home and Ringer seeds the same relative paths
into each worker HOME from `RINGER_SAFE_SEED_HOME`. Failed-run trees are
diagnostics; do not store long-lived credentials there.

## Failed-run artifacts

On failure the wrapper prints the runtime path and leaves the tree in
place. Inspect:

- `<runtime>/runs/*.json`
- `<runtime>/runs.jsonl`
- `<runtime>/artifacts/`
- the task workdir (`worker.log`)

On success the runtime is retained by default. Set
`RINGER_SAFE_CLEAN_SUCCESS=1` to drop ephemeral `tmp` / `engine-homes` /
`work` / `host-home` after copying artifacts to `RINGER_SAFE_REPORT_DIR`
when that is set.

## Allowed roots

| Variable | Purpose | Default |
|---|---|---|
| `RINGER_SAFE_MANIFEST_ROOTS` | Manifest allowlist | `<checkout>/templates:<checkout>/manifests` |
| `RINGER_SAFE_PROJECT_ROOTS` | Manifest `repo` and real-home workdir allowlist | empty (Ringer checkout and the real home are rejected) |
| `RINGER_SAFE_RUNTIME_ROOTS` | Absolute output allowlist | empty |
| `RINGER_SAFE_ARTIFACT_ROOTS` | Absolute `expect_files` allowlist | same as runtime roots |
| `RINGER_SAFE_ALLOWED_ENGINES` | Engine allowlist | `agy:mock` |
| `RINGER_SAFE_MAX_PARALLEL` | Parallelism cap | `4` |
| `RINGER_SAFE_RUNTIME_PARENT` | `mktemp` parent | `$TMPDIR` or `/tmp` |
| `RINGER_SAFE_CONFIG` | Config passed to Ringer | `<checkout>/config.safe.toml` |
| `RINGER_SAFE_AGY_COPY_PATHS` | Relative files copied from the real home into host-home and each worker HOME | empty |
| `RINGER_SAFE_SEED_HOME` | Source tree for worker HOME seeding | wrapper host-home |
| `RINGER_SAFE_ENFORCE` | Re-validate the manifest and apply the engine allowlist during preflight | unset (`1` in the wrapper) |

## Expected invocation

```bash
export RINGER_SAFE_MANIFEST_ROOTS=/path/to/orchestrator/manifests
export RINGER_SAFE_PROJECT_ROOTS=/path/to/target/repo
bin/ringer-safe-run --manifest /path/to/orchestrator/manifests/review.json --identity grok-build
```

## Codex prefix_rule

Allow only the installed wrapper executable. Adapt the path and current
Codex rule syntax; this is the shape, not a file to paste into this
repository:

```
prefix_rule(
    pattern = [
        "/absolute/path/to/ringer/bin/ringer-safe-run",
    ],
    decision = "allow",
    justification = "Runs validated manifests through the isolated Ringer host boundary.",
)
```

Do not allow generic prefixes: `bash`, `sh`, `python`, `python3`,
`ringer.py`, `agy`, or the entire Ringer directory. Those let the outer
sandbox launch AGY without the validator, isolated host-home, or engine
allowlist. Never edit `~/.codex` rules from Ringer itself.
