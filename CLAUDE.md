# CLAUDE.md — Ringer (eljese fork)

Project-specific rules for Claude Code / MiniMax working in this checkout.

## Hard rule: fork only

- This is `eljese/ringer`, a personal fork of `NateBJones-Projects/ringer`.
- **Never push, PR, or issue `gh` commands against the upstream
  `NateBJones-Projects/ringer`**. Branches, PRs, and issue comments live
  on the fork.
- The git remotes are wired fork-first: `origin` = `eljese/ringer`,
  `upstream` = the original. Treat `upstream` as a one-way read source
  for `git fetch` only; nothing leaves it via `git push` or `gh pr`.
- When invoking `gh`, pass `--repo eljese/ringer` explicitly, or alias
  it locally, so an inherited default pointing at the upstream does not
  cross the line.

## Branch + PR workflow

1. **Issue first.** Pick the open issue on `eljese/ringer`. Read it
   end-to-end, including reproduction steps, environment, related PRs.
2. **Branch off `main`.** Name the branch `fix/<slug>` for bugs or
   `feat/<slug>` for new engine lanes / templates. One branch per
   issue.
3. **Implement + test + docs together.** No fix without a regression
   test; no testable engine change without a `docs/<engine>.md` blurb.
4. **Commit messages are conventional.** `<type>(<scope>): <sentence>`.
   `<type>` is `feat|fix|refactor|docs|test|chore|perf`. Keep subject
   ≤ 72 chars; body explains "what/why" not "what/how". Attribution is
   disabled globally in `~/.claude/settings.json` — do not add
   `Co-Authored-By` trailers.
5. **Push to `origin` only.**
   `git push -u origin <branch>`.
6. **Open a PR via `gh pr create --repo eljese/ringer`.** Body must
   include:
   - Summary (1–3 lines)
   - Behaviour added / changed (bullet list)
   - Tests run (commands + outcomes)
   - Test plan checkboxes (so the reviewer can tick them)
   - Outside scope (what was intentionally NOT done)
7. **Never force-push to shared branches.** `--force-with-lease` only on
   your own branch if you must rebase after review.

## Review loop

The local rule is `codex + agy review` per substantive code change.
Both engines are real CLIs (`codex review ...`, `agy -p "<prompt>"`).

In an inline coding session the harness auto-classifier blocks direct
`codex`/`agy` invocations as a "third-party call". When that happens:

1. Spawn two parallel `general-purpose` sub-agents. Each runs exactly
   one CLI (codex or agy) against the staged diff, captures output,
   and returns a bullet-list of findings with severity and file:line.
2. If the sub-agents can't finish the CLI call either, fall back to a
   self-review pass that exercises the same checklist (correctness,
   security, race conditions, missing edge cases, bash idiom / quoting
   under `set -euo pipefail`, test gaps) and document what was
   self-reviewed vs. machine-reviewed in the PR body.
3. Apply real findings. Ignore false positives. Re-run the test suite
   after each apply.
4. Re-review at least once. Two rounds minimum for non-trivial fixes.

### Severity bar

- **Critical / High** — fix before merge.
- **Medium** — fix in this PR if cheap; otherwise file a follow-up.
- **Low / nit** — note in PR body, do not block.

## Test discipline

- TDD where practical; regression tests are mandatory for bug fixes.
- Engine wrapper changes get a `tests/test_<engine>_<scope>.py` with
  stdlib `unittest` and a stub binary in a tempdir so it runs offline.
- One assertion per test method, named after the behaviour it proves.
  No `test_main` mega-tests.
- `python3 tests/test_*.py -v` is the local green gate. Run the broader
  `pytest tests/ --deselect tests/test_design_reference.py
  --deselect tests/test_scoreboard_page.py` before pushing (those two
  files have pre-existing failures from hardcoded macOS paths and date
  stamps — unrelated to most changes; flag if your change touches
  either).

## Style

- Bash: `set -euo pipefail`, quote everything, `command -v` instead of
  `which` for portability. Trap cleanup on `EXIT`. No `eval`.
- Python: stdlib first; reach for `pytest` only via the existing test
  modules. Type hints on new code.
- Docs: terse, second person, every claim sourced (commit hash, manual
  verification step, or upstream URL). No emojis.
- Many small files over few large ones. 200–400 lines typical, 800 max.

## When you are stuck

- Re-read the issue body and the linked PR / doc — most "stuck" moments
  are the bug already documented.
- If a hook or the harness refuses a tool, do not work around it.
  Surface the refusal and ask the user how to proceed.
- This is a non-commercial fork. There is no deadline. Prefer a
  documented `won't-fix` over a rushed PR.
