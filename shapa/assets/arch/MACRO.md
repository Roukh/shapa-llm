---
id: MACRO
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 6
locus: output
uses: 0
---

# MACRO — shapa North-Star

> shapa is a standalone open-source repo.
> It has no coupling to any database or personal infrastructure.
> State lives in an EXTERNAL wiki directory (`$SHAPA_MEMORY` or `~/.shapa/memory`)
> as plain markdown; the tool never stores memory inside itself. `shapa init`
> connects a wiki anywhere and installs these arch docs into it.
> Related design docs: [[PRD]] · [[memri-spec]] · [[hook-design]].

---

## North-star objective

shapa maintains a persistent, self-healing markdown-graph memory of an LLM agent's operation that uses one coherent file format everywhere, connects notes through `[[wikilinks]]` so the graph is navigable in Obsidian, scores every note by its consequence for the consuming agent, and prunes structural decay through an automated heartbeat — a memory that is honest, connected, scored, and auditable with zero external dependencies.

---

## Verifiable criteria

Each criterion below is checkable by a command or a specific test.

| # | Criterion | Verified by |
|---|-----------|-------------|
| 1 | The heartbeat prunes a planted orphan (a file with no `[[wikilink]]` to or from any other file) while retaining all linked files. | `tests/test_heartbeat.py::test_prune_removes_only_orphans` |
| 2 | A heartbeat over a fully-linked graph reports zero removals. `shapa heartbeat --dry-run` (over the connected wiki) finds no orphans. | `tests/test_heartbeat.py::test_walk_stays_connected` |
| 3 | The validator enforces the uniform frontmatter schema: type in the enum, consequence 1-10, valid locus, non-negative uses. | `tests/test_validate.py::test_bad_type`, `::test_bad_consequence_and_locus` |
| 4 | Scoring ranks notes by `locus_weight × (consequence/10) × freshness × use_factor`; `record_use` increments the mechanical counter. | `tests/test_score.py::test_meta_high_outranks_output_low`, `::test_record_use_increments_and_boosts` |
| 5 | The validator exits non-zero when the `id` field does not match the filename stem. | `tests/test_validate.py::test_id_must_match_stem` |
| 6 | Connections are body `[[wikilinks]]` and the link graph is undirected (a link from A to B connects both). | `tests/test_heartbeat.py::test_graph_edges_are_undirected` |

---

## Invariants

These hold at all times and are never overridden by any future milestone:

1. **The heartbeat never prunes a file that has a `[[wikilink]]` to or from another file in the same memory.** Connectivity, not content quality, is the sole pruning criterion.

2. **Every wiki file carries the uniform frontmatter** (`id, type, created, consequence, locus, uses`) and connects through `[[wikilinks]]` in the body. One format across `arch/` and `memory/`; there is no separate node format.

3. **A file's `id` always equals its filename stem** (kebab-case, no `.md`). A mismatch is a schema violation the validator rejects.

---

## Current state

The engine is built and tested: the frontmatter parser, the `[[wikilink]]` link-graph, the heartbeat (random-walk pulse + orphan prune), the frontmatter-schema validator, the scoring model, and the fetch/read hook (`shapa/fetch.py`). The wiki is **external** to the tool: `shapa init [DIR]` connects it anywhere (recording the path so every command and hook resolve the same place) and installs these `arch/` docs plus `AGENTS.md` into it. `install.sh` wires the hooks against that connected wiki: fetch on `UserPromptSubmit`, and capture + maintain on `Stop`. The maintainer treats the `arch/` cluster (`type: reference`) as curated and never prunes it. The wiki uses one coherent format across `arch/` and the operational memory notes.

---

## Deferred

- The automated **capture hook** that writes notes at the end of agent work (see [[hook-design]]). Notes are currently written by hand.
