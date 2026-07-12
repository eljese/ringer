# Model identity taxonomy

This document is Ringer's normative contract for model identity. Every scoreboard surface must be able to answer: **which lab's model, in which harness, on whose plan, at what effort?** These fields describe different things and must not be conflated.

## Model

A model is a trained artifact produced by a lab. A model name is never a test-fixture name, harness product name, CLI name, or billing plan. When a harness explicitly sets reasoning effort, the model's scoreboard identity includes that effort.

Registry aliases are allowed only when the actual model lineage is not established. They must be marked as aliases in both the registry and the displayed name; they must not be presented as a confirmed model lineage.

## Lab

The lab is the organization that trained the model, such as OpenAI, xAI, Z.ai (Zhipu AI), Moonshot AI, NVIDIA, Meta, or Cursor (Anysphere). A harness, CLI, OAuth plan, or API provider is never a lab.

Registered models use the lab recorded in `registry/model-identity.toml`. An unregistered OpenRouter slug may show its organization segment with `?` as an explicitly unverified best-effort value. Other unregistered models show `(unverified)`.

## Harness

The harness is the agent shell that invokes the model: Codex CLI, Grok Build CLI, or OpenCode. It runs a model but does not become the model or its lab.

Worked example, because this exact conflation has happened: **"Grok Build" is a harness, never a model and never a lab.** The Grok Build CLI serves exactly two models — Grok 4.5 (lab: xAI) and Composer 2.5 (lab: Cursor/Anysphere). There is no model called "Grok Build"; a scoreboard row named after a harness is a taxonomy bug.

## Access/Plan

Access/Plan describes billing and access, such as an OAuth plan or the OpenRouter API. It does not identify the trained model, its lab, or its harness.

## Reasoning effort

Reasoning effort is part of model identity when the effective harness invocation sets it. Ringer records only explicit values; it never guesses a harness-side default. If any run for a model records effort, that model's buckets display the recorded value or `(effort unrecorded)` so unlike configurations remain separate. Harnesses and models with no recorded effort remain unsuffixed.

## How to establish identity

Run this procedure when a new slug appears, Ringer writes an identity mismatch warning, or a registry entry needs re-verification. Ringer's evidence precedence is **harness-reported model > manifest/config-resolved model > unattributed**. A line beginning `[ringer.py] identity:` and the scoreboard's `Unregistered model slug(s)` pointer are direct triggers to do this work.

1. **Codex CLI:** Open any worker log from the run and read the self-reported `model:` and `provider:` lines in the Codex header. Record the model slug exactly, then cross-check it against <https://developers.openai.com/codex/models>. The self-report wins when it differs from the manifest or config; the attempt row retains the resolved slug as `expected_model` so the drift is visible.
2. **Grok Build CLI:** Run `grok --help` and inspect any available models listing, then cross-check xAI's release notes at <https://docs.x.ai/developers/release-notes>. The CLI currently serves Grok 4.5 by xAI and Composer 2.5 by Cursor/Anysphere. Grok's JSON output does not self-report the model, so the explicit manifest/config slug is the evidence for the attempt.
3. **OpenCode/OpenRouter:** Run `./ringer.py catalog` to use Ringer's local snapshot, or fetch `GET https://openrouter.ai/api/v1/models` when performing the identity research outside Ringer. Match the slug without its `openrouter/` prefix to the catalog `id`. Use the slug's organization segment and the catalog `name` field for the provisional lab and display name. Confirm the lab's own model page before changing the catalog-derived `?` lab to a verified registry identity.
4. **Record the evidence:** Add or update the entry in `registry/model-identity.toml`. Set `display`, `lab`, `confidence`, `source`, and `last_verified` from the checks above, using today's ISO date. Then run the identity tests and inspect all scoreboard surfaces.

Use this entry template verbatim, replacing only the bracketed values:

```toml
[engines.<engine>.models."<model-slug>"]
display = "<display-name>"
lab = "<lab-name>"
confidence = "verified"
source = "<source-url>"
last_verified = <YYYY-MM-DD>
```

Keep the prohibitions short: do not use a harness, CLI, provider, plan, or fixture name as a model or lab; do not mark derived `?` identity as verified without the lab's source; do not assign unattributed legacy results to an engine default.

## Reserved fixture names

The names `proven-model`, `probation-model`, `mock-model`, and `test-model` are reserved for tests. Raw log rows may retain them, but they are excluded from every scoreboard aggregation, ranking, tier, JSON payload, and HTML surface and will never display.

## Unattributed rows

An unattributed row is a historical log row whose `model` field is empty or blank. It is not a run where the manifest omitted a model and Ringer resolved and stamped the engine default at write time.

Unattributed rows are quarantined per engine under `(unattributed legacy rows)`. They remain visible at the bottom of the scoreboard for data transparency, but they are never credited to an engine default or any real model, never receive a proven/probation tier, and never receive a rank. Their results cannot establish a model's record because their actual model identity is unknown.
