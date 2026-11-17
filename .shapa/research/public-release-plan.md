---
id: public-release-plan
type: reference
created: "2026-10-06T21:40:00Z"
consequence: 8
locus: output
scope: repo
summary: What a public shapa needs - install, init and harness connect as commands, repo polish, feature candidates - from the 2026-10-06 research round.
---

# Public release (research 2026-10-06)

## Question

Operator ask, 2026-10-06: an impressive public repo and a genuinely useful tool. CLI and shell matter. `shapa init` in a repo creates `.shapa`, links git and starts everything. Each harness is configured by one simple command that finds its config files and connects shapa and its hooks. Global init happens where the user starts it, by default in shapa's own home under the user's directory. Install, global init and repo init are three clear processes. What does shapa need to get there, and which new features does the research support?

## Method

shapa 0.8.0 checked locally against the ask (table below), then the synthesis of five research files: [[agent-memory-problems]], [[agent-work-problems]], [[memory-tool-landscape]], [[install-and-cli-ux]], [[harness-config-surfaces]]. The operator's decisions on it (2026-10-08) are decision memories in this wiki (`shapa fetch --tag decision`); since then `shapa init` (end to end), `shapa doctor` and `shapa commit` shipped.

## Where shapa stood (0.8.0, checked 2026-10-06)

| Area | Today | Gap against the ask |
|---|---|---|
| Install | `bootstrap.sh` (pipx, uv or pip from git) runs bash `install.sh`, which needs bash and jq | not on PyPI; harness wiring is shell only; jq missing means no hooks |
| Global init | `install.sh` runs `shapa init --global ~/.shapa/memory` with its output discarded | location never shown or asked; `~/.shapa/memory` name predates format 4 |
| Repo init | bare `shapa init` creates a visible `./shapa/` in the cwd (not the git root), plus `.obsidian/`; discovery already prefers `.shapa/` | no `.shapa/`, no git hooks, no root detection, no commit, no harness check, no next steps |
| Git link | separate `shapa ledger git-hooks` | writes into `core.hooksPath` even when that is a global hooks dir shared by every repo (seen on the maintainer's machine) |
| Harness connect | Claude Code hooks; Codex and OpenCode MCP only; no Gemini, Cursor, Copilot and others | no `shapa connect`; no disconnect except `install.sh --uninstall`; edited configs not backed up |
| Health | `shapa doctor` | prints "problems: none" with no global wiki and no hooks wired (reproduced in a scratch repo) |
| Repo presence | 2 stars, no description, topics, tags or releases; README 374 characters | none of the adoption table stakes in [[memory-tool-landscape]] |

## Command surface (proposed)

Three processes, each one command, each idempotent, each with `--yes` (no prompts), `--dry-run` (print the plan and the config diffs) and `--json`.

### 1. Install

- `curl -fsSL .../install.sh | sh`, `uv tool install shapa`, `pipx install shapa` or `uvx shapa setup`: all end in `shapa setup`.
- The shell script shrinks to: find or install uv, then pipx, then pip; install `shapa`; run `shapa setup`. All config editing moves into Python (stdlib `json`, marker blocks for TOML), so jq and bash are no longer needed.
- `shapa setup` is the wizard: where the global wiki goes (default shown), which detected harnesses to connect (detected ones pre-selected), extras (`[semantic]`, `[mcp]`), then `shapa doctor`. `shapa setup --yes` takes every default.
- `shapa uninstall`: disconnects every harness from the stored connection record and restores backups; the wikis are left in place.

### 2. Global init

- `shapa init --global [DIR]`: creates the global wiki, writes the pointer, registers it.
- Default when DIR is omitted (operator 2026-10-08): inside shapa's install home in the user's home directory. `~/.shapa/` holds the tool, `config.json`, `wikis.json` and the global wiki at `~/.shapa/global`; `$SHAPA_HOME` overrides. The old `~/.shapa/memory` default moves there on upgrade.
- Prints the path, what reads it, and how to move it (`shapa init --global NEWDIR` moves the pointer; hooks follow automatically because they resolve through it).

### 3. Repo init

`shapa init` inside any git repo:

1. Resolves the git top level (a subdirectory never gets its own wiki); outside git it stops with a message (`--no-git` allows it).
2. Creates `<root>/.shapa/` with `AGENTS.md`, `placement.md`, `arch/index.md`, `shapa.db`, `.gitignore`, `.gitattributes`, `.shapa-format`. Obsidian scaffolding only with `--obsidian`.
3. Links git: pre-commit and post-commit hooks chained safely (husky, lefthook, pre-commit and a global `core.hooksPath` detected, with the snippet printed instead of writing into a shared directory); records the default branch and remote.
4. Registers the repo with the global wiki and the registry.
5. Seeds: offers to import existing `CLAUDE.md`, `AGENTS.md`, `.cursor/rules` and `.github/copilot-instructions.md` lines as rule rows; drafts `arch/index.md` from the directory layout.
6. Checks harness wiring; offers `shapa connect` when nothing is connected.
7. Prints a short "what happens next" (session start shows memory, corrections become issues, `J<n>:` commits close jobs).

Footprint (operator 2026-10-08): init creates `.shapa/` and nothing else in the working tree; it never edits the repo's `AGENTS.md` or `CLAUDE.md`. The git link lives in git's own hooks directory, which is not tracked. Committing `.shapa/` is shapa's job on the default branch (today only the maintainer's private harness hook commits the database, so a stranger's wiki never gets committed).

### 4. Harness connect

- `shapa connect` (detect and choose), `shapa connect claude codex gemini`, `shapa connect --all`, `shapa connect --list` (detected, connected, mode), `shapa disconnect <harness>`.
- Per harness: hooks where the harness has them, MCP registration, the `shapa-upgrade` skill where skills exist, and an instruction-file pointer; user scope by default, `--project` for a repo-local setup.
- Every edit: backup with timestamp, atomic write, marker or owned-key so re-runs are no-ops and disconnect removes exactly shapa's entries; JSONC files edited without dropping comments, or refused with the snippet printed.
- Native packages as a second path: Claude Code plugin (`.claude-plugin/` with hooks, MCP, skills; `/plugin install`), Gemini extension, and whatever packaging [[harness-config-surfaces]] confirms for others.
- Harness coverage and file shapes: [[harness-config-surfaces]]. Seven harnesses (Claude Code, Codex, Qwen Code, Factory Droid, Copilot CLI, Cursor, Gemini CLI) accept Claude-style hooks with context injection, so one hook contract plus a per-harness event and path map covers them. OpenCode, Amp and Goose need a generated plugin shim. Cline, Kiro, Crush and Windsurf take partial hooks. Copilot in VS Code, Zed, Continue, Augment, Kilo and Warp get MCP plus an instruction pointer.

### 5. Health

`shapa doctor` checks: binary on PATH and version, global wiki present, current repo wiki present, git hooks installed and not shadowed, each connected harness still wired (config entry present, binary path valid), extras importable, index fresh. Exit 1 on any problem, each with its fix command.

## Repo polish (adoption table stakes)

Each is an independent item; examples in [[memory-tool-landscape]].

- PyPI release as `shapa` (name free 2026-10-06) with trusted publishing from tagged GitHub releases; CHANGELOG; semver tags.
- README rebuilt: one-line install, 30-second demo GIF (recorded with `vhs` from a tape file in the repo), what it does in three bullets, harness support matrix, comparison table (claude-mem, mem0, basic-memory, beads, native memories), Mermaid architecture diagram, badges.
- Repo description, topics, social preview image.
- `docs/` site (MkDocs Material on GitHub Pages) or a longer README; CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue and PR templates.
- Shell completions (bash, zsh, fish) generated by `shapa completions SHELL`, stdlib only.
- `--json` on every read command; `NO_COLOR` respected.
- Reproducible benchmark: the in-repo retrieval eval extended into a public harness (recall@k, no-answer precision, token cost, p50 latency) against named baselines.
- Listings: Claude Code plugin marketplace, MCP registry, Smithery.
- Windows and macOS in CI.

## Feature candidates from the research

Raw, not ranked; tagged with the problem they hit. "Today" marks partial coverage that already exists. Full lists with sources: [[agent-memory-problems]] (M), [[agent-work-problems]] (W).

| Candidate | Problem | Today |
|---|---|---|
| Pinned rows re-injected after compaction (PreCompact / post-compact SessionStart hook) | M2 compaction, W1 plan loss | per-prompt recall only |
| `shapa resume`: open feature, open jobs, last verify failure as the first injection | W1 | session-start overview |
| Hard token budget on everything shapa injects, ranked truncation | M1 rule fade, M3 context rot | relevance floor |
| "Changed since your last session" digest at start | M1, M3 | full overview |
| Last-verified date per row, staleness badge, re-verify prompt | M4 staleness | sweep deletes stale rows |
| Contradiction pass at write time: new row vs closest existing, supersede or ask | M4 | none |
| Trust tier per row (operator said, agent derived, read from file or web) and a quarantine queue | M8 poisoning, M9 secrets | `origin` column |
| Secret scan before every write | M9 | `redact.py` on capture |
| Readable git diffs of `shapa.db` (textconv driver) | M7 audit, M5 team | binary |
| Row-level merge: JSONL tracked plus SQLite cache plus merge driver, or a SQLite merge driver | W7 unmergeable store, M5 | default-branch-only commits |
| Audit trail per write (session, harness, time, source) and `shapa log` | M7 | partial |
| Export and import to a documented JSONL or Markdown format | M6 lock-in | none |
| Verify result stored on the closed row; `shapa audit` re-runs closed verifies | W2 fake done | verify runs at close |
| `blocks` edges plus `shapa ready` | W8 dependencies | containment only |
| Claims with TTL, released at session start; "who else is on this file" | W3 collisions | claim/release |
| Overlap warning: open features touching the same files | W3 | none |
| Scope check: `J<n>:` commit size vs job; feature diff vs its goal | W4 scope creep | none |
| `review` state with an approve command | W12 review bottleneck | none |
| Stale-worktree sweep in the after-merge pass | W9 | temp dirs only |
| Shadow recall log: injected vs used, as a "does memory help" signal | M12 evaluation | use counters |
| Local web or TUI viewer for rows and ledger (`shapa ui`) | M7 inspectability | Obsidian, `row list` |
| Team mode: shared repo wiki through git, per-row visibility | M5 team memory | repo wiki in git |

## Features filed from it (2026-10-08) and where they stand

| Feature | Covers | State (2026-11-16) |
|---|---|---|
| F8 | install and `shapa setup`, shapa's home directory, Python config editing, `shapa uninstall` | closed as expired by the sweep bug (J73) |
| F9 | `shapa init` end to end: git root, `.shapa/`, safe git hooks, seed import, next steps | merged |
| F10 | `shapa connect` for every harness | closed as expired (J73) |
| F11 | `shapa doctor` checks the whole install | merged |
| F12 | portable, committable, exportable database: export/import, textconv, shapa commits `.shapa/` | commit built; closed as expired (J73) |
| F13 | PyPI release, README, repo metadata, completions, macOS/Windows CI, listings | gone from the ledger |
| F14-F16, F18-F20 | compaction and token budget, trust and freshness, ledger proves done, measure memory, `shapa ui`, team mode | closed as expired (J73) |
| F17 | ledger coordinates parallel agents | open |

## Sources

The five research files named under Method (each cites its own sources), and the shapa 0.8.0 code checked locally.

## Date

2026-10-06 (research), 2026-10-08 (operator decisions), feature states 2026-11-16.

Related: [[installer]] · [[cli-and-hooks]]
