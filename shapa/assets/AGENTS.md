---
id: AGENTS
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 8
locus: output
uses: 0
---

# AGENTS.md — shapa Schema and Operating Rules

> This file is the authoritative schema for the shapa memory graph.
> It governs every file the agent writes. It is a rules document, not a node,
> so it may use headings, tables, and formatting freely.

---

## 1. What shapa is

shapa = **S**elf-**H**ealing **A**utonomous **P**ersistent **A**gent.

It is a persistent markdown-graph memory. The graph lives in an external wiki directory (`$SHAPA_MEMORY`, the path recorded by `shapa init`, or the default `~/.shapa/memory`) — never inside the tool's repo. Design docs (`type: reference`) are installed into the wiki's `arch/` subdirectory by `shapa init`; operational notes (`memory`, `rule`, `issue`) are written directly to the wiki root. Every file is a note-to-self written by an agent at the end of a work session: an instruction on operation, a correction to prior understanding, or an issue flagging a detected problem. The fetch hook (`shapa/fetch.py`, wired by `install.sh` on `UserPromptSubmit`) reads the most relevant notes at the start of each prompt and calls `record_use` on each surfaced note. Writing to the graph at session end is the "always-meta" discipline: every substantive piece of work ends with the agent reflecting on what just happened and committing at least one note to the graph. A topic note is just a file whose `id` matches the topic slug; a phantom `[[wikilink]]` referencing it lights up automatically once that file is added.

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
uses: 0                      # mechanical read counter (set by record_use, never by an LLM)
# last_used: "..."           # added automatically by record_use on first use
---
```

### Frontmatter rules

- `id` must be a kebab-case slug (lowercase letters, digits, hyphens only).
- `id` must match the filename stem exactly: `memory-genesis.md` → `id: memory-genesis`. This is error **F01**.
- `type` must be one of the four values (`memory`, `rule`, `issue`, `reference`). Any other value is error **F02**.
- `created` must be a valid ISO-8601 datetime string. Quote it in YAML. Absence is error **F03**.
- `consequence`, `locus`, and `uses` are required (validated as S01, S02, S03 respectively — see the Scoring fields subsection below).

### Scoring fields (optional; validated when present)

A node's value to the consuming agent is scored from four fields (see §6.5 and [[memri-spec]]):

- `consequence` (1-10) — how much the agent's performance would degrade without this node. Set by the author at capture time; the engine does not compute it. Invalid range is error **S01**.
- `locus` — `output` (changes the agent's answer), `output-meta` (changes how the agent works), or `meta` (changes the agent's self-governance). Drives a weight and is intended to drive retrieval policy. Invalid value is error **S02**.
- `uses` — a non-negative integer incremented by `record_use` when the node file is read or used. Never set by a language model. Invalid value is error **S03**.
- `last_used` — written automatically by `record_use`; drives freshness decay.

Score = `locus_weight × (consequence / 10) × freshness × use_factor`, where freshness decays since `last_used` with a stability that grows with consequence, and `use_factor` rises with `uses`. Run `shapa score` (or `python3 -m shapa.score $SHAPA_MEMORY`) to rank notes.

---

## 4. Node body (LLM autonomy — not enforced)

The body is everything after the closing `---` of the frontmatter. **Its format is the maintaining LLM's discretion.** The two former hard rules — the 400-word plain-prose limit and the links-only-in-metadata ban — have been removed in favour of autonomy: the agent that owns this memory decides how a node reads and may use whatever markdown serves it.

Guidance (not enforced):

- Keep a note focused on one operational point; brevity still helps recall, but there is no hard word cap.
- **Connections to other files appear as Obsidian `[[wikilinks]]` in the body.** This is the only connection mechanism; it is what Obsidian renders in its graph view and what `nodes.py` uses to build the link graph for the heartbeat.
- Headings, lists, tables, and emphasis are allowed when they aid clarity.

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
- **S03** — `uses` is a non-negative integer.

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

- **LLM-distilled capture** — a heuristic capture hook (`shapa/capture.py`) is built and wired on Stop/SubagentStop; the richer LLM-salience version (see [[hook-design]]) is deferred.
- **Heartbeat scheduling** — maintenance runs on the Stop hook and on demand; a standalone cadence trigger is deferred.

**Implemented but not yet run on a live config:** `install.sh` finds the Claude config and wires the fetch hook (UserPromptSubmit) plus capture + `maintain --prune` (Stop) against the connected wiki. Verified in a sandbox but not yet applied to a real `~/.claude/` installation.

These are tracked in [[MACRO]].

---

## 10. File layout within the wiki

The wiki is an external directory (configured by `shapa init` — `$SHAPA_MEMORY`
/ `~/.shapa/memory`), never inside the shapa repo:

```
<wiki_root>/          (external — connected by `shapa init`, never inside the repo)
  AGENTS.md           ← this file, installed by `shapa init` from package assets
  arch/               ← design docs (type: reference), installed by `shapa init`
    MACRO.md          ← shapa's north-star and verifiable criteria
    PRD.md  memri-spec.md  hook-design.md  ...
  <id>.md             ← operational notes captured at the wiki root;
                        each links to its [[type]] + related topics/peers
```

There is no central index. Notes connect peer-to-peer and to type/topic
nodes, so clusters form organically as topics recur. The `arch/` docs are a
separate cluster and are not wired into the memory graph.

## 11. Privacy invariant (memory is never inside the repo)

Privacy is **structural**: the wiki is an external directory, never located
inside the shapa repo or the installed package. The tool ships no wiki content —
`shapa init` connects an external path and installs the design docs there — so a
user's captured memory is never git-tracked and there is nothing to gitignore.

This is an invariant, not a convention: the capture hook (deferred, see
[[hook-design]]) **must** write only to the configured wiki root
(`$SHAPA_MEMORY` or the path `shapa init` recorded), never to a path inside the
repo or package directory. Writing a note anywhere inside a tracked repository is
a privacy violation.
