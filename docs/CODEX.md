# Codex CLI integration

This document describes how Ringer integrates with the Codex CLI when you
run `./ringer.py install-agent`. The Claude Code side is documented in
`docs/CLAUDE-CODE.md`.

## What install-agent does for Codex

`install-agent` configures Ringer as a Codex plugin using the Codex 0.144.0+ marketplace and cache architecture.

### 1. Staging the Plugin
The installer stages a complete copy of the Ringer plugin from the repository's `plugins/ringer` bundle:

| Scope | Staged Location |
|---|---|
| User Scope | `~/plugins/ringer/` |
| Project Scope | `./.agents/plugins/ringer/` |

During staging:
- The `SKILL.md` is refreshed from the canonical repository source (`.claude/skills/ringer/SKILL.md`).
- The nudge handler script `ringer_nudge.py` is refreshed from `hooks/ringer_nudge.py`.
- The plugin includes the default `hooks/hooks.json` configuration file, which configures hooks to run via `${PLUGIN_ROOT}`.
- A fresh strict-semver cachebuster (for example, `1.0.0+codex.local-20260710-063500-123456`) is assigned to the staged `.codex-plugin/plugin.json` so Codex refreshes its installation cache. The committed source manifest remains at its base version. Verified empirically on 2026-07-10 against Codex 0.144.0: after a re-install, the cache directory `~/.codex/plugins/cache/personal/ringer/<new-version>/` was created with the new cachebuster, and the old version was retained for rollback. To verify on your machine, see the "Verify Codex Cache" step under [Manual verification](#manual-verification).

The installer detects when the user is running from inside the ringer repository with `$HOME` pointing at the repo and skips the in-place staging mutation. The marketplace entry is still registered, and Codex will load the plugin directly from the repository path on each `codex` invocation.

### 2. Marketplace Registration
The installer registers the staged plugin in a local marketplace file:

| Scope | Marketplace JSON Path |
|---|---|
| User Scope | `~/.agents/plugins/marketplace.json` |
| Project Scope | `./.agents/plugins/marketplace.json` |

New user marketplaces are named `personal`; new project marketplaces are named `ringer-project`. If the marketplace already has a non-empty name, the installer preserves and uses it.

The ordered `plugins` array contains a Ringer entry with:
- `name`: `ringer`
- `source`: `{"source": "local", "path": "./plugins/ringer"}` for user scope, or the project path `./.agents/plugins/ringer`
- `policy.installation`: `AVAILABLE`
- `policy.authentication`: `ON_INSTALL`
- `category`: `Developer Tools`

Other pre-existing entries and metadata in the marketplace file are preserved.

### 3. Installation execution
The installer runs `codex plugin add ringer@<marketplace-name>` to import, cache, and register the plugin. For a new user marketplace, that selector is `ringer@personal`; for a new project marketplace, it is `ringer@ringer-project`.

### 4. Legacy Cleanup
Any legacy files from the old installer (`~/.codex/plugins/ringer` or project equivalent, and `[plugins.ringer]` in `.codex/config.toml`) are cleanly removed to prevent conflicts.

**Note:** Plugin enablement remains host-global in Codex. Even when the source marketplace and staged plugin directory are project-local (`--project`), the `~/.codex/plugins/cache/` copy is host-wide.

## Hook trust requirement

To prevent unauthorized shell command execution, Codex does not run hooks automatically after installation. You must explicitly review and trust the plugin hooks.

See the "Verify Hooks Trust" step in the [Manual verification](#manual-verification) section below for the exact command.


## Manual verification

After `./ringer.py install-agent`:

1. **Verify Staged Files**:
   ```bash
   ls ~/plugins/ringer
   # .codex-plugin  hooks  skills
   ```

2. **Verify Marketplace Entry**:
   ```bash
   cat ~/.agents/plugins/marketplace.json
   ```
   Should contain the Ringer entry with `"source": {"source": "local", "path": "./plugins/ringer"}`.

3. **Verify Codex Cache**:
   Codex installs the cached plugin under `~/.codex/plugins/cache/`. Verify that the cached plugin exists and contains the fresh cachebustered version in its `plugin.json`.

4. **Verify Hooks Trust**:
   To trust the hooks, start a new Codex session, open `/hooks`, review the exact hook, and trust it there.


## Uninstall

```bash
./ringer.py uninstall-agent          # both engines
./ringer.py uninstall-agent --no-claude   # codex only
```

Uninstall performs the following:
1. Runs `codex plugin remove ringer@<marketplace-name>` using the same preserved selector as installation.
2. Removes only the staged `~/plugins/ringer` directory (or the project staged path).
3. Strips the `ringer` entry from the marketplace JSON, preserving all other entries.
4. Cleans any legacy Ringer state if present.
