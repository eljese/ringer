# Codex CLI integration

This document describes how Ringer integrates with the Codex CLI when you
run `./ringer.py install-agent`. The Claude Code side is documented in
`docs/CLAUDE-CODE.md`.

## What install-agent does for Codex

`install-agent` installs a minimal `ringer` Codex plugin under the user
(or project) config root:

| Path under ringer repo | Installed to (user scope) | Installed to (project scope, with `--project`) |
|---|---|---|
| `plugins/ringer/.codex-plugin/plugin.json` | `~/.codex/plugins/ringer/.codex-plugin/plugin.json` | `./.codex/plugins/ringer/.codex-plugin/plugin.json` |
| `plugins/ringer/hooks.json` | `~/.codex/plugins/ringer/hooks.json` | `./.codex/plugins/ringer/hooks.json` |
| `plugins/ringer/skills/ringer/SKILL.md` | `~/.codex/plugins/ringer/skills/ringer/SKILL.md` | `./.codex/plugins/ringer/skills/ringer/SKILL.md` |

The install also writes `[plugins.ringer] enabled = true` into the
matching `config.toml` (preserving all other keys).

The skill payload is the same `SKILL.md` that Claude Code uses; Ringer
copies it from `.claude/skills/ringer/SKILL.md` at install time so the
repo has a single source of truth.

## Why a plugin (not a direct skill copy)

Codex has no top-level user skills directory. Skills only ship inside
plugins, registered in `config.toml` under `[plugins.<name>]`. So the
smallest faithful unit of "install the ringer skill for Codex" is a
plugin that bundles the skill plus its hook definitions together.

## Why only PreToolUse (not PostToolUse)

Codex hook events are narrow on purpose: `PreToolUse` and `PostToolUse`
hooks only fire on `Bash` tool calls. They never receive `Edit` or
`Write` events. The existing `ringer_nudge.py` script has two actions:

- `pre-bash` — nudges when a Bash command looks like a model call or a
  harness script (matches `api.openai.com`, `api.anthropic.com`,
  `simulate*.py`, `harness*.mjs`, etc.)
- `post-edit` — nudges when an edit loop accumulates ≥ 8 edits across
  ≥ 3 files without a live Ringer run

`post-edit` is designed for `Edit`/`Write` events. Under Codex it would
receive only Bash events and never fire, so Ringer installs only
`PreToolUse Bash -> pre-bash` for Codex. The edit-loop nudge is
Claude-only.

## Hook command substitution

The committed `plugins/ringer/hooks.json` contains a
`__RINGER_NUDGE_PATH__` placeholder. At install time, Ringer rewrites it
with the absolute path to its own `hooks/ringer_nudge.py`, so the plugin
works regardless of where the user cloned the repo:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /home/<user>/ringer/hooks/ringer_nudge.py pre-bash"
          }
        ]
      }
    ]
  }
}
```

Re-running `install-agent` recreates the plugin tree and re-runs the
substitution, so moving the ringer repo between installs updates the
absolute path automatically.

## Manual verification

After `./ringer.py install-agent`:

```bash
# Plugin files
ls ~/.codex/plugins/ringer
# .codex-plugin  hooks.json  skills

# Hook command points at the ringer repo
cat ~/.codex/plugins/ringer/hooks.json

# Plugin is registered
python3 -c "import tomllib; \
  print(tomllib.load(open('$HOME/.codex/config.toml','rb'))['plugins']['ringer'])"
# {'enabled': True}

# Skill byte-matches the canonical source
diff ~/.codex/plugins/ringer/skills/ringer/SKILL.md \
     <repo>/.claude/skills/ringer/SKILL.md
```

Live nudge probe — start a Codex session and run something that should
trigger the pre-bash nudge:

```
echo api.openai.com
```

The session receives a single nudge pointing at the ringer skill. The
nudge is deduplicated per session (one nudge per session per event) and
silent inside a live Ringer run.

## Uninstall

```bash
./ringer.py uninstall-agent          # both engines
./ringer.py uninstall-agent --no-claude   # codex only
```

Removes `~/.codex/plugins/ringer/` and strips `[plugins.ringer]` from
`config.toml` (leaving any other `[plugins."..."]` entries alone). The
matching `config.toml.bak-<UTC-stamp>` is preserved on disk for recovery.

## Known limitations

- **Hand-rolled TOML emitter.** Ringer rewrites `config.toml` with a
  small in-tree emitter (`write_toml_settings` in `ringer.py`) rather
  than taking on a `tomli_w` dependency. The subset covers the shapes
  observed in real Codex configs: strings, bools, ints, floats,
  nested tables, arrays of tables, and quoted keys with embedded `@`,
  `.`, or `/`. Inline tables (`a = {x = 1}`) and heredocs are not
  supported; if your config uses them, file an issue and the emitter
  will be extended or the project will adopt `tomli_w`.
- **Codex hook delivery is Bash-only.** This is a Codex design
  constraint, not a Ringer limitation. The edit-loop nudge is
  Claude-only by design.
- **Plugins are not project-isolated by Codex.** Codex applies
  `[plugins."<name>"] enabled = true` from the user's `config.toml`
  globally. The `--project` install scope for Codex is provided for
  parity with the Claude install path but is uncommon in practice.

## Source layout

```
ringer/
├── plugins/
│   └── ringer/
│       ├── .codex-plugin/
│       │   └── plugin.json
│       ├── hooks.json
│       └── skills/
│           └── ringer/
│               └── SKILL.md    # copied from .claude/skills/ringer/SKILL.md at install time
├── .claude/
│   └── skills/
│       └── ringer/
│           └── SKILL.md        # canonical skill source
├── hooks/
│   └── ringer_nudge.py         # shared hook script (pre-bash, post-edit)
└── ringer.py                   # install_agent / uninstall_agent + TOML emitter
```