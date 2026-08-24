# Isolated AGY review settings

`engines/agy` is the validator-compatible executable entrypoint for isolated AGY review runs. It imports `engines/agy_safe_review.py`, validates the isolated worker environment, locates the real `agy` executable outside the Ringer launcher directory, and then replaces itself with that provider.

For the exact preflight probe `agy --version`, the entrypoint forwards directly to the real provider without creating settings. This keeps version detection independent of the review profile. All normal review invocations install the Ringer-owned AGY settings profile into the isolated per-task worker `HOME` before provider execution.

The entrypoint is intended only for `bin/ringer-safe-run` review tasks. It requires `RINGER_SAFE_ENFORCE=1`, requires `HOME` below `RINGER_RUNTIME_ROOT/engine-homes/agy/`, refuses symlinks, refuses to overwrite an existing settings file, writes the checked-in profile byte-for-byte with mode `0600`, and never copies the operator's normal AGY settings.

The profile allows only AGY file-review capabilities:

- `read_file`
- `grep_search`
- `list_dir` / `list_directory`
- `write_file`

It explicitly denies command, shell aliases, MCP, web search, and URL-reading capabilities. It contains no `toolPermission`, `artifactReviewPolicy`, `trustedWorkspaces`, full-access flag, or command wildcard in the allow list.

## Safe-run configuration

Create a disposable copy of `config.safe.toml` and change only the AGY `bin` value to the absolute entrypoint path from the exact Ringer checkout being reviewed:

```toml
[engines.agy]
bin = "/absolute/path/to/ringer/engines/agy"
model_default = "gemini-3.7-flash-high"
args_template = [
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

Do not set `bin = "python3"` and do not prepend `agy_safe_review.py` to `args_template`. The safe-run command validator intentionally requires an AGY worker command whose executable basename is `agy`; the Ringer-owned `engines/agy` entrypoint preserves that invariant while installing the isolated profile for review invocations.

Keep the existing `[engines.agy.env]` block unchanged, then pass the disposable configuration through `RINGER_SAFE_CONFIG`. Never add `--dangerously-skip-permissions` or enable `allow_full_access`.

## Security boundary

The profile enables non-interactive review; it is not the hard filesystem boundary. AGY 1.1.13 has an upstream fine-grained-permission defect where path-specific file denies and precedence can be bypassed. Ringer therefore continues to rely on its isolated runtime, isolated worker `HOME`, controller-prepared review bundle, `--add-dir` workspace anchors, process cleanup, and before/after repository fingerprints. Do not use AGY review for untrusted repositories or secrets until upstream file-tool confinement is fixed or an OS-level filesystem sandbox is added.
