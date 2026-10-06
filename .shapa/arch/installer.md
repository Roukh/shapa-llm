---
id: installer
type: reference
created: "2026-10-05T20:22:00Z"
consequence: 7
locus: output
scope: repo
summary: bootstrap.sh and install.sh get a fresh machine from nothing to a wired shapa - hooks, optional extras, MCP registration, the shapa-upgrade skill.
---

# Installer

Gets a stranger from nothing to a wired, working shapa with one command.

## Owns

- `bootstrap.sh` - the `curl | sh` one-liner: installs the `shapa` command (pipx > uv > pip --user), then runs `install.sh`.
- `install.sh` - wires harness hooks, optional MCP server registration, the `shapa-upgrade` skill, and an Obsidian vault when present.
- `shapa/assets/` - what ships into a fresh wiki: `AGENTS.md`, `placement.md`, the `arch/index.md` template, `skills/shapa-upgrade`.

## Interfaces in

- `curl -fsSL .../bootstrap.sh | sh` (flags: `--with-semantic`, `--with-mcp`, `--full`).
- A local clone's `./install.sh` (flags: `--memory DIR`, `--harness claude|codex|opencode|all`, `--mcp`/`--no-mcp`, `--dry-run`, `--no-embeddings`, `--no-obsidian`, `--uninstall`).

## Interfaces out

- An installed `shapa` binary; wired hooks in the harness's own settings; a scaffolded wiki (`AGENTS.md`/`placement.md`/`arch/`, with `shapa.db` created on first ledger/row write); the `shapa-upgrade` skill; a registry entry in `~/.shapa/wikis.json`.

## Invariants

- Core install is pure stdlib; `[semantic]`/`[mcp]` extras are asked for interactively on a TTY and skipped (with the follow-up command printed) otherwise - never silently assumed.
- Only `shapa init --global` ever writes the global wiki pointer (`~/.shapa/config.json`); a bare `shapa init` or `shapa init DIR` never does.
- `install.sh` is idempotent and routes hook/MCP registration per `--harness` to the right config surface (the harness's own settings, `~/.codex/config.toml`, `opencode.json`).
- Ends with `shapa upgrade --all --check`: the installer's own exit code says whether every wiki in scope is current.
- A change to a hook path counts as done only after a live, non-synthetic session (real or scratch settings) shows the expected merged output - green unit tests alone are not acceptance.
