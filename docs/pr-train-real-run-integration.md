# PR-train real-run integration boundary

Use `tools/ringer_supervisor_integrated.py` for codex-pr-train implementation runs.
It delegates the worker lifecycle to `ringer_supervisor_hardened.py` and adds the
integration invariants that are easy to violate when the controller, nested
Codex process, OpenCode and Ringer use different runtime assumptions.

## Contract

- The source checkout, worktrees, artifacts and runtime root must resolve to
  separate locations. Runtime roots inside the source checkout fail closed.
- The wrapper creates one canonical isolated `HOME` and XDG tree. If OpenCode
  credentials are available, they are copied only to
  `$XDG_DATA_HOME/opencode/auth.json`, which is the location used by both the
  exact-model inference probe and the implementation worker.
- `expect_files` entries are normalized to `{{TASK_DIR}}/...`. Absolute paths
  are accepted only when they refer to the logical task directory; this avoids
  stale unsuffixed paths when Ringer creates `--attempt-001` worktrees.
- Harness-owned files such as `attempt.json`, supervisor outcomes, heartbeats,
  worker logs and `.ringer`/`.codex-pr-train` directories are rejected before
  a candidate patch is sealed.
- `supervisor-progress.json` is structured telemetry only. It can show
  preflight, provider, objective-check and terminal phases, but PASS is
  established solely by the canonical `supervisor-outcome.json`.

## Credential seeding

The wrapper finds an OpenCode auth source in this order:

1. `supervisor.credential_seed.source` in the manifest;
2. `OPENCODE_AUTH_SOURCE`;
3. the caller's `$XDG_DATA_HOME/opencode/auth.json`;
4. the caller's `~/.config/opencode/auth.json`.

Set `supervisor.credential_seed.required=true` when a missing source must stop
before provider invocation. A configured destination must exactly match the
canonical isolated XDG data path. Seeded credentials are removed when the run
returns; non-sensitive manifests, progress and outcome evidence remain.

## Invocation

```bash
python3 /path/to/ringer/tools/ringer_supervisor_integrated.py run \
  /external/runtime/manifest.json \
  --artifact-dir /external/runtime/artifacts
```

The compatibility and original hardened entrypoints remain available for their
existing callers. New codex-pr-train configurations should invoke only the
integrated entrypoint.
