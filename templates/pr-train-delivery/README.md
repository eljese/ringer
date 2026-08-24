# PR-train delivery kit

A single-PR implementation profile for `codex-pr-train`. It combines the existing repo-feature discipline with the authoritative hardened PR-train supervisor boundary.

## Ownership model

- The PR-train controller owns worktree creation, retries, staging, commits, pushes, pull-request creation, required-check validation, and merge.
- The delivery coordinator fills this manifest from the controller decision packet and invokes `tools/ringer_supervisor_pr_train.py` exactly once.
- Ringer owns the isolated implementation attempt and executable checks.
- The implementation worker leaves repository changes uncommitted.
- AGY performs candidate-bound independent review after a sealed patch exists.

`profile.json` is the machine-readable identity. Consumers should bind `name=pr-train-delivery` and `version=1`, hash the completed manifest and canonical supervisor outcome, and persist those values with the PR evidence.

## Required substitutions

Replace every placeholder in `manifest.json`. In particular, the coordinator must copy these values verbatim from the decision packet rather than reconstructing them:

- exact provider-probe object;
- exact worker-capability-probe object;
- exact non-empty allowed changed paths;
- approved implementation engine/model and timeout;
- source repository, base SHA, PR ID, attempt, and external runtime paths.

Object placeholders are strings in the starter so the checked-in template remains valid JSON. Replace the complete JSON string value with the corresponding object before execution. No `{{...}}` placeholder may remain.

Use a stable run identity across attempts:

```text
<repository>:<train-id>:<PR-ID>
```

## Validation

Run the static kit check before use:

```bash
python3 templates/pr-train-delivery/checks/validate_profile.py \
  --profile templates/pr-train-delivery/profile.json \
  --manifest templates/pr-train-delivery/manifest.json
```

After filling the manifest, also run normal Ringer lint and the PR-train safe-manifest validator. Relevant lint or policy findings are blocking for this profile, even when standalone Ringer would treat a lint finding as advisory.

Execute only through:

```bash
python3 tools/ringer_supervisor_pr_train.py run /external/runtime/manifest.json \
  --artifact-dir /external/runtime/artifacts
```

The caller consumes only the canonical `supervisor-outcome.json`. Progress files, worker notes, completion markers, and model summaries never establish PASS.
