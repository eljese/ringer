# Test recipes

## `ringer.py ask`

Status: tested

Purpose: verify context-packet selection, one-worker execution, opt-in request
redaction, Ringside state, artifact registration, and the one-attempt contract.

Safe actions:

- Run the unit suite; worker tests use temporary directories and local Python
  fixture workers.
- Run `ask --dry-run` against temporary text or Markdown sources.

Unsafe actions:

- Do not omit `--dry-run` from a smoke command unless a real model call is
  intended.

Verification steps:

1. Run `RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests`.
2. Create a temporary Markdown source containing a distinctive answer passage.
3. Run `RINGER_NO_SELF_UPDATE=1 python3 ./ringer.py ask "<question>" --source
   <temp-file> --dry-run`.
4. Confirm the packet report names the source passage and stdout says
   `No model call was made.`

Cleanup:

- Remove the temporary source and generated request directory when one was
  supplied explicitly.

Known test-environment constraint:

- Worker tests mock only the dashboard socket bind because restricted test
  sandboxes can reject local listeners. They assert that the run records a
  dashboard port and enters the artifact library.

## Runtime isolation

Status: tested

Purpose: verify `--runtime-root` containment, isolated engine HOME, and
`bin/ringer-safe-run` policy.

Verification steps:

1. Run `RINGER_NO_SELF_UPDATE=1 python3 -m unittest tests.test_runtime_isolation -v`.
2. Isolated mock run: `RINGER_NO_SELF_UPDATE=1 python3 ./ringer.py --config
   <temp-config> --runtime-root <temp-runtime> run <manifest> --identity
   iso-test --no-dashboard` with engine `mock`. Confirm state lands under
   the runtime root, not `~/.ringer`.
3. Unwritable HOME: chmod `0500` a fake `HOME` and repeat the mock run.
   The run still passes and writes nothing into that HOME.
4. Wrapper allowlist: `bin/ringer-safe-run --manifest /tmp/outside.json
   --identity iso-test` must print `MANIFEST_POLICY_FAILURE` and exit
   non-zero.
