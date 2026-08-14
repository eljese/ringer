# Deterministic Ringer supervisor

`tools/ringer_supervisor.py` moves process control out of the parent model. The
parent creates one lifecycle manifest, invokes one blocking command, and reads a
canonical outcome after it exits. The supervisor owns:

- Git ancestry, writability, free-byte and free-inode preflight;
- optional provider health probes;
- one-attempt-per-provider routing;
- MiniMax → Grok Build → AGY implementer fallback;
- timeouts, progress heartbeats and no-progress termination;
- expected-artifact validation;
- durable patch sealing through `ringer_lifecycle.py`;
- JSONL state-transition events and one canonical terminal outcome.

## Invocation

```bash
python3 /opt/ringer/tools/ringer_supervisor.py run \
  /path/to/ringer-manifest.json \
  --ringer /opt/ringer/ringer.py \
  --config ~/.config/ringer/config.toml \
  --artifact-dir /path/to/durable/artifacts
```

The process exits `0` only when every task passes and required artifacts are
present. It writes:

- `supervisor-preflight.json`
- `supervisor-events.jsonl`
- `supervisor-outcome.json`
- attempt manifests and logs under `<artifact-dir>/<task>/attempt-NNN/`
- `worktree.patch` and its SHA-256 when a repository-backed task succeeds

## Manifest supervisor policy

```json
{
  "run_name": "pr-02-repair",
  "workdir": "/srv/runs/pr-02",
  "repo": "/srv/repos/project",
  "supervisor": {
    "base_ref": "origin/main",
    "minimum_free_bytes": 5368709120,
    "minimum_free_inodes": 100000,
    "heartbeat_seconds": 60,
    "no_progress_seconds": 600,
    "fallback_on": [
      "PROVIDER_QUOTA",
      "PROVIDER_TIMEOUT",
      "NETWORK_SANDBOX",
      "CHECK_FAILURE",
      "MISSING_EXPORT",
      "NO_PROGRESS",
      "MALFORMED_OUTCOME"
    ],
    "routes": [
      {
        "engine": "opencode",
        "model": "minimax-coding-plan/MiniMax-M3",
        "timeout_seconds": 900
      },
      {
        "engine": "grok",
        "model": "grok-4.6",
        "timeout_seconds": 900
      },
      {
        "engine": "agy",
        "model": "gemini-3.7-flash-high",
        "timeout_seconds": 600
      }
    ],
    "provider_probes": {
      "grok": {
        "argv": ["grok", "--version"],
        "timeout_seconds": 60
      }
    }
  },
  "tasks": [
    {
      "key": "implementation-repair",
      "spec": "Fix the bounded failure and preserve unrelated work.",
      "check": {"argv": ["python", "-m", "pytest", "tests/test_target.py"]},
      "expect_files": ["{{TASK_DIR}}/notes.md"]
    }
  ]
}
```

A provider with a configured failed health probe is skipped. An unprobed route
remains eligible. Final independent review is intentionally outside this
implementation route: use a fresh AGY review identity bound to the exact
candidate tree.

## Parent-agent contract

The parent agent should not tail logs or poll workers. It should:

1. write the manifest;
2. run the supervisor command once;
3. read `supervisor-outcome.json`;
4. use `supervisor-events.jsonl` only for diagnostics or telemetry;
5. invoke a separate independent review after implementation passes.
