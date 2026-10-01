---
id: AGENTS
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 8
locus: output
---

# AGENTS.md — shapa Schema and Operating Rules

> This file is the authoritative schema for the shapa memory graph.
> It governs every file the agent writes. It is a rules document, not a node,
> so it may use headings, tables, and formatting freely.

---

## 1. What shapa is

shapa = **S**elf-**H**ealing **A**utonomous **P**ersistent **A**gent.

It is a persistent markdown-graph memory. The graph lives in an external wiki — a directory named `shapa/` holding this `AGENTS.md` marker. `shapa init` scaffolds one at a repo root (`<repo>/shapa/`, resolved automatically for any session run inside that repo, discovered by walking up from the cwd) or globally (`$SHAPA_MEMORY`, the path recorded by `shapa init`, or the default `~/.shapa/memory`) — never inside the tool's own repo. Project arch templates (`type: reference`) are installed into the wiki's `arch/` subdirectory by `shapa init` for agents to fill in; operational notes (`memory`, `rule`, `issue`) are written directly to the wiki root. Every file is a note-to-self written by an agent at the end of a work session: an instruction on operation, a correction to prior understanding, or an issue flagging a detected problem. The fetch hook (`shapa/fetch.py`, wired by `install.sh` on `UserPromptSubmit`) reads the most relevant notes at the start of each prompt and calls `record_use` on each surfaced note. Writing to the graph at session end is the "always-meta" discipline: every substantive piece of work ends with the agent reflecting on what just happened and committing at least one note to the graph. A topic note is just a file whose `id` matches the topic slug; a phantom `[[wikilink]]` referencing it lights up automatically once that file is added.

The graph is maintained by a lightweight Python engine (`shapa/`) that uses only the Python 3.11+ standard library. Zero external dependencies.

---

## 2. The MemRI framework

Every node belongs to exactly one of three types:

| Type | Symbol | Meaning |
|------|--------|---------|
| `memory` | M | A record of an interaction or session. Every session yields at least one memory node. |
| `rule` | R | A standing rule of operation, promoted from a memory when a pattern becomes stable policy. |
| `issue` | I | A detected problem, contradiction, or gap. Closed when a resolving memory or rule is created. |
| `reference` | D | A design or specification document (e.g. PRD, spec, architecture notes). Lives in the `arch/` subdirectory of the connected wiki, installed by `shapa init`; never pruned or auto-merged by the maintainer. |

These types are mutually exclusive. The `type` field in frontmatter is the canonical label.

---

## 3. Node frontmatter schema

Every node file is a markdown file with a YAML frontmatter block at the top. The schema is strict and versioned here.

```yaml
---
id: kebab-case-slug          # unique; must match the filename stem exactly
type: memory | rule | issue | reference  # exactly one of these four values
created: "2026-06-30T12:00:00Z"  # ISO-8601 timestamp string, quoted
consequence: 7               # 1-10: performance loss if this node were absent (author-set)
locus: output-meta           # output | output-meta | meta (what the node affects)
---
```

`uses`/`last_used` are NOT part of this schema any more: the mechanical read
counter lives entirely in the wiki's index store (`shapa.store`, keyed by
root + note id), never in the file — a note is only ever rewritten when its
own content changes (`shapa-backend-spec.md` §10 decision 7, "reads never
write notes"). A note captured before this still carries `uses`/`last_used`
lines; they are tolerated legacy (S03 only fires on a present-but-malformed
value, never on their absence) and `shapa maintain --backfill` strips them.

### Frontmatter rules

- `id` must be a kebab-case slug (lowercase letters, digits, hyphens only).
- `id` must match the filename stem exactly: `memory-genesis.md` → `id: memory-genesis`. This is error **F01**.
- `type` must be one of the four values (`memory`, `rule`, `issue`, `reference`). Any other value is error **F02**.
- `created` must be a valid ISO-8601 datetime string. Quote it in YAML. Absence is error **F03**.
- `consequence` and `locus` are required (validated as S01, S02 respectively — see the Scoring fields subsection below).

### Scoring fields (optional; validated when present)

A node's value to the consuming agent is scored from four fields (see §6.5; full rationale in the shapa tool's `docs/design/memri-spec.md`):

- `consequence` (1-10) — how much the agent's performance would degrade without this node. Set by the author at capture time; the engine does not compute it. Invalid range is error **S01**.
- `locus` — `output` (changes the agent's answer), `output-meta` (changes how the agent works), or `meta` (changes the agent's self-governance). Drives a weight and is intended to drive retrieval policy. Invalid value is error **S02**.
- `uses` — a non-negative integer incremented by `record_use`, which lives in the index store (`shapa.store`), never in this file, when the node is read or used. Never set by a language model. A legacy in-file value is only ever read as a fallback for a note the store hasn't indexed; present-but-invalid is error **S03**.
- `last_used` — bumped automatically alongside `uses`, in the same index store; drives freshness decay.

Score = `locus_weight × (consequence / 10) × freshness × use_factor`, where freshness decays since `last_used` with a stability that grows with consequence, and `use_factor` rises with `uses`. Run `shapa score` (or `python3 -m shapa.score $SHAPA_MEMORY`) to rank notes.

### 3.1 Progressive-disclosure fields (required for memory/rule/issue; recommended for reference)

- `summary` — one line, <=160 characters, no embedded newline. This is the
  ONLY thing a session-start bootstrap loads for a note; write it so it is
  useful on its own, the way a Claude Code skill's `description` is. Missing
  or oversized is warning F04 (error once backfilled).
- `scope` — `global` (true in any repo) or `repo` (true only in this wiki's
  own repo). Must match physical location (F06, auto-fixable). Placement is
  an agent decision, not automatic — see `placement.md`.
- `applies_to` — repo name (or list); used only as a capture-time routing
  flag (`--scope external --applies-to <repo>`) that files the note into
  that other repo's own wiki. The note's own stored `scope` is then `repo`
  (physically in that repo) — `external` is not a value `scope` itself
  takes; see decision 4/6 in the shapa-backend spec.
- `tags` — optional kebab-case list for keyword boosting.
- `status` — `active` (default), `superseded`, or `draft` (S04).
- `supersedes` — optional id of a note this one replaces; dangling ref is F08.

---

## 4. Node body

The body is everything after the closing `---`. Format is the maintaining
LLM's discretion, but SIZE is not free:

- `memory` / `rule` / `issue`: target 150-300 words. A note needing more
  splits into two linked notes rather than growing one file — this keeps
  every note inside the range fetch/embed retrieval performs best at
  (roughly 100-400 tokens; matches this engine's own SNIPPET_CHARS=500 /
  DEFAULT_BUDGET=4000 constants). Past 300 words: warning F07.
- `reference` (installed into `arch/`): may run long, but MUST open with a
  1-2 sentence abstract immediately below the frontmatter, and MUST use `##`
  headings past ~500 words. Past 2000 words: F07 (split into
  `arch/<topic>-<n>.md` + an index note).

**Connections to other files appear as Obsidian `[[wikilinks]]` in the
body.** This is the only connection mechanism; it is what Obsidian renders in
its graph view and what `nodes.py` uses to build the link graph for the
heartbeat.

Interlinked operational notes live at the root of the connected wiki; they connect to each other and to type/topic nodes directly (no central index), so clusters form organically. Design docs live in the wiki's `arch/` subdirectory and are a separate cluster. Both use the same uniform frontmatter schema.

---

## 5. Connection model ([[wikilinks]])

Connections are the edges of the memory graph. They are **Obsidian `[[wikilinks]]` written in the body** — this is the only connection mechanism. There is no `connections` frontmatter field and no noun-keyword system.

- **A connection is a `[[wikilink]]` in the body** referencing another wiki file by its `id` (the filename stem). The link may appear anywhere in the body — in prose, in a list, in a table.
- **Two files are connected** when at least one of them contains a `[[wikilink]]` referencing the other. (Undirected: `A → B` connects both A and B for graph purposes.)
- **An orphan** is a file with no wikilink pointing to it and no wikilink pointing from it to any other file.
- `nodes.py` builds the link graph by parsing `[[wikilinks]]` from every file body; the graph is the sole input to `heartbeat.py`.
- Orphan pruning runs only on operational notes at the wiki root. Files in `arch/` are design docs (`type: reference`) and are never pruned.

To avoid immediate orphan status, link a new note to its type (`[[memory]]`/`[[rule]]`/`[[issue]]`) and to a related topic or peer note.

---

## 6. The heartbeat

The heartbeat is the engine's maintenance process. It runs on demand or on a cadence.

1. It walks a **random string of connected files** (the pulse), starting from a random seed file and following `[[wikilink]]` edges built by `nodes.py`.
2. After the walk, it scans every operational note at the wiki root (excluding the `arch/` cluster) for orphan status (the walk is a sample; the orphan scan is whole-directory).
3. **Orphans are pruned**: any file with no wikilink edge to or from any other file is deleted.
4. Files that share at least one wikilink edge with any other file are **never pruned**, regardless of other properties.

Command interface:

```
shapa heartbeat --dry-run   # pulse + show what would prune, no writes
shapa heartbeat             # pulse + prune orphans
```
(Both default to the connected wiki: `$SHAPA_MEMORY` / `~/.shapa/memory`.)

---

## 7. The validator

The validator checks a single file for frontmatter schema compliance. It does not check the body.

```
python3 -m shapa.validate <file>.md    # exits 0 if valid, non-zero if not
```

It checks:

- **F01** — `id` matches the filename stem exactly.
- **F02** — `type` is one of the four valid values (`memory`, `rule`, `issue`, `reference`).
- **F03** — `created` is present (and a valid ISO-8601 string).
- **S01** — `consequence` is an integer in 1–10.
- **S02** — `locus` is one of `output`, `output-meta`, or `meta`.
- **S03** — `uses`, when present, is a non-negative integer (absence is fine — legacy field, see §3).

Schema v2 (§3.1) adds warnings-only checks for one release, promoted to
errors after a one-time backfill — a note missing these never fails
validation today:

- **F04** — `summary` present, <=160 chars, single line.
- **F05** — `scope` present, one of `global`/`repo`.
- **F06** — `scope` matches the file's physical bucket (global vs. repo).
- **F07** — body past the type's word ceiling (300 memory/rule/issue, 2000 reference).
- **F08** — `supersedes` names an id that does not exist.
- **F09** — duplicate `id` across two roots in one `wiki_roots()` result. **Error**, not a warning — checked only by `--all-roots`. Ids every wiki carries by construction are exempt: the per-wiki convention files (`AGENTS`, `placement`, `agenda`, `ideas`) and the `arch/` templates (`PRD`, `architecture`, `system-design`). That recurrence is not the ambiguity this rule exists to catch, and `fetch` never annotates those ids as ambiguous either.
- **F10** — lean wiki shape (§11.1): too many live root notes (>40), too many `arch/` reference docs (>12), or too much live disk footprint (>250 KB, excluding `archive/`/`attic/`). **Error**.
- **F11** — `agenda.md` missing, or listing more than 3 top-level items. **Error**.
- **S04** — `status` is one of `active`/`superseded`/`draft`.

```
python3 -m shapa.validate --all-roots [START]   # also checks F09/F10/F11 across every wiki_roots() root
```

---

## 8. Capture workflow ("always meta")

Node capture happens at the **end** of any agent's work. The discipline:

1. After completing any substantive task, the agent reflects on what just happened.
2. It creates at least one `memory` note describing the session.
3. If the session produced a stable policy, it additionally creates a `rule` note.
4. If the session revealed a problem or gap, it additionally creates an `issue` note.
5. All new notes are written to the wiki root (`$SHAPA_MEMORY/<id>.md`) with filenames matching their `id` field.
6. Each note includes at least one `[[wikilink]]` to an existing file to avoid immediate orphan status.

The fetch hook (`shapa/fetch.py`, registered on `UserPromptSubmit` by `install.sh`) reads the wiki root at the start of each prompt, surfaces the most relevant and highest-scored notes, and calls `record_use` on each. The automated capture hook (Stop/SubagentStop) that would write notes without manual action is deferred — see §9.

---

## 9. What is deferred

- **LLM-distilled capture** — a heuristic capture hook (`shapa/capture.py`) is built and wired on Stop/SubagentStop; the richer LLM-salience version (see the shapa tool's `docs/design/hook-design.md`) is deferred.
- **Heartbeat scheduling** — maintenance runs on the Stop hook and on demand; a standalone cadence trigger is deferred.

**Implemented but not yet run on a live config:** `install.sh` finds the Claude config and wires the fetch hook (UserPromptSubmit) plus capture + `maintain --prune` (Stop) against the connected wiki. Verified in a sandbox but not yet applied to a real `~/.claude/` installation.

These are tracked in the shapa tool's `docs/design/MACRO.md`.

---

## 10. File layout within the wiki

The wiki is an external directory named `shapa/` (scaffolded by `shapa init` —
a repo root's `shapa/`, `$SHAPA_MEMORY`, or `~/.shapa/memory`), never inside the
shapa tool's own repo:

```
<wiki_root>/          (a `shapa/` dir — repo-root or global; never inside the tool repo)
  AGENTS.md           ← this file, installed by `shapa init`; also the wiki marker
  .shapa-format       ← the wiki format this wiki is current at (tracked; see §11.2)
  .gitignore          ← keeps the index/vector caches out of git
  agenda.md           ← the top 3 fires (§11)
  arch/               ← project templates (type: reference), installed by `shapa init`
    PRD.md  architecture.md  system-design.md   ← fill these in for your project
  <id>.md             ← operational notes captured at the wiki root;
                        each links to its [[type]] + related topics/peers
```

There is no central index. Notes connect peer-to-peer and to type/topic
nodes, so clusters form organically as topics recur. The `arch/` docs are a
separate cluster and are not wired into the memory graph.

### 10.1 Reading spans more than one wiki

A session's memory is not one directory. `shapa bootstrap`/`fetch`/`search`
resolve and search ALL of: the global wiki, this repo's own wiki (if
distinct), and this repo's `external/<repo>/` bucket (if it has no wiki of
its own). Writing is narrower and agent-gated: a new note declares exactly
one `scope` and lands in exactly one place — see `placement.md`. Never
assume a single resolved directory is the whole picture when reading.

## 11. Lean wiki shape

Every wiki, global or repo, keeps the same lean shape — checked by
`validate.py`'s **F10**/**F11** and reported by `shapa maintain --lean`:

- `agenda.md` is required at the wiki root. It lists the top 3 fires only
  — no more (**F11**). `ideas.md` is optional: an append-only dated log
  with a status per entry, for everything that isn't one of the top 3.
- Live notes at the root (`memory`/`rule`/`issue`): at most 40. Reference
  docs in `arch/`: at most 12. Live wiki total (note content, not derived
  index files): at most 250 KB. All three are **F10**; the limits are
  config values (defaults 40/12/250 — see `shapa.validate`).
- No duplicates: one note per subject. `shapa maintain` auto-merges
  near-duplicates and never keeps two live files on the same topic.
- A note that's done being live gets `status: superseded` and moves to
  `archive/` — by hand, or via `shapa maintain --lean --apply` (git-aware:
  `git mv` when this is a git checkout, else a plain move; **never
  deletes**).

### 11.1 `archive/` and `attic/` are never loaded

Two directories at the wiki root are invisible to every reader in this
engine — `nodes.load_nodes`, `fetch`, `bootstrap`, the sqlite index
(`store.py`), and the MCP `search`/`get` tools all skip them entirely, at
any depth, the same way `.obsidian/` (Obsidian's own config folder) is
skipped:

- **`archive/`** — notes that were live and are now `status: superseded`.
- **`attic/`** — scratch material waiting on an operator decision.

Both hold history; neither counts toward the F10 limits, and nothing under
either is ever pruned, merged, fetched, or found by search. A note doesn't
leave the wiki by being deleted — it leaves the *live* wiki by moving into
one of these two directories.

### 11.2 Format upgrades

`.shapa-format` holds one integer: the wiki format this wiki was last brought
fully current at. A wiki without it predates the marker (format 1). When a
shapa update raises the format, the session start says so in one line, and
`shapa upgrade` brings the wiki current. It applies the mechanical migrations
itself: this file, `placement.md`, the cache `.gitignore`, legacy counters
moved into the index, and derived `id`/`scope`. It then lists the judgment
items, such as caps, duplicates, missing summaries and over-length notes,
which the `shapa-upgrade` skill resolves. `shapa upgrade --check` changes
nothing and exits 1 while anything is left.

## 12. Privacy invariant (memory is never inside the *tool's* repo)

Privacy is **structural**: memory lives in a wiki directory that is external to
the shapa *tool* — never inside the shapa repo or the installed package. The
tool ships no wiki content; `shapa init` scaffolds the wiki elsewhere (a
repo-root `shapa/` of *your* project, or a global path) and installs only the
`AGENTS.md` rules and the `arch/` templates there. A repo-root wiki does live
inside your own project — gitignore `shapa/` if you want those notes private —
but the capture hook never writes back into the shapa tool's own tree.

This is an invariant, not a convention: the capture hook **must** write only to
the resolved wiki root (a repo-root `shapa/`, `$SHAPA_MEMORY`, or the path
`shapa init` recorded), never to a path inside the shapa tool's own repo or
package directory. Writing a note anywhere inside the *tool's* repository is a
privacy violation.
