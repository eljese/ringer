---
name: ringer-recovery
description: Diagnose and recover failed, timed-out, stale, or ambiguous Ringer lifecycle runs without discarding work. Use after ERROR/TIMEOUT, provider or sandbox failures, stale worktrees, stale reports, missing exports, path/check mismatches, or when a retry would otherwise require manual cleanup.
---

# Ringer recovery

Use recovery as a deterministic evidence-preserving step, not as another implementation attempt.

## Required evidence

Before changing anything, inspect the latest run JSON, task worker log, declared `expect_files`, task worktree status, check result, configured engine/model, and any lifecycle artifact directory. Record the exact task key and attempt being recovered.

## Failure classification

Classify the incident into exactly one primary class before choosing an action:

- `WORKER_FINDING`: the worker/reviewer found a substantive code issue.
- `CHECK_FAILURE`: the executable verifier failed.
- `PROVIDER_QUOTA`: provider quota/rate/credit prevented execution.
- `PROVIDER_TIMEOUT`: the provider or worker exceeded its bounded timeout.
- `NETWORK_SANDBOX`: DNS, network, permission, or sandbox boundaries prevented execution.
- `STALE_WORKTREE`: a previous task-owned worktree blocks setup.
- `STALE_ARTIFACT`: report/evidence belongs to an earlier attempt or tree.
- `MISSING_EXPORT`: disposable work exists but no durable patch/artifact was sealed.
- `MANIFEST_PATH_ERROR`: the check and artifact/output paths disagree or escape their boundary.
- `SHELL_INTERPOLATION`: source text such as `${{ ... }}` was interpreted by a verification shell.
- `COORDINATOR_ERROR`: orchestration failed independently of worker correctness.

Infrastructure classes do not count as code-fix failures and must not consume the same retry budget as substantive findings.

## Recovery rules

For stale worktrees, first preserve any dirty state as a binary patch outside the worktree. Remove or prune only worktrees that are positively identified as lifecycle-owned. Never use `git reset --hard`, `git clean`, `git restore`, or stash as cleanup.

For stale artifacts, create a fresh attempt directory. Evidence is valid only when it is bound to the current attempt and current tree identity. Keep the older evidence for audit, but never reuse its verdict.

For missing exports, stop before deleting the worktree. Seal a durable patch, hash it, and verify the hash before cleanup. A successful worker with an unsealed disposable worktree is not a successful lifecycle outcome.

For path mismatches, prefer the canonical variables `{{RUN_DIR}}`, `{{TASK_DIR}}`, `{{ARTIFACT_DIR}}`, `{{SOURCE_REPO}}`, `{{BASE_SHA}}`, and `{{ATTEMPT}}`. Durable outputs belong outside a disposable worktree.

For shell interpolation, express checks as structured `{"argv": [...]}` when using `tools/ringer_lifecycle.py`; do not embed GitHub Actions expressions or other dollar-prefixed source text into an unquoted shell command.

For AGY/reviewer timeouts, preserve the exact tree and retry with a smaller review packet before changing reviewers. Use tier 1 exact diff first, tier 2 changed-file context only if needed, and tier 3 repository context only for genuinely cross-cutting findings.

## Lifecycle helper

Use the repository helper for safety-sensitive PR runs:

```bash
python3 tools/ringer_lifecycle.py run manifest.json --config ~/.config/ringer/config.toml --identity <identity>
```

Build a bounded independent review packet with:

```bash
python3 tools/ringer_lifecycle.py review-packet \
  --repo /path/to/repo --base <base-sha> --head <head-sha> \
  --tier 1 --out /tmp/review-packet.md
```

Clean only lifecycle-owned, clean stale worktrees with:

```bash
python3 tools/ringer_lifecycle.py gc /path/to/run-root --older-than-days 7 --dry-run
```

Run without `--dry-run` only after checking the proposed set.

## Completion contract

Recovery is complete only when the current attempt has an unambiguous failure class or PASS verdict, durable evidence outside disposable state, exact tree identity, and a clear next action. Do not convert ambiguous evidence into approval.
