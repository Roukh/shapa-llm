---
id: memri-spec
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 7
locus: output
uses: 0
---

# MemRI Specification

> Canonical spec for the MemRI node system — what was designed and built this session.
> See also: [[PRD]] · [[hook-design]] · [[MACRO]]. (Arch is a separate cluster from the memory graph.)

---

## 1. MemRI Philosophy

MemRI is the memory model that sits inside shapa. Every entry in the memory graph is a **note-to-self** written by an agent at the end of a work session. Notes are never written to instruct the agent's next session (the memory is not read at startup); they are written to create a durable, auditable record of what happened, what was decided, and what was found to be wrong.

Three and only three things can be noted:

- A **record of an interaction or session** (what was done and why it matters).
- A **standing rule promoted to permanent policy** (a pattern stable enough to govern future work).
- A **detected problem or contradiction** (something that needs fixing or watching).

This forces the agent into a single, well-defined reflective act: at the end of any substantive work, classify what happened into one or more of those three categories and commit the result to the graph. The discipline is called "always-meta" — every piece of work ends with the agent noting itself.

The read path is built: `shapa/fetch.py`, wired by `install.sh` as a `UserPromptSubmit` hook, surfaces the most relevant and highest-scored notes at the start of each prompt and calls `record_use` on each.
The graph is therefore no longer one-way — work produces nodes, and relevant nodes are fed back at the start of each prompt. What remains one-way is automated *capture*: the hook that would write new notes at session end has not been built yet, so notes are still written by hand.

---

## 2. Node Types

| Type | Label | When to create |
|------|-------|----------------|
| `memory` | MEMORY | After every interaction or agent session, always — at least one per session |
| `rule` | RULE | When a pattern from a memory becomes a stable standing policy |
| `issue` | ISSUE | When a problem, contradiction, or gap is detected that needs resolution |

Types are mutually exclusive. A node has exactly one `type` value. An `issue` is closed when a resolving `memory` or `rule` is created and linked to it via `[[wikilink]]`.

---

## 3. Node File Format

Every wiki file is a UTF-8 markdown file. Operational notes live at the wiki root:

```
<wiki_root>/<id>.md
```

Design docs live in `<wiki_root>/arch/<id>.md`, where `<wiki_root>` is the path `shapa init` connected (`$SHAPA_MEMORY` / `~/.shapa/memory`). Both share the same format: a **YAML frontmatter block** delimited by `---` lines, followed by a **rich markdown body**. There is one file format for the entire wiki.

```
---
id: kebab-case-slug
type: memory | rule | issue | reference
created: "2026-06-30T12:00:00Z"
consequence: 7
locus: output-meta
uses: 0
---
Rich markdown body — headings, lists, tables, emphasis, and Obsidian
[[wikilinks]] are all permitted. Connections to other files are expressed
as [[wikilinks]] here. The body format is the maintaining LLM's discretion.
```

### Frontmatter Schema

| Field | Format | Notes |
|-------|--------|-------|
| `id` | kebab-case slug (lowercase letters, digits, hyphens) | Must match the filename stem exactly (F01) |
| `type` | `memory`, `rule`, `issue`, or `reference` | Exactly one of these four values (F02) |
| `created` | ISO-8601 datetime string, quoted | Example: `"2026-06-30T12:00:00Z"` (F03) |
| `consequence` | integer 1–10 | Performance loss if this file were absent; author-set (S01) |
| `locus` | `output`, `output-meta`, or `meta` | What aspect of the agent this file affects (S02) |
| `uses` | non-negative integer | Mechanical counter; bumped by `record_use`, never by an LLM (S03) |

The parser (`shapa/frontmatter.py`) handles both inline-list and block-list YAML forms. It requires no third-party libraries — the subset is parsed by the Python 3.11 standard library only.

---

## 4. Body — LLM autonomy

The body is free markdown. The two original hard rules — a 400-word plain-prose limit and a links-only-in-metadata ban — were removed in favour of giving the maintaining LLM full autonomy over body format. `shapa/validate.py` validates only frontmatter (F01–F03, S01–S03); it does not check the body.

Guidance (not enforced):

- Keep a note focused on one operational point. There is no hard word cap, but brevity still helps recall.
- **Connections to other files appear as Obsidian `[[wikilinks]]` in the body.** This is the only connection mechanism. Body wikilinks are what Obsidian renders in the graph view and what `nodes.py` parses to build the link graph for the heartbeat.
- Headings, lists, tables, and emphasis are fine when they aid clarity.

All wiki files — operational notes at the wiki root and design docs in the `arch/` subdirectory — share this one permissive body policy and the same frontmatter schema.

---

## 5. Connection Model ([[wikilinks]])

Connections are the edges of the memory graph. They are **Obsidian `[[wikilinks]]` written in the body** — this is the only connection mechanism. There is no `connections` frontmatter field and no noun-keyword system.

**Where they live:** in the body, as `[[wikilinks]]`. A wikilink references another wiki file by its `id` (the filename stem without `.md`). The link may appear anywhere in the body — in prose, a list, or a table.

**Edge condition:** two files are connected when at least one contains a `[[wikilink]]` referencing the other. The edge is undirected: `A → B` adds both A and B to each other's neighbour set in the graph.

**Orphan definition:** a file with no wikilink pointing to it and no wikilink pointing from it to any other file. An orphan's edge set is empty.

**Graph construction:** `nodes.py` parses every file body for `[[wikilinks]]`, builds the adjacency graph, and passes it to `heartbeat.py`. No frontmatter field participates in graph construction.

**Pruning scope:** orphan pruning runs only on operational notes at the wiki root. Files in the `arch/` subdirectory are design docs (`type: reference`) and are never pruned or auto-merged by the maintainer.

To avoid immediate orphan status, link a new note to its type (`[[memory]]`, `[[rule]]`, or `[[issue]]`) and to a related topic or peer note. There is no central index; clusters form organically as topics recur.

---

## 6. Heartbeat Algorithm

The heartbeat is the graph's maintenance process. It runs on demand. One cycle has two phases.

### Phase 1 — Random Walk (the Pulse)

A seedable random walk over the `[[wikilink]]` graph built by `nodes.py`:

1. Sort all file IDs deterministically.
2. Pick a random start file using the seeded RNG (`random.Random(seed)`, or non-deterministic if seed is `None`).
3. At each step, sort the current file's `[[wikilink]]` neighbours, pick one at random, move to it.
4. Stop when the current file has no neighbours, or after `max_steps` files have been visited.
5. Return the ordered list of visited file IDs as the "pulse" — the random string of connected files.

All sort-then-pick operations ensure that the same seed produces the same walk regardless of filesystem or dict ordering. Determinism is per-seed via sorted collections, not global state.

### Phase 2 — Orphan Scan and Prune

The orphan scan operates over the **entire wiki root** (operational notes, excluding the `arch/` cluster), not just the files visited during the walk:

- An orphan is any file whose `[[wikilink]]` edge set is empty.
- Files visited during the walk are connected by construction (they each had at least one wikilink neighbour); they will never be orphans.
- But the walk is a sample — unvisited connected files are also safe. The orphan scan therefore ignores the walk result and checks every file in the directory independently.
- Orphans found are pruned: their `.md` files are deleted (or reported without deletion in `--dry-run` mode).

**Why whole-directory scope, not walk scope:** the walk visits at most `max_steps` files from a random start. A file could be well-connected (never an orphan) but simply not visited. Limiting the orphan scan to walked files would incorrectly prune connected-but-unwalked files. The correct scope is always the full set of operational notes at the wiki root.

### Implementation

| Module | Responsibility |
|--------|----------------|
| `shapa/heartbeat.py` | `heartbeat()` entry point, `random_walk()`, `find_orphans()`, `prune_orphans()`, CLI |
| `shapa/nodes.py` | `Node` and `Graph` dataclasses, `load_nodes()`, `build_graph()` — parses `[[wikilinks]]` from bodies |
| `shapa/validate.py` | `validate_node()`, frontmatter-schema checks (F01–F03, S01–S03), CLI |
| `shapa/frontmatter.py` | `parse()` — shared YAML-subset parser, zero dependencies |

---

## 7. How to Run

**Run the heartbeat (dry-run — no files deleted):**

```
shapa heartbeat --dry-run
```

**Run the heartbeat with a fixed seed and custom step count:**

```
shapa heartbeat --seed 42 --max-steps 8
```

**Validate a single file:**

```
shapa validate <wiki_root>/<id>.md
```

**Validate multiple files at once (defaults to the connected wiki):**

```
shapa validate
```

The validator exits 0 if all files are valid, 1 if any violation is found. Each violation is reported as `[RULE] line N: message`.

**Run the test suite:**

```
python3 -m unittest discover -s tests
```

---

## 7.5 Node scoring

Each node may carry four fields that estimate its value to the consuming agent (`shapa/score.py`):

- `consequence` (1-10, author-set, not engine-computed) — performance loss if the node were absent.
- `locus` — `output` (changes the answer), `output-meta` (changes how the agent works), or `meta` (changes self-governance). Weights the score and is intended to drive retrieval policy (meta pre-loaded, output on demand).
- `uses` — a mechanical counter incremented by `record_use()` when the node is read; never set by a language model.
- `freshness` (derived) — exponential decay since `last_used`, with a stability that grows with `consequence` so a high-consequence rule barely fades while a routine memory decays fast.

Composite:

```
locus_weight = {output: 1.0, output-meta: 1.5, meta: 2.0}
stability    = 7 + (consequence-1)/9 * (365-7)          # days
freshness    = exp(-age_days / stability)
use_factor   = 1 + 0.5 * min(1, log1p(uses)/log1p(50))  # 1.0 .. 1.5
score        = locus_weight * (consequence/10) * freshness * use_factor
```

The validator checks these fields: **S01** (consequence 1-10), **S02** (valid locus), **S03** (uses non-negative). Rank with `shapa score`; record a use with `shapa score --use <file>`.

**Honest limitation:** `uses` is now live — the fetch hook increments it via `record_use` on every prompt a note is surfaced. What still remains future work is the *measured* consequence: a node's actual effect on agent output across sessions requires session-outcome logging that does not yet exist. This is the open work tracked in the `issue-scoring-and-capture` node.

## 8. Status

**Built:** `shapa/frontmatter.py`, `shapa/nodes.py` (wikilink graph builder), `shapa/heartbeat.py`, `shapa/validate.py` (frontmatter-schema only: F01–F03, S01–S03), `shapa/score.py`, `shapa/cli.py` (the `shapa` command incl. `init`), `shapa/config.py` (external-wiki resolution + pointer), and the design docs shipped as package assets: `shapa/assets/AGENTS.md` (the schema rules document) and `shapa/assets/arch/` (PRD, memri-spec, hook-design, MACRO), installed into the connected wiki by `shapa init`. The noun-keyword machinery (`shapa/nouns.py`, `shapa/data/nouns.txt`) and the `wiki/nodes/` folder were removed; connections are now `[[wikilinks]]` in the body only.

**Implemented but not yet run on a live config:** `install.sh` finds the Claude config and wires the fetch/capture/maintain hooks against the connected wiki; verified in a sandbox.

**Deferred:** the automated capture hook (Stop/SubagentStop) that writes new notes at the end of every agent session. Specified in [[hook-design]].
