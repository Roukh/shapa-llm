# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Documents: a new stored type `D` (database schema 2, additive, applied
  lazily on connect; an older shapa keeps working on the file). `shapa doc
  add|edit|rm|list` (body from `--body`, `--file` or stdin), `shapa get D<n>`
  or by alias, and `document` as a type for MCP `save` and `shapa save
  --type`. Documents are tagged and linked like rows, pass secret
  redaction, appear in recall by title, and are never swept for staleness.
- `idea` as a type for MCP `save` and `shapa save --type`: a memory tagged
  `idea`, not a new kind.
- Delete tombstones: every row, item or document removed (the after-feature
  sweep included) leaves an `(id, kind, created, deleted_at)` record.
- `shapa remote`: an opt-in hosted store (a Cloudflare Worker you deploy,
  under `hosted/`). `login URL` registers the machine as a device (owner
  key from `$SHAPA_OWNER_KEY` or a hidden prompt, never argv), `sync`
  two-way syncs every wiki in scope (ledger items, rows, documents and the
  `arch/` and `research/` notes) by revision with last-write-wins,
  `--hook` for a session hook (8 s budget, never fails), `--if-changed`,
  `--dry-run`, `--all`, `status [--check]`, `devices [--revoke]`, `logout`
  and `instructions`. HTTPS is required (plain HTTP only for localhost),
  redirects are never followed, the server's answers are validated and
  redacted before anything is written locally, and a database swapped by git
  heals from the server instead of pushing deletes. Records are matched by
  a uid (random at creation; a derived one for records that predate it), so
  two devices that create the same label in the same second keep both.
  Batches are cut at 200 ops or 4 MB, a record over 1 MB is skipped and
  reported, and note files sync only while the checkout is on its default
  branch (a note deleted locally comes back; delete it through the API).
  A delete is sent for every tombstone until the server answers; local edits
  are never judged stale by clock unless the database was swapped (a sync
  token in `meta` tells); a record the server refuses is reported and not
  resent until it changes; and the after-merge sweep no longer drops memories
  for disuse on a wiki with sync state, since use counts are per device.
  Standard library only.

## [0.9.0] - 2026-10-08

### Added

- `shapa init`, run inside a git repo, creates `.shapa/` at the git top
  level from any subdirectory. A linked worktree initializes the primary
  checkout. Outside git, `init` stops with a message; `--no-git` creates
  `./.shapa` instead. Obsidian scaffolding is opt-in behind `--obsidian`.
  `init` installs the repo's git pre-commit and post-commit hooks and
  prints what happens next, without touching any file outside `.shapa/`.
  It never writes into a shared or global `core.hooksPath`, or into hooks
  owned by husky, lefthook or the pre-commit framework; it prints the
  lines to add instead.
- `shapa doctor` checks the global wiki, the repo wiki, git hooks, and
  every connected harness: Claude Code hooks and MCP, Codex MCP, OpenCode
  MCP. It also confirms the `shapa` command resolves, and exits 1 listing
  each problem with its fix command. `shapa status` shows the same
  report.
- `shapa commit [--hook] [--dry-run] [--json] [--scrub TERM]` commits
  `.shapa/` on the default branch only. It never pushes, touches only
  wiki paths, and leaves other staged work alone. `install.sh` wires it
  to the Claude Code `SessionEnd` hook, plus a `SessionStart` catch-up
  for anything left uncommitted from a prior session. `install.sh
  --no-auto-commit` opts out. An optional `scrub_terms` list in
  `~/.shapa/config.json` redacts matching terms before a commit.
- Trusted publishing to PyPI: tagging a release (`vX.Y.Z`) and publishing
  it on GitHub builds the sdist and wheel and uploads them with no
  stored token.
- A real README: install paths, a 60-second quickstart, how shapa works,
  a harness support matrix, and a comparison against other agent memory
  tools.
- `CONTRIBUTING.md`, `SECURITY.md`, issue templates and a pull request
  template.

### Changed

- `pyproject.toml` metadata (description, keywords, classifiers, project
  URLs) now describes what shapa does today, not the pre-format-4 tool.
- A bare `shapa init` no longer creates a visible `shapa/` in the current
  directory; an existing `shapa/` wiki at the top level is still used.

### Fixed

- `shapa ledger git-hooks` wrote its hooks into whatever `core.hooksPath`
  pointed at, including a machine-wide hooks directory shared by every
  repo. It now writes only inside the repo's own git directory.
- `shapa doctor` reported "problems: none" with no global wiki and no
  harness connected.
- The default branch is detected from branches that exist in the repo, so
  a machine-wide `init.defaultBranch` naming another branch no longer
  blocks the database on the real default branch.

## [0.8.0] - 2026-10-06

### Added

- Wiki format 4: one SQLite database per wiki (`shapa.db`), replacing the
  format-3 Markdown log. It holds the work ledger (features, jobs,
  tasks) and memory, rule and issue rows; a derived, gitignored
  `.shapa-index.db` carries the search index and vectors.
- The work ledger: `F` features (a branch and a PR), `J` jobs (one
  commit whose subject starts `J<n>:`), and `T` tasks. `shapa ledger`
  adds, claims, closes and shows a tree of ledger items.
- Git hooks for the ledger: a `pre-commit` hook refuses to commit
  `shapa.db` outside the default branch, and a `post-commit` hook closes
  the job a commit's subject names.
- A sweep after every merged feature deletes closed, expired, duplicate,
  superseded and stale rows; the database's git history keeps the record.
- Correction capture now stores only the operator's own words as issue
  rows, never a harness's restatement of them.
- A resumable `shapa upgrade` path from format 3 to format 4.

Related research and design notes live under `.shapa/arch/` and
`.shapa/research/` in this repository's own wiki.
