---
id: install-and-cli-ux
type: reference
created: "2026-10-06T21:00:00Z"
consequence: 6
locus: output-meta
scope: repo
summary: How comparable agent tools ship install, global init, per-repo init and harness connect in 2026; PyPI name check; git-hook, merge-driver and CLI table stakes.
---

# Install, init and CLI UX research (2026-10-06)

Web research round. Sources inline. Feeds the install / global-init / repo-init / connect redesign in [[public-release-plan]]; harness file shapes are in [[harness-config-surfaces]].

## Distribution

| Finding | Source |
|---|---|
| PyPI names `shapa`, `shapa-llm`, `shapa-memory` all return 404 on the JSON API (free) on 2026-10-06 | `https://pypi.org/pypi/shapa/json` |
| `uv tool install` / `uvx` is the dominant Python CLI path; uv cold install ~0.1 s vs seconds for pipx | https://pydevtools.com/handbook/explanation/how-do-uv-tool-and-pipx-compare/ |
| PyApp: few-MB native launcher, fetches a standalone Python on first run; PyInstaller: big offline bundle, per-platform builds | https://pydevtools.com/handbook/explanation/how-do-i-ship-a-python-application-to-end-users/ |
| curl-pipe-sh hygiene: whole body in a function called on the last line (truncation-safe), HTTPS only, inspect-then-run alternative, `--non-interactive`, no sudo, XDG paths | https://www.chef.io/blog/5-ways-to-deal-with-the-install-sh-curl-pipe-bash-problem , https://docs.atuin.sh/main/guide/installation/ |

## Install, then init, then per-repo setup in comparable tools

| Tool | Flow | Source |
|---|---|---|
| beads | `bd init` creates `.beads/`, imports git history, then prompts to install git hooks (`bd hooks install`) and a JSONL merge driver (`bd merge`) | https://pkg.go.dev/github.com/steveyegge/beads |
| spec-kit | `specify init NAME --ai claude`, `--no-git`, `--force`, `--ai-skills`; flag-driven | spec-kit docs/installation.md |
| task-master | `task-master init --rules cursor,windsurf -y`; `task-master rules add` later | https://npmjs.com/package/task-master-ai |
| claude-mem | `npx claude-mem install` or the Claude Code `/plugin` marketplace; a per-session check compares an `.install-version` marker and only prints "run repair" (drift detection kept apart from mutation) | https://docs.claude-mem.ai/hooks-architecture.md |
| basic-memory | `uv tool install basic-memory` / `uvx basic-memory`; MCP wiring is a manual JSON edit per client | https://development.docs.basicmemory.com/local/local-install |
| lefthook | `lefthook install` documented idempotent; `--force` overwrites foreign hooks | https://lefthook.dev/usage/commands/install/ |
| git-lfs | `git lfs install` honours an existing `core.hooksPath`; `--worktree` scoping | https://man.archlinux.org/man/git-lfs-install.1.en |
| FireConnect | `fireconnect <harness> on/off` rewrites a harness config and restores it byte-for-byte on off: direct precedent for `shapa connect` | https://github.com/fw-ai/fireconnect |

Common pattern: interactive wizard by default with full flag parity (`-y`, `--ai`, `--rules`), idempotent re-runs, a cheap staleness check separate from the mutating repair.

## Git integration

- `core.hooksPath` is one value; husky and lefthook fight over it. Git 2.54 adds config-based multi-hooks per event (https://www.dsebastien.net/git-2-54-config-based-hooks-replace-husky-and-pre-commit/).
- Local finding: shapa's `install_git_hooks` writes into `core.hooksPath` even when that path is a global, shared hooks directory belonging to another repo, so it can install shapa hooks machine-wide by accident (seen on the maintainer's machine 2026-10-06).
- Merge drivers: `[merge "x"] driver = cmd %O %A %B` in git config plus `path merge=x` in `.gitattributes`; beads merges its JSONL this way (https://graphite.dev/guides/git-merge-driver).
- Committing SQLite: binary diffs, unmergeable, repo growth. Hybrid used elsewhere: JSONL (or SQL text) tracked as source of truth, SQLite as a gitignored cache (https://www.mintlify.com/stoneforge-ai/stoneforge/concepts/storage-model). shapa today tracks `shapa.db` as `binary` and allows commits only on the default branch.

## CLI table stakes

- clig.dev: human output by default, stable `--json` / `--plain`, stdout vs stderr, exit codes, `NO_COLOR` (https://github.com/cli-guidelines/cli-guidelines). Agents are first-class CLI users, so machine output matters.
- Shell completions: Typer/Click give them for free; a stdlib core needs hand-written bash/zsh/fish snippets or an optional extra.
- `doctor` as a living diagnostic (atuin) and a self-update or re-run-installer story.
- Idempotent jq-free merge of Claude Code `settings.json` with timestamped backup and atomic write exists in the wild (context: https://github.com/anthropics/claude-code/issues/11392).

## Raw design options (no pick)

- Install: PyPI `shapa` then `uv tool install shapa` / `pipx install shapa` / `uvx shapa`; hardened `bootstrap.sh`; PyApp binary; Homebrew tap.
- Global location: inside the tool's home (`~/.shapa/`), XDG data dir (`~/.local/share/shapa`), or wherever the user runs `shapa init --global`; wizard prompt with default.
- Repo state: keep committed SQLite (default-branch-only), or JSONL tracked plus SQLite cache plus merge driver.
- Connect: explicit `shapa connect <harness>` vs detect-all wizard; symmetric `disconnect` restoring the original; stdlib JSON merge with backup; TOML marker blocks (stdlib `tomllib` reads only).

Related: [[installer]] · [[cli-and-hooks]] · [[harness-config-surfaces]]
