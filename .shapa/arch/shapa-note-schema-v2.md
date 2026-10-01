---
id: shapa-note-schema-v2
type: reference
created: "2026-09-30T02:00:00Z"
consequence: 8
locus: output-meta
scope: repo
summary: shapa spec §6 - schema v2 frontmatter (summary/scope/tags/status/supersedes), validator codes F04-F11/S04, body size rules, backfill path.
---

The progressive-disclosure note schema, the validator codes that enforce it, and how existing notes migrate. Part of [[shapa-backend-spec]] (§6). The authoritative rule text now ships in `shapa/assets/AGENTS.md` (§3.1 fields, §4 body size, §10.1 multi-wiki reads, §11 lean shape) and lands in every wiki via `shapa upgrade` / `shapa init --upgrade-docs`, which overwrite only `AGENTS.md`/`placement.md`, never a user's notes.

## 6. Note schema v2

Additive frontmatter, backward-compatible (existing notes validate with **warnings**, not errors, for one release, promoted after a one-time backfill):

```yaml
summary: "One line, <=160 chars, no newline"   # the ONLY thing Tier 1 loads
scope: global | repo                            # must match physical bucket
tags: [git, worktree, safety]                    # optional keyword boost
status: active | superseded | draft              # default active
supersedes: old-note-id                          # directed retirement edge
```

`scope: external` and the `applies_to` field as a stored scope are retired (operator decisions 4 and 6, [[shapa-operator-decisions]]); `--applies-to` survives only as a capture-time routing flag into another repo's own wiki. `uses`/`last_used` are no longer frontmatter: the counters live in the index store ([[shapa-index-backend]]), and `shapa upgrade` / `maintain --backfill` strip legacy lines.

`validate.py` codes added by v2:

| code | check | severity |
|---|---|---|
| F04 | `summary` present, ≤160 chars, single line | error (memory/rule/issue), warning (reference) |
| F05 | `scope` present, valid enum | warning this release → error next |
| F06 | `scope` matches physical containing bucket | auto-fixable by `maintain --backfill` |
| F07 | body length within target (150–300 words memory/rule/issue; 2000-word hard cap reference) | warning; never auto-split |
| F08 | `supersedes` target id exists | auto-fixable (clears dangling ref) |
| F09 | duplicate `id` across two roots in one `wiki_roots()` result | **error** — see [[shapa-wiki-resolution]] |
| F10 | lean shape: >40 live root notes, >12 `arch/` refs, or >250 KB live | **error** (decision 6) |
| F11 | `agenda.md` missing or over 3 top-level items | **error** (decision 6) |
| S04 | `status` valid enum | warning |

## Body size

- `memory` / `rule` / `issue`: target 150-300 words. A note needing more splits into two linked notes rather than growing one file — this keeps every note inside the range fetch/embed retrieval performs best at (roughly 100-400 tokens; matches the engine's `SNIPPET_CHARS=500` / `DEFAULT_BUDGET=4000`). Past 300 words: F07.
- `reference` (in `arch/`): may run long, but MUST open with a 1-2 sentence abstract immediately below the frontmatter, and MUST use `##` headings past ~500 words. Past 2000 words: F07 (split into linked `arch/` refs plus an index ref — as this spec itself is).

## Migration

`shapa maintain --backfill` fixes F06/F08 mechanically. A missing `summary` is **never fabricated silently** — it reuses the `resolve_contradictions()` subprocess pattern (`claude -p`, stdin closed) to draft one, written with `summary_status: draft` until confirmed. **Fatal-flaw fix (Codex/OpenCode gap):** because that path requires the `claude` CLI, `maintain --backfill` explicitly reports (not silently skips) any note it could draft a summary for vs. any it couldn't (no `claude` on PATH) — a Codex/OpenCode-only install still gets F04 warnings pointing at exactly which notes need a manually or MCP-`save`-drafted summary.

For whole-wiki format moves (marker, mechanical steps, judgment work list) see §11 in [[shapa-backend-spec]].
