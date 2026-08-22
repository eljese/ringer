# Hardened PR-train supervisor

Use `tools/ringer_supervisor_hardened.py` for PR-train implementation runs.
The legacy `tools/ringer_supervisor.py` remains available for compatibility.

The hardened entrypoint adds five fail-closed invariants:

1. Implementation routes are restricted to the configured implementation role.
   The default permits only OpenCode models whose model name contains `minimax`.
2. Every provider attempt receives a new detached worktree at the same source
   `HEAD`. A failed or timed-out provider's working tree is never reused by a
   later provider.
3. Every repository task declares non-empty `objective_checks`. These checks
   use structured `argv`, run directly under the supervisor and cannot derive
   PASS from worker-authored report files such as `notes.md`.
4. The supervisor writes immutable attempt provenance containing the actual
   engine, model, baseline SHA, worktree, objective results and sealed patch.
5. Once `RUN_STARTED` exists, ordinary exceptions and SIGINT/SIGTERM produce an
   atomic `supervisor-outcome.json` and exactly one terminal event. Worker
   process groups are terminated before the supervisor exits.

## Manifest additions

```json
{
  "workdir": "/tmp/pr-train-run",
  "repo": "/home/user/project",
  "supervisor": {
    "routes": [
      {
        "engine": "opencode",
        "model": "minimax-coding-plan/MiniMax-M3",
        "timeout_seconds": 900
      }
    ],
    "allowed_implementation_engines": ["opencode"],
    "allowed_implementation_model_markers": ["minimax"],
    "require_inference_probes": true,
    "provider_probes": {
      "opencode": {
        "kind": "inference",
        "argv": [
          "opencode",
          "run",
          "--model",
          "minimax-coding-plan/MiniMax-M3",
          "Return exactly PROBE_OK"
        ]
      }
    }
  },
  "tasks": [
    {
      "key": "implementation",
      "engine": "opencode",
      "model": "minimax-coding-plan/MiniMax-M3",
      "spec": "Implement the scoped change.",
      "check": "true",
      "objective_checks": [
        {"argv": ["npm", "run", "test:unit"]},
        {"argv": ["npm", "run", "verify"]}
      ],
      "expect_files": []
    }
  ]
}
```

The `check` field remains the inner Ringer worker check. It is not sufficient
for a hardened PASS. All declared `objective_checks` must also pass.

Run once and wait for the foreground process:

```bash
python3 /home/eljese/ringer/tools/ringer_supervisor_hardened.py run \
  /absolute/path/to/manifest.json \
  --artifact-dir /absolute/path/to/artifacts
```

A caller must consume only `supervisor-outcome.json`. Heartbeats are telemetry,
not completion evidence. A live process plus a fresh heartbeat is `RUNNING`,
never `MALFORMED_OUTCOME`.
