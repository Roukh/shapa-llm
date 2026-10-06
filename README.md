# shapa

**shapa** = **S**elf-**H**ealing **A**utonomous **P**ersistent **A**gent.

The operational memory of an LLM agent: a persistent markdown graph of notes about *how the agent works*, scored by how much each note matters and kept healthy by automatic maintenance. The core engine is pure Python 3.11+ standard library; the optional `[semantic]` extra (model2vec) adds vector search fused with BM25. Without it shapa runs BM25-only and says so. What the hooks capture on their own lands as small atomic records in an append-only log inside the wiki ([Captured memory](#captured-memory-format-3)).

A note's value is its effect on the consuming agent's performance. shapa is **not** a topic knowledge base; it stores operational memory only.

---

## What shapa is

A memory made of plain markdown files. An agent writes to it at the end of a work session (the "always-meta" discipline). The fetch hook (`shapa/fetch.py`, wired by `install.sh` on `UserPromptSubmit`) reads the most relevant, highest-scored notes at the start of every prompt and calls `record_use` on each surfaced note.

| Part | What it is |
|------|-----------|
| **Wiki** (external) | The memory. A directory named `shapa/` you scaffold with `shapa init` — at a **repo root** (`<repo>/shapa/`, resolved automatically for any session inside that repo) or globally (`~/.shapa/memory`). Every file — the installed `arch/` templates and your operational notes alike — has the same frontmatter and links to others with `[[wikilinks]]`, so the whole thing is one graph you can browse in Obsidian. |
| **AGENTS.md** (installed into the wiki) | The rules. The authoritative schema and operating conventions, and the marker that makes a `shapa/` directory a wiki. Shipped with the tool and installed by `shapa init`. |
| **Engine** (`shapa/`) | The maintainer. A stdlib Python package that validates frontmatter, scores notes, retrieves relevant notes (fetch), captures new ones (capture), and self-heals (maintain: prune + auto-merge duplicates). Optional embeddings via `requirements.txt`. |

There is **one coherent file format** across the whole wiki — no separate "node" format.

---

## The MemRI framework

Every file is one of:

- **memory** — a record of what happened in a session.
- **rule** — a standing policy promoted from a memory when a pattern stabilizes.
- **issue** — a detected problem or gap; open until a resolving memory or rule is written.
- **reference** — design and spec material (the `arch/` docs).

---

## How connections work

Connections are the edges of the graph. They are Obsidian `[[wikilinks]]` written in the body — the same links Obsidian renders in its graph view. Two files are connected when one links the other. A file that links to nothing and that nothing links to is an **orphan**, and the heartbeat prunes it.

---

## Scoring

Every file carries a value score driven by its frontmatter:

```
score = locus_weight × (consequence / 10) × freshness × use_factor
```

- **consequence** (1–10) — how much agent performance would degrade without this note (author-set).
- **locus** — `output` (1.0), `output-meta` (1.5), or `meta` (2.0): what the note affects.
- **freshness** — decays since `last_used`; stability grows with consequence.
- **uses** — a mechanical counter bumped by `record_use` (never by an LLM); incremented by the fetch hook on every prompt a note is surfaced.

---

## The database (format 4)

Format 4 keeps a wiki's work and memory in one SQLite file, `<wiki>/shapa.db`, tracked in git as the file itself. Context efficiency is the point: a session loads only open work and the rows that bear on it, and every merged feature cleans the database.

- **Work ledger** (repo wikis only): `F` features (a branch and a PR; close on merge) hold `J` jobs (one commit whose subject starts `J<n>:`; close on that commit) which hold `T` tasks (no git artifact; an agent closes them). IDs are a kind letter plus a per-wiki counter, never reused. `shapa ledger add|claim|close|tree|branch|issues`.
- **Rows:** `M` memories (something the operator said that matters, captured from the operator's own messages, never from agent reports), `R` rules, and `I` issues: every operator correction ("no", "wrong", "not like this") becomes an issue through the UserPromptSubmit hook (`shapa correction`), linked to the work claimed at the time. Claiming work prints its most related past issues. `shapa row add|edit|rm|link|tag|list`.
- **Triggers:** `shapa ledger git-hooks` installs a repo's git hooks: `post-commit` runs `shapa ledger on-commit` and `pre-commit` runs `shapa ledger pre-commit`; after `gh pr merge` a PostToolUse hook runs `shapa ledger hook-posttool`; SessionStart runs `shapa ledger hook-start`. A merged feature closes its children, deletes `temp/<feature>/`, and sweeps: rows open more than 30 days expire, earlier closed rows are deleted, duplicate and superseded rows are deleted, memories unused for 60 days are deleted, similar rules are flagged for judgment.
- **One file per repo:** every worktree writes the primary checkout's database, and it is committed only on the default branch (`shapa ledger pre-commit` refuses it elsewhere), so branches never fight over a binary file.
- **Why SQLite:** relations (joins, foreign keys, recursive queries, transactions), one writer plus many readers across short-lived hook processes, and standard-library only. Vectors stay in the derived index.
- **Files that remain:** `arch/` (an index of boxes and edges, one file per box), `research/` (one file per topic), and gitignored `temp/<feature>/` scrap.

## Captured memory (format 3)

The Stop/SubagentStop hook (`shapa capture`) never writes a note per session. It reads the session's final assistant message and the operator's later requests, and extracts a few atomic memories: an outcome, a fact with its file paths, each open decision, and stated preferences. The first prompt and tool output are never stored. Secrets are redacted (`shapa/redact.py`) before anything is hashed, embedded or written.

- **Source of truth:** `<wiki>/memory/YYYY-MM.jsonl`, one JSON record per line (`id, created, session, repo, scope, kind, summary <=160, body <=600, tags, source, supersedes, hash`), committed with the wiki so a fresh clone keeps every memory. Lines are never edited: an update supersedes, a retirement appends an archive op, and `memory/*.jsonl merge=union` lets two branches' appends merge cleanly.
- **Derived index:** tables in the gitignored `.shapa-index.db`, rebuilt incrementally from the log: FTS5 (unicode61), float32 model2vec vectors cached by content hash, and usage counters, which live only there.
- **Dedup and placement:** exact and near duplicates are dropped, and close variants supersede. A repo session writes its own repo's log. Only clearly global preferences go to the global wiki.
- **Recall:** `fetch`, `bootstrap` and MCP `search` rank md notes and memory records together in one fused list, keeping the per-root guarantees. The prompt hook injects one `- id: summary` line per hit inside a compact `<shapa-memory>` wrapper. The full text is one step away: `shapa get <id>`, or MCP `get`.
- **No-answer floor:** a prompt that names something memory has never seen (an acronym, a camelCase word, a capitalized name), or that has no content word memory knows ("ok"), gets `(no query-relevant memory)` instead of a padded guess, unless the semantic match is strong anyway.
- **Modes:** `fused` with the `[semantic]` extra, `bm25` without it. The mode is printed by `shapa status`, `shapa doctor`, the session header, the MCP search reply and the degraded hook header, never silently.
- **Promotion:** `shapa maintain --memories` lists hot records (high use counts). `--promote ID` turns one into a curated note and archives the record. Nothing is ever deleted.
- **Import:** `shapa upgrade [PATH] --import-memri FILE [--dry-run]` is an opt-in importer for memri JSON exports.

### Measured

The memory lab's harness measured the shipped code (0.8.0) and the previous release (0.7.1) on the same corpus: 609 curated rows from four project memory exports, plus a 56-note wiki. 0.8.0 holds them as 2,181 log records; 0.7.1 holds one md note per row. The held-out set has 82 questions, 10 of them with no answer anywhere in the corpus, and nothing was tuned on it. The no-answer floor was calibrated on a separate 110-question dev set, written by a different agent from different rows. Tokens are chars/4 of the injected block, wrapper included. Recall is over source documents.

| | recall@1 | recall@3 | recall@5 | MRR | no-answer false positives | tokens / prompt |
|---|---|---|---|---|---|---|
| 0.7.1 (full-body injection) | 0.584 | 0.668 | **0.759** | **0.744** | 10/10 | 1,103 |
| 0.8.0, floor off | 0.543 | 0.676 | 0.713 | 0.700 | 10/10 | 308 |
| **0.8.0 fused** | 0.543 | 0.676 | 0.713 | 0.700 | **2/10** | **290** |
| 0.8.0 bm25-only (no `[semantic]`) | 0.484 | 0.654 | 0.705 | 0.649 | 2/10 | 288 |

Latency on the same corpus, all three timed back to back (20 runs each, on a shared machine at load average 8-14, so the absolute figures are noisy):

| | warm fetch p50 / p95 | hook wall p50 / p95 | peak RSS | disk per 100 memories |
|---|---|---|---|---|
| 0.7.1 | 379 / 857 ms | 1,865 / 5,065 ms | 153 MB | 751 KB (notes + caches) |
| 0.8.0 fused | 160 / 260 ms | 1,380 / 2,627 ms | 145 MB | 64 KB log + 262 KB index |
| 0.8.0 bm25-only | 105 / 173 ms | 238 / 530 ms | 30 MB | same |

The fused hook's wall time is mostly the model2vec load, which the optional `shapa serve` daemon pays once. The capture hook, run fresh on a 1.2 MB transcript into a 2.2k-record log, took p50 231 / p95 323 ms (an incremental Stop took 160 / 217 ms); a 50 MB transcript takes 0.5 s.

The trade is honest: 0.8.0 injects 74% fewer tokens and stops answering most off-topic prompts, and it trails 0.7.1 by 4.6 points of recall@5. The loss is on paraphrased and multi-document questions; exact-match recall goes from 0.95 to 1.00. The floor itself costs no recall: no answerable question was rejected. The two no-answer questions it still answers ask about a known project, using only lowercase words for something it lacks. Method, the lab's 32-variant storage grid, and the floor's calibration are in `.shapa/arch/shapa-memory-v3-results.md`.

---

## Install

One line — installs the tool and wires the Claude Code hooks (no clone):

```
curl -fsSL https://raw.githubusercontent.com/Roukh/shapa-llm/main/bootstrap.sh | sh
```

It installs `shapa` (pipx > uv > pip --user) and wires the bootstrap/fetch/
capture/maintain hooks, and scaffolds the default global wiki. When stdin is a
TTY it asks whether to add semantic search and/or MCP server support; piped
(`curl | sh`, no TTY) it installs the core (BM25-only) and prints the exact
follow-up command instead of guessing. Skip the prompt outright:
`sh -s -- --with-semantic` (or `--with-mcp` / `--full`). `SHAPA_NO_HOOKS=1`
installs the tool only, no hooks. Or install by hand:

### MCP server registration (Codex, OpenCode, and Claude Code's own MCP surface)

Installing the `[mcp]` extra makes `shapa mcp` runnable; *registering* it
with a harness so that harness actually starts it is a separate step, wired
by `install.sh --mcp --harness claude|codex|opencode|all` (default harness:
`claude`; `--no-mcp` skips it explicitly). Each harness is only ever touched
if its binary is on `PATH`:

| Harness | Surface | How |
|---|---|---|
| Claude Code | user-scope config | `claude mcp add shapa --scope user -- shapa mcp` (idempotent; `claude mcp remove` on `--uninstall`) |
| Codex | `~/.codex/config.toml` | a marked `[mcp_servers.shapa]` block, inserted/removed in place |
| OpenCode | `opencode.json`'s `"mcp"` key | `{"mcp": {"shapa": {"type": "local", "command": [...]}}}`, merged with `jq` |

With neither flag, `install.sh` asks on a TTY and otherwise skips wiring and
prints the exact command to run later; `--dry-run` always previews the plan.
`bootstrap.sh` forwards `--mcp`/`--no-mcp`/`--harness` to `install.sh`, and
answering yes to its own "install MCP server support?" prompt also wires it
by default (add `--no-mcp` after to install the extra without wiring it).

An existing wiki's `AGENTS.md`/`placement.md` (the schema/placement docs,
never `arch/` templates or a note) can be refreshed to the version bundled
with the installed `shapa` without touching anything else: `shapa init
--upgrade-docs [DIR]` (DIR defaults to the wiki already in scope).

### Upgrades: every wiki stays on the current format

Each wiki records its format in a tracked `.shapa-format` file. A wiki
without one predates the marker and counts as format 1. shapa also keeps a
registry of every wiki it has seen in `~/.shapa/wikis.json`. `shapa
upgrade` applies the mechanical migrations itself: schema docs, the cache
`.gitignore`, counters moved into the index, and derived `id`/`scope`. It
also prints the judgment items an agent must resolve, such as lean-shape
caps, duplicates, missing summaries and over-length notes. `install.sh` and
`bootstrap.sh` finish with `shapa upgrade --all --check`, and the session
start flags a wiki that is behind. They also install the `shapa-upgrade`
skill for Claude Code, Codex and OpenCode, which gives one agent per wiki
that is behind.

Format 3 adds the captured-memory log. `shapa upgrade` converts each
`memory-session-*.md` note into one log record, carries its use count over,
archives the md file (never deletes it), and marks the log `merge=union` in
`.gitattributes`. A malformed log line is listed as a judgment item.

```
pipx install shapa                # the tool (retrieval via BM25)
pipx install "shapa[semantic]"    # + local embeddings (model2vec), fused with BM25
```

### Where memory lives

Your **memory is external to the tool** — never inside the installed package or
this repo. A wiki is a directory named `shapa/` holding an `AGENTS.md` marker.
Two ways to make one:

```
cd <your-repo> && shapa init       # a repo-root wiki: <your-repo>/shapa/
shapa init --global ~/.shapa/memory  # the global wiki (LLM rules, any session)
```

`shapa init [DIR]` creates the directory, installs `AGENTS.md` + the `arch/`
project templates, scaffolds an Obsidian vault, and registers the wiki. Only
`--global` records the path as the global wiki; plain `shapa init DIR` never
touches that pointer. A named DIR that already holds notes but no `AGENTS.md`
is adopted: the missing scaffold files are added and the notes are left as
they are, for `shapa upgrade DIR` to migrate. A session
running **inside a repo that has a `shapa/` wiki uses it automatically** — the
hooks walk up from the cwd to the nearest `shapa/AGENTS.md` (like git finding
`.git`) — unless `$SHAPA_MEMORY` is set, which always takes precedence.
Outside any such repo, resolution falls back to the recorded path or
`~/.shapa/memory`. Open any wiki folder as an Obsidian vault to browse the
graph.

To wire the hooks into Claude Code from a clone instead of the one-liner, run
`./install.sh` (wires the hooks, installs the docs, registers the Obsidian
vault). Contributors can `git clone` and work from the repo.

## How to run

```
shapa init [DIR] [--global]            # scaffold/adopt a wiki (default: ./shapa)
shapa where                            # print the resolved memory directory
shapa fetch --query "fix the git flow" # surface relevant memory (read path)
shapa get m-1a2b3c4d5e                 # the full text of a memory record or note
shapa status                           # wikis, memory counts, and the recall mode (fused/bm25)
shapa doctor                           # the same, exiting 1 when something needs fixing
shapa heartbeat --dry-run              # preview orphan pruning
shapa maintain --dry-run               # preview merges/prunes (nothing changes)
shapa maintain --prune                 # prune orphans/stale + auto-merge duplicates
shapa maintain --memories              # hot memory records: candidates for a curated note
shapa maintain --promote ID            # promote one into a note, archive the record
shapa upgrade --all --check            # which known wikis are behind the current format
shapa upgrade PATH                     # apply mechanical migrations, list the judgment work
shapa upgrade PATH --import-memri FILE # opt-in: import a memri JSON export as memory records
shapa maintain --resolve               # LLM-reconcile contradictions (claude CLI)
shapa score                            # rank notes by value
shapa validate                         # validate every note's frontmatter
```

Every command resolves the wiki the same way (local `shapa/` → `$SHAPA_MEMORY` → recorded path → `~/.shapa/memory`); pass a directory to override. From a clone without installing, use `python3 -m shapa <command>`.

## Repo layout (the tool)

```
shapa-llm/
  pyproject.toml         ← packaging (pipx/PyPI); `shapa` console command
  bootstrap.sh           ← curl|sh installer (installs the tool + wires hooks)
  install.sh             ← wires the Claude Code hooks + Obsidian vault (from a clone)
  shapa/                 ← Python engine (core is stdlib; embeddings optional)
    config.py            ← wiki resolution (local shapa/ · $SHAPA_MEMORY · pointer · default)
    cli.py               ← the unified `shapa` command (incl. `init`)
    frontmatter.py · nodes.py · heartbeat.py · validate.py · score.py
    bootstrap.py · fetch.py · capture.py · maintain.py · embed.py
    memlog.py · redact.py  ← the format-3 memory log, its derived index, secret redaction
    get.py · status.py · memri_import.py  ← `shapa get`, `shapa status`/`doctor`, the memri importer
    registry.py · upgrade.py  ← format marker, wiki registry, `shapa upgrade`
    assets/              ← docs installed into every wiki by `shapa init`
      AGENTS.md          ← the schema and rules (also the wiki marker)
      arch/              ← generic project templates (PRD, architecture, system-design)
      skills/            ← harness skills install.sh installs (shapa-upgrade), never into a wiki
  tests/                 ← regression suite
```

The repo contains **no user memory** — your notes live in the external `shapa/` wiki you scaffold with `shapa init`, so private notes are never inside this repo at all. There is nothing to gitignore and no commit guard to maintain: the tool ships only the engine, the `AGENTS.md` rules, and the generic `arch/` templates under `shapa/assets/`.

> **Note on `.shapa/` / `shapa/` at this repo's own root:** this repo is shapa's own source, not a shapa wiki. `shapa/config.discover()` treats a directory named `.shapa/` or `shapa/` containing an `AGENTS.md` file as a wiki marker (see [Where memory lives](#where-memory-lives)). The `shapa/` directory here is the Python package, not a wiki — it has no `AGENTS.md` at its root (only nested under `assets/`), so it's never mistaken for one. For this reason the tool's own repo deliberately does **not** carry a `.shapa/` documentation folder: doing so would create exactly the marker `discover()` looks for and would make every `shapa` session run from within this repo silently treat the repo as its own connected memory wiki.

---

## File format (quick reference)

```markdown
---
id: kebab-case-slug          # must equal the filename stem
type: memory | rule | issue | reference
created: "2026-06-30T12:00:00Z"
consequence: 7               # 1-10
locus: output | output-meta | meta
uses: 0
---
Free markdown. Headings, lists, emphasis are fine. Connect to other notes
with [[wikilinks]] in the body, e.g. its type [[rule]] and a peer [[memory-hygiene]].
```

See `AGENTS.md` (installed into your wiki by `shapa init`, source at `shapa/assets/AGENTS.md`) for the full schema.

---

## Architecture reference

### Heartbeat (self-healing)

One heartbeat cycle (`shapa/heartbeat.py`) runs two phases over the wikilink graph built by `shapa/nodes.py`:

1. **Random walk (the pulse)** — a seedable walk (`random.Random(seed)`) starting at a random file, hopping to a random `[[wikilink]]` neighbour at each step, stopping at a dead end or after `max_steps` files. All collections are sorted before any random choice, so a given seed reproduces the same walk regardless of filesystem/dict ordering.
2. **Orphan scan + prune** — independent of the walk (the walk is only a sample), every operational note at the wiki root is checked; a file with no `[[wikilink]]` to or from it is an orphan and its file is deleted (or reported, in `--dry-run`). Files under `arch/` (`type: reference`) are curated and exempt from pruning.

### Hooks wired by `install.sh`

| Event | Command | Purpose |
|-------|---------|---------|
| `SessionStart` | `shapa bootstrap` | Read path — once per session, a metadata-only overview (id/type/summary, never bodies) of every wiki in scope, within a small token budget. |
| `UserPromptSubmit` | `shapa fetch` | Read path — surfaces the most relevant, highest-scored notes at the start of each prompt; calls `record_use` on each. |
| `Stop` | `shapa capture` | Write path — distils the finished session into a few atomic, redacted memory records appended to the wiki's `memory/` log (heuristic, stdlib-only; an LLM distiller is opt-in). |
| `Stop` | `shapa maintain --prune` | Prunes orphans/stale notes and auto-merges near-duplicates. |
| `SubagentStop` | `shapa capture` | Same command, but skipped by default: a subagent's report is an intermediate work product for the parent agent, not a session's own job report. Opt in with `--capture-subagents`/`SHAPA_CAPTURE_SUBAGENTS=1`. |

Hooks receive a JSON payload on stdin (`transcript_path`, `session_id`, `cwd`, plus `agent_id`/`agent_type` on `SubagentStop`). `capture` and `maintain` are write-only, non-blocking, and always exit 0. Registration is idempotent and deep-merged into `settings.json` (existing hooks from other tools are preserved, never overwritten).

**Privacy is structural, not a convention:** hooks resolve their target via `config.resolve()`/`config.discover()` and write only to the connected wiki — never inside the shapa repo or the installed package. The wiki lives outside any git working tree by construction, so captured notes are never git-tracked.

### Verified guarantees

| Guarantee | Verified by |
|---|---|
| The heartbeat prunes a planted orphan while retaining all linked files. | `tests/test_heartbeat.py::test_prune_removes_only_orphan` |
| A file with no `[[wikilink]]` in or out is the only note flagged as an orphan. | `tests/test_heartbeat.py::test_only_zero_link_note_is_orphan` |
| The validator enforces the frontmatter schema (type enum, consequence 1-10, valid locus, non-negative uses). | `tests/test_validate.py::test_bad_type`, `::test_bad_consequence_and_locus` |
| Scoring ranks by `locus_weight × (consequence/10) × freshness × use_factor`; `record_use` increments the mechanical counter. | `tests/test_score.py::test_meta_high_outranks_output_low`, `::test_record_use_increments_and_boosts` |
| The validator rejects an `id` that doesn't match the filename stem. | `tests/test_validate.py::test_id_must_match_stem` |
| Wikilink edges are undirected — a link from A to B connects both. | `tests/test_heartbeat.py::test_graph_edges_are_undirected` |

## What is deferred

- **Contradiction resolution.** `maintain` *detects* similar/contradiction-candidate pairs; *reconciling* them (merge a duplicate, resolve a conflict) is a semantic judgement left to the maintaining agent.

---

## Standalone

shapa is a standalone, self-contained repo. The engine has zero external dependencies; your memory lives in an external wiki directory as plain markdown (connect it with `shapa init`). Clone the tool, point it at your own memory, and run.
