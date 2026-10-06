---
id: read-path
type: reference
created: "2026-10-05T20:22:00Z"
consequence: 8
locus: output-meta
scope: repo
summary: Read path - SessionStart/UserPromptSubmit/get/MCP search, ranking ledger items and MRI rows across every wiki root in scope.
---

# Read path

Surfaces relevant memory to a session: metadata only at session start, ranked
summaries per prompt, full bodies on demand. Read-only toward the agent;
never blocks.

## Owns

- `shapa/bootstrap.py` - SessionStart hook, Tier 1.
- `shapa/fetch.py` - UserPromptSubmit hook, Tier 2.
- `shapa/get.py` - `shapa get ID`, Tier 3.
- `shapa/rank.py`, `shapa/bm25.py`, `shapa/embed.py` - fusion ranking (RRF/min-max over BM25 + model2vec vectors).
- `shapa/store.py` - per-root `.shapa-index.db` (FTS5 + vectors + usage counters), scan+BM25 fallback.
- `shapa/serve.py` - optional warm daemon, one Unix socket per wiki root.

## Interfaces in

| Caller | Shape |
|---|---|
| SessionStart hook | stdin JSON `{"cwd": ...}` |
| UserPromptSubmit hook | stdin JSON `{"prompt": ...}` |
| `shapa get ID` | CLI arg |
| MCP `search`/`get` | tool call |

## Interfaces out

| To | Shape |
|---|---|
| harness | `additionalContext` JSON (bootstrap), `<shapa-memory>` summary block (fetch) |
| caller | full body text (get), ranked list (MCP search) |

## Invariants

- Tier 1 (bootstrap) loads metadata only (`id/type/summary/tags/consequence/locus`), never a body.
- Tier 2 (fetch) is summary-only, snippet-capped and budget-capped; a confidence floor drops below-floor results rather than padding to a fixed `k`.
- Tier 3 (get, MCP) returns full bodies; agent-invoked only, never injected automatically.
- Reads fan out across every wiki root in scope (this repo's own wiki, then the global wiki); an id that exists in more than one root is reported ambiguous, never silently picked.
- `.shapa-index.db` is derived and gitignored; any consumer falls back to a full scan plus hand-rolled BM25 if it is missing, stale or corrupt.
- Reads never write a row's or note's content - only the index's usage counters move.
- Never blocks the session: any error, or an empty/unreachable wiki, yields empty output and exit 0.
