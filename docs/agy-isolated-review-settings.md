# Isolated AGY review settings

`engines/agy_safe_review.py` installs a Ringer-owned AGY settings profile into the isolated per-task worker `HOME` and then replaces itself with the real `agy` executable.

The launcher is intended only for `bin/ringer-safe-run` review tasks. It requires `RINGER_SAFE_ENFORCE=1`, requires `HOME` below `RINGER_RUNTIME_ROOT/engine-homes/agy/`, refuses symlinks, refuses to overwrite an existing settings file, writes the profile with mode `0600`, and never copies the operator's normal AGY settings.

The profile allows only AGY file-review capabilities:

- `read_file`
- `grep_search`
- `list_dir` / `list_directory`
- `write_file`

It explicitly denies command, shell aliases, MCP, web search, and URL-reading capabilities. It contains no `toolPermission`, `artifactReviewPolicy`, `trustedWorkspaces`, full-access flag, or command wildcard in the allow list.

## Safe-run configuration

Create a disposable copy of `config.safe.toml` and change only the AGY command to invoke the launcher through Python. Use the absolute launcher path from the exact Ringer checkout being reviewed:

```toml
[engines.agy]
bin = "python3"
args_template = [
  "/absolute/path/to/ringer/engines/agy_safe_review.py",
  "--add-dir",
  "{workdir}",
  "--add-dir",
  "{taskdir}",
  "--model",
  "{model}",
  "--mode",
  "accept-edits",
  "--sandbox",
  "{access_args}",
  "{engine_args}",
  "-p",
  "{spec}",
]
sandbox_args = []
full_access_args = []
```

Keep the existing `[engines.agy.env]` block unchanged, then pass the disposable configuration through `RINGER_SAFE_CONFIG`. Never add `--dangerously-skip-permissions` or enable `allow_full_access`.

## Security boundary

The profile enables non-interactive review; it is not the hard filesystem boundary. AGY 1.1.13 has an upstream fine-grained-permission defect where path-specific file denies and precedence can be bypassed. Ringer therefore continues to rely on its isolated runtime, isolated worker `HOME`, controller-prepared review bundle, `--add-dir` workspace anchors, process cleanup, and before/after repository fingerprints. Do not use AGY review for untrusted repositories or secrets until upstream file-tool confinement is fixed or an OS-level filesystem sandbox is added.
