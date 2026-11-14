# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Focus: one northstar and exactly three ranked big fires per wiki scope
  (global and repo). `shapa focus` (show, northstar, fire, unfire, order,
  rank, candidates, promote), the MCP tool `focus`, and on the hosted store
  `GET|PUT /v1/wikis/{wiki}/focus`, `POST .../focus/promote` and the tools
  `focus` and `focus_set`. A fire points at a ledger feature or is free text
  (a fire that spans repos); a fourth is refused. Stored as one document
  (alias `focus`, tag `focus`), so it syncs like any record: no new kind and
  no schema change. Closing a fire's feature (ledger close, merge hook,
  sweep, the hosted close) frees its slot, the other fires move up, and the
  highest-ranked open top-level feature that is not a fire takes the last
  slot: a `rank:N` tag on the feature, else the oldest. `shapa focus
  promote` overrides.
- One document per project (the lean gate). A project or subproject
  (`--project KEY`, default the wiki's own) keeps one document; a second is
  refused with a message naming the first, or appended to it as a section
  with `--merge` (`merge: true` over MCP and REST). `shapa doc merge SRC DST`
  folds two that raced from two devices. The gate stops the creation of a new
  second document only: a wiki that already holds two or more for one project
  keeps and syncs them (`shapa doc list` flags the project as over the limit),
  so an upgrade refuses nothing for good. A `project:KEY` tag must be exactly
  the clean key (lowercase letters, digits, `. _ / -`): `project:Foo` and
  zero-width or full-width lookalikes are refused on both sides, including by
  `shapa row tag`. Focus records and skill versions are not project
  documents. The tags `focus` and `skill` are reserved.
- Every wiki's focus record has one fixed uid, so two devices that each set a
  northstar offline sync into the same record (last write wins) instead of the
  hosted store refusing the second. A `rank:N` tag is 1 to 999. `shapa
  bootstrap --budget` also bounds the focused session start: the northstar and
  fires are never cut, the tree is, in rank order, and what is left out is
  said.
- Skills as stored records: `shapa skill list|show|add|update|activate`, the
  MCP tool `skill`, and on the hosted store `GET /v1/wikis/{wiki}/skills[/{name}]`
  and the tools `skills` and `skill`. A version is a document tagged
  `skill`, `skill:NAME`, `v:N` and `status:active|pending|previous`, scoped
  to the global wiki or a repo's own, with a `SKILL.md` body; every version
  is kept, and activating one makes the active one `previous`.
- A memory, rule or issue that names a skill (tag `skill:NAME`, or an `about`
  link to one of its versions) files a pending new version whose body is the
  latest version plus the lesson, every lesson line quoted. It is never
  activated by itself; the hosted store does the same for REST and MCP
  writes. Activation is operator-only: the `skill` MCP tool lists, shows, adds
  and updates (always pending), and neither the Worker's REST PATCH nor its MCP
  `update` touches a skill version; `shapa skill activate` does. No edit adds
  or removes the `focus` or `skill` tag of a stored record, so the focus record
  cannot be retagged into a skill version. On the hosted store a sync put that
  activates a skill version, or edits an active one, needs a device token with
  the delete scope (OAuth tokens never): from any other device a new active
  version is stored pending and an edit of the active one is rejected. A skill
  with no active version is not served by name (a pending one is read with
  `--version N` / `?version=N`). A row files lessons for at most 3 skills, and
  a skill keeps at most 5 pending versions.
- The code map's JavaScript import pattern no longer backtracks on long runs of
  whitespace (a 400 KB file took 55 s), and a file's parse stops after 2 s.

- Code partitions per fire. `shapa map` builds a bounded, incremental code map
  of the checkout into `.shapa/.shapa-code.db` (derived, gitignored, never
  synced; `shapa upgrade` adds it to a wiki's `.gitignore`): files and the
  functions and classes defined in them, import edges and call edges resolved
  through the imports (Python by `ast`; JavaScript and TypeScript by regex, no
  node dependency), and per file a short terms document searched by FTS5/BM25
  and, with `[semantic]`, model2vec vectors. It skips hidden, vendored and
  generated folders, nested checkouts, symlinks, files over 400 KB, generated
  files, paths with control or markup characters and every credential file
  (`.env*` but the templates, `.envrc`, `.dev.vars`, `remote.token*`, keys),
  caps at 20,000 files, stops parsing after 30 seconds and resumes on the next
  run, and replaces a corrupt index. `shapa partition [F<n>|1|2|3]` prints a
  fire's partition, each file with why: named by a feature, job, task or linked
  row (path or function), recalled for the item's words (a score), or reached by
  impact (callers of a named function, importers, imports, callees, one decayed
  hop), at most 24 files and 120 KB, never a file gone from disk. Session start
  adds one `code map:` line per fire (when a map exists) inside its budget. The
  MCP tool `partition` (`map` builds). The hosted Worker stores nothing of it.

### Changed

- `shapa bootstrap` (the SessionStart hook) prints the northstar first, then
  the three fires and only their tree (the feature, its open jobs and tasks,
  and the files and arch boxes they name). The old overview, every ranked
  row and the open ledger, is `shapa bootstrap --full`. On a fixture of 12
  features, 80 jobs and 140 rows the output went from 8546 to 1763 bytes. The
  hosted `GET /v1/instructions` and `session_start` follow the same order;
  `full=1` adds the rules and the other live features (template version 2).
- `db.add_doc` and the hosted document create refuse a second document for
  the same project (see above).

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
  A repo with no git origin has no automatic wiki key: pass `--key KEY` once
  (kept in the sync state, hooks included). A command to run (`verify`) never
  leaves the machine, a remote `git_ref` is applied only when safe, harness
  instruction files (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) never sync, and
  recall output can no longer forge `shapa-*` wrapper tags.
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
