---
id: installer
type: reference
created: "2026-10-05T20:22:00Z"
consequence: 7
locus: output
scope: repo
summary: bootstrap.sh and install.sh get a fresh machine from nothing to a wired shapa - hooks, optional extras, MCP registration, the shapa-upgrade skill, and shapa's system prompt in each harness (shapa harness).
---

# Installer

Gets a stranger from nothing to a wired, working shapa with one command.

## Owns

- `bootstrap.sh` - the `curl | sh` one-liner: installs the `shapa` command (pipx > uv > pip --user), then runs `install.sh`.
- `install.sh` - wires harness hooks, optional MCP server registration, the `shapa-upgrade` skill, shapa's system prompt (it calls `shapa harness install`), and an Obsidian vault when present.
- `shapa/harness.py` (`shapa harness install|uninstall|status|watch`) - composes `assets/prompts/core.md` and `assets/prompts/adapters/<harness>.md` into `~/.shapa/prompts/<harness>.md` and wires it into Claude Code, Codex, OpenCode and Gemini CLI. `harnesswatch.py` fingerprints the harness defaults; `hookcap.py` keeps the SessionStart hook output under 8,000 characters.
- `shapa/assets/` - what ships into a fresh wiki: `AGENTS.md`, `placement.md`, the `arch/index.md` template, `skills/shapa-upgrade`; and, never into a wiki, `prompts/` (core and adapters).

## Interfaces in

- `curl -fsSL .../bootstrap.sh | sh` (flags: `--with-semantic`, `--with-mcp`, `--full`, `--prompt-mode`).
- A local clone's `./install.sh` (flags: `--memory DIR`, `--harness claude|codex|opencode|gemini|all`, `--mcp`/`--no-mcp`, `--prompt-mode append|replace|default|skip`, `--no-prompt`, `--dry-run`, `--no-embeddings`, `--no-obsidian`, `--uninstall`).
- `shapa harness ...` directly, with `--harness`, `--mode`, `--dry-run`, `--json`, `--force`, `--follow-symlinks`, `--allow-repo-config`.

## Interfaces out

- An installed `shapa` binary; wired hooks in the harness's own settings; a scaffolded wiki (`AGENTS.md`/`placement.md`/`arch/`, with `shapa.db` created on first ledger/row write); the `shapa-upgrade` skill; a registry entry in `~/.shapa/wikis.json`.
- Per harness, user-level only: the prompt file and `harness.json` under `~/.shapa/`, plus one of: a managed block in `CLAUDE.md` / Codex `AGENTS.md` / `GEMINI.md` or an `instructions` entry (append, the default); the Claude Code `agent` setting and `agents/shapa.md`, Codex `model_instructions_file`, OpenCode `agents.<id>.system`, a printed `GEMINI_SYSTEM_MD` line (replace). Each shared config is backed up once to `<file>.shapa-bak`.

## Invariants

- Core install is pure stdlib; `[semantic]`/`[mcp]` extras are asked for interactively on a TTY and skipped (with the follow-up command printed) otherwise - never silently assumed.
- Only `shapa init --global` ever writes the global wiki pointer (`~/.shapa/config.json`); a bare `shapa init` or `shapa init DIR` never does.
- `install.sh` is idempotent and routes hook/MCP registration per `--harness` to the right config surface (the harness's own settings, `~/.codex/config.toml`, `opencode.json`).
- Ends with `shapa upgrade --all --check`: the installer's own exit code says whether every wiki in scope is current.
- A change to a hook path counts as done only after a live, non-synthetic session (real or scratch settings) shows the expected merged output - green unit tests alone are not acceptance.
- The system prompt step never blocks the install: a refusal (malformed config, symlink, an existing user value, a config directory inside a repo) warns and leaves the file as it was. Default mode is `append`; `replace` is an explicit per-harness opt-in; a stored mode survives re-installs. Nothing session-specific goes into the composed prompt: the SessionStart hook delivers it.
- Uninstall restores each touched config byte for byte (the backup) while it is as install left it, and otherwise removes only shapa's edit. No project-level file is written, and no shell rc file is ever edited.
- What a harness keeps whatever shapa installs (tool descriptions, environment blocks, project instruction files, sandbox and approval text, Gemini's GEMINI.md block) is listed by `shapa harness status`. Never copy a vendor's prompt text into this repository: the watch stores hashes and heading lists only.
