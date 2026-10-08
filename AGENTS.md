# shapa-llm

Source of the `shapa` tool: an operational-memory engine for LLM agents (one SQLite database per wiki holding the work ledger and memory/rule/issue rows, ranked recall, self-healing maintenance). This file is for agents working on the tool itself. The note schema a wiki follows is `shapa/assets/AGENTS.md`, which `shapa init` installs into every wiki; this repo's own wiki is `.shapa/`.

## Navigation

- `shapa/` is the engine package; `cli.py` is the `shapa` command (`python3 -m shapa` from a clone).
  - Wiki resolution and discovery: `config.py`, `registry.py` (`~/.shapa/wikis.json`).
  - Read path: `bootstrap.py` (SessionStart), `fetch.py`, `rank.py`, `bm25.py`, `embed.py`, `get.py`.
  - Database (format 4): `db.py` (schema, work ledger, rows, sweep, worktree resolution), `ledger.py` (`shapa ledger`/`row`/`correction` and the git and harness triggers), `migrate4.py` (format 3 to 4), `commit.py` (`shapa commit`: the wiki commits itself on the default branch, as a SessionEnd/SessionStart hook).
  - Write path: `capture.py` (Stop hook), `redact.py`, `memlog.py` (format-3 log, and the derived index that also mirrors database rows), `save.py`.
  - Index and daemon: `store.py`, `serve.py`. MCP server: `mcp.py`.
  - Maintenance: `maintain.py`, `heartbeat.py`, `score.py`. Format: `frontmatter.py`, `nodes.py`, `validate.py`, `upgrade.py`, `status.py`, `reconfigure.py` (the session-start restructure directive; its prompt is `assets/prompts/reconfigure.md`). Opt-in importer: `memri_import.py`.
- `shapa/assets/` ships into user wikis: `AGENTS.md` (schema and wiki marker), `placement.md`, the `arch/index.md` template, and `skills/shapa-upgrade` (installed into harnesses, never into a wiki).
- `tests/` is the pytest suite, with fixtures under `tests/fixtures/`.
- `install.sh` wires hooks, MCP and the skill from a clone. `bootstrap.sh` is the `curl | sh` installer.
- `.shapa/` is this repo's wiki: `shapa.db` holds its work ledger and rows (`shapa ledger`, `shapa row list`), `arch/` the design docs.
- `.github/workflows/test.yml` is CI.

## Owner

Roukh (github.com/Roukh/shapa-llm), sole maintainer, MIT license. Commits use the Roukh noreply identity, and pushes need the `Roukh` gh account active (`gh auth status`). Work is tracked in this repo's ledger (`shapa ledger`).

## Commands

- Tests: `.venv/bin/python -m pytest -q`. In a network-sandboxed shell, `tests/test_serve.py` (Unix sockets) and `tests/test_installer_upgrade.py` fail; run those outside the sandbox before calling the suite green.
- CI runs `python -m pytest -q` on Python 3.11, 3.12 and 3.13, once on the bare core install and once with `[semantic,mcp]`.
- Install the live CLI from this checkout: `uv tool install --force '.[semantic,mcp]'` from the repo root, then `shapa upgrade --all --check`.
- Health: `shapa doctor` (exits 1 when a wiki needs a hand), `shapa status`, `shapa validate`.
- Before checking a wheel's contents, delete `build/`; a stale one re-ships files removed from `shapa/assets/`.

## Boundaries

- The repo is public. Everything tracked, `.shapa/` included, is published on push. Keep secrets, client names, other projects' details and private wiki content out of code, tests, fixtures, notes and commit messages.
- The core engine stays pure standard library (`dependencies = []` in `pyproject.toml`). A new dependency goes behind an optional extra, and the core path must still work without it.
- The engine has no hosted-database or hosted-service coupling; its one database is the local `shapa.db` file. Integrations with other systems live outside this repo.
- Never run `shapa maintain --prune` or a non-dry-run heartbeat against a live wiki from an agent session; use `--dry-run`. Prune is operator-run only.
- Destructive paths never trust frontmatter an agent wrote: `arch/` is protected by path, and superseded note files move to `archive/`. Database rows are different by design: the after-feature sweep deletes closed, expired, duplicate, superseded and stale rows, and the database file's git history is the record.
- A wiki database is committed only on the default branch - `shapa commit` does this itself at session end (SessionEnd hook, with SessionStart as a catch-up for a crashed session), scrubbing any configured terms first; `shapa ledger pre-commit` still refuses a manual commit of it elsewhere. Worktrees write and commit through the primary checkout's file.
- Only `shapa init --global` may write the global wiki pointer (`~/.shapa/config.json`).
- `capture` and `maintain` run as hooks: they never block a session (always exit 0) and never write inside this repo or the installed package.
- Tests run against temporary wikis. `tests/conftest.py` points `$SHAPA_REGISTRY` at a throwaway file; never aim the suite at a real wiki.
- A headless `claude -p` call from the engine passes empty hooks (`--settings '{"hooks":{}}'`) and closed stdin.

## Dependencies

- Runtime: Python 3.11 or newer, standard library only.
- Optional extras: `[semantic]` adds `model2vec>=0.3.0` (the potion-base-8M model, loaded from the local cache with no hub round-trip) for fused vector plus BM25 recall; `[mcp]` adds `mcp>=1.0.0`, and `shapa mcp` falls back to a stdlib JSON-RPC shim without it. `[embeddings]` is a deprecated alias of `[semantic]`.
- Dev: `pytest`, in the repo's `.venv`.
- Touched by the installers: Claude Code `settings.json`, `~/.codex/config.toml`, `opencode.json` (merged with `jq`), and Obsidian when present. `maintain --resolve` shells out to the `claude` CLI.
- State outside the repo: `~/.shapa/config.json` (global pointer), `~/.shapa/wikis.json` (registry), `$SHAPA_MEMORY` (override).

## Contracts

Current:
- A wiki is a `.shapa/` or `shapa/` directory holding `AGENTS.md`, found by walking up from the cwd (`.shapa/` first). `config.memory_dir()` resolves `$SHAPA_MEMORY`, then the discovered wiki, then the pointer, then `~/.shapa/memory`.
- Reads fan out over the global and repo wikis in scope; writes are scope-gated (`save --scope global|repo|external`).
- Wiki format 4: `shapa.db` (tracked, committed whole) is the source of truth for the work ledger (features, jobs, tasks) and memory/rule/issue rows. `.shapa-index.db` (FTS5, vectors, use counters) is derived and gitignored and mirrors the rows for recall. `arch/` and `research/` stay files; `temp/<feature>/` is gitignored scrap. `.shapa-format` marks a wiki's format, and `shapa upgrade` migrates older wikis.
- Recall mode is `fused` with `[semantic]` and `bm25` without it, and every surface reports which.

Historical (do not resurrect): the format-3 `memory/*.jsonl` log, `checklist.md`, `agenda.md` and `ideas.md` as wiki files (replaced by the database in format 4), memory kept only outside the repo (v0.6), the `uses` frontmatter counter, `sentence-transformers` embeddings, the single repo-root wiki with no global/repo split (2026-07-10), one md note per captured session (before 0.8.0), and a `shapa search` command (0.8.0 has `fetch`). Superseded specs live in git history.
