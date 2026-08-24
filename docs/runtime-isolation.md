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
`RUNTIME_PATH_ESCAPE`, `MANIFEST_POLICY_FAILURE`, `PREFLIGHT_FAILURE`, or
`CLEANUP_FAILURE`, stop and report the infrastructure failure. Do not retry
by weakening permissions.

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

When `RINGER_SAFE_AGY_COPY_PATHS` is unset, Ringer seeds the default
AGY oauth files plus OpenCode `auth.json` into each worker HOME from
`RINGER_SAFE_SEED_HOME` (fallback: the current process `HOME`). An
explicit empty value copies nothing. The wrapper exports
`RINGER_SAFE_SEED_HOME` to its isolated host-home after the copy.
Sources must be relative, must not contain `..`, must not be
symlinks, and the destination must stay inside the worker HOME. The
whole `~/.gemini` tree and `opencode.db` are never copied.

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
When its caller already has an intentionally isolated `HOME`, set the boolean
`RINGER_SAFE_USE_ACCOUNT_HOME=1`. The wrapper then resolves the current login
account through `/usr/bin/getent`, rejects a symlinked, foreign-owned, or
group/other-writable account home, and seeds only its fixed credential
allowlist. The caller cannot supply an alternate path; any value other than
unset or `1` fails closed. In this mode the wrapper also pins `/usr/bin/python3`,
the checked-in `config.safe.toml`, the `agy`-only engine allowlist, maximum
parallelism `1`, the fixed credential paths, the Ringer source, and `/tmp` as
the runtime parent. It uses `/bin/bash`, starts with `PATH=/usr/bin:/bin`, then
adds only the validated account's `.local/bin` for the installed AGY command.
Only the three AGY OAuth files are eligible for seeding in this mode; OpenCode
auth is excluded because the engine allowlist is AGY-only. Caller values cannot
replace those executable surfaces.

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
5. copies only paths listed in `RINGER_SAFE_AGY_COPY_PATHS` (unset
   default: the three AGY oauth files plus OpenCode `auth.json` —
   never the whole `~/.gemini` tree or `opencode.db`) into the
   host-home, then exports `RINGER_SAFE_SEED_HOME` so each worker HOME
   is seeded from that host-home rather than the real home; an EXIT
   trap later scrubs those copies;
6. runs `ringer.py preflight` then `ringer.py run --no-dashboard`
   `--no-self-update` with `config.safe.toml`;
7. never sets `allow_full_access` and never adds
   `--dangerously-skip-permissions`;
8. keeps the runtime on failure;
9. returns Ringer's exit status.

The wrapper runs **outside** the outer Codex sandbox so AGY can bind a
local loopback socket. AGY itself still runs with `--sandbox` and
`--add-dir {workdir}` plus `--add-dir {taskdir}`. The two sandboxes are not the same layer: the
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

Do not copy `~/.gemini` wholesale. Isolated `HOME` is what made a working
terminal AGY session look unauthenticated: AGY reads
`~/.gemini/antigravity-cli/antigravity-oauth-token` (not
`~/.gemini/antigravity-oauth-token`). Keyring/D-Bus alone is not enough
on this host.

Isolated OpenCode workers also receive `.local/share/opencode/auth.json`
from the seed home. Never copy `opencode.db` or the rest of
`~/.local/share/opencode`. Do not recover a missing MiniMax session by
pointing OpenCode at the operator HOME.

When an isolated codex-pr-train coordinator sets the fixed boolean
`RINGER_SAFE_USE_ACCOUNT_HOME=1`, `engines/opencode-sandboxed.sh` also ignores
the coordinator's `PATH`. On Linux it resolves the current UID through fixed
`/usr/bin` tools, validates the canonical login home, and runs only the
owner-controlled executable reached from `~/.local/bin/opencode`. The launcher
must resolve inside that home through owner-controlled, non-group/world-
writable directories to an owner-controlled executable. The caller cannot
provide an executable or home path. A missing or unsafe installation fails
closed before the provider probe.

When `RINGER_SAFE_AGY_COPY_PATHS` is unset, the wrapper seeds only:

- `.gemini/antigravity-cli/antigravity-oauth-token`
- `.gemini/oauth_creds.json`
- `.gemini/google_accounts.json`
- `.local/share/opencode/auth.json`

Set the variable to empty to copy nothing, or list extra relative files
under the real home. The wrapper copies those files into the isolated
host-home and Ringer seeds the same relative paths into each worker HOME
from `RINGER_SAFE_SEED_HOME`. Isolated workers inherit the process
`XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS` so a session bus still
works; they do not remap `XDG_RUNTIME_DIR` under engine-home. Failed-run
trees are diagnostics; do not store long-lived credentials there.

Linux `engines/opencode-sandboxed.sh` mounts `--tmpfs /tmp` on top of
the host filesystem. A manifest `repo` under `/tmp` then vanishes
inside bubblewrap. Ringer exports `RINGER_OPENCODE_EXTRA_BINDS` (the
repo and any workdir that is not the taskdir or an ancestor of it;
never `/`, `/home`, `/tmp`). The wrapper bind-mounts those paths after
the tmpfs so they reappear. Binding the workdir ancestor would make
sibling task dirs writable; the taskdir is already bound. macOS
Seatbelt does not hide `/tmp`; extra binds are Linux only. This is
not a reason to point OpenCode `HOME` at the operator home.

## Failed-run artifacts

An EXIT/INT/TERM trap scrubs seeded authentication files from
`host-home`, `engine-homes`, and any other copied-auth destination on
every exit, including success, validator/preflight/worker failure,
SIGINT, and SIGTERM. `--keep-runtime` may retain the diagnostic tree;
it never retains authentication material. `RINGER_SAFE_CLEAN_SUCCESS` is
not required for credential-safe cleanup and is ignored.

Cleanup refuses empty, non-canonical, symlink, or out-of-parent runtime
paths and never follows symlinks. A cleanup failure prints
`CLEANUP_FAILURE` and the retained runtime path, never credential
contents, and exits non-zero on an otherwise successful run.

On failure the wrapper prints the runtime path and leaves diagnostics
in place after scrubbing auth. Inspect:

- `<runtime>/runs/*.json`
- `<runtime>/runs.jsonl`
- `<runtime>/artifacts/`
- the task workdir (`worker.log`)

On success the wrapper copies artifacts to `RINGER_SAFE_REPORT_DIR`
when that is set, then drops ephemeral `tmp` / `engine-homes` /
`work` / `host-home`. State, logs, artifacts, and reports remain.

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
| `RINGER_SAFE_AGY_COPY_PATHS` | Relative files copied from the real home into host-home and each worker HOME | oauth token, `oauth_creds.json`, `google_accounts.json`, OpenCode `auth.json` (unset = that list; empty = nothing) |
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
