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

- `shapa/bootstrap.py` - SessionStart hook, Tier 1. Default: the northstar of each wiki in scope (global first), then each wiki's three fires and only their tree (feature, open jobs and tasks, files and arch boxes their text names), from `shapa/focus.py`; `--full` is the old overview (every ranked row and the open ledger).
- `shapa/fetch.py` - UserPromptSubmit hook, Tier 2.
- `shapa/get.py` - `shapa get ID`, Tier 3.
- `shapa/rank.py`, `shapa/bm25.py`, `shapa/embed.py` - fusion ranking (RRF/min-max over BM25 + model2vec vectors).
- `shapa/store.py` - per-root `.shapa-index.db` (FTS5 + vectors + usage counters), scan+BM25 fallback.
- `shapa/serve.py` - optional warm daemon, one Unix socket per wiki root.
- `shapa/codemap.py` - the code map: `shapa map` walks the checkout and fills the derived, gitignored `<wiki>/.shapa-code.db` (files, functions/classes, import edges, call edges, per-file terms and optional vectors). Python by `ast`, JS/TS by regex; incremental by mtime then hash; bounded (see invariants).
- `shapa/partition.py` - `shapa map` / `shapa partition` and the MCP tool `partition`: seeds a fire (paths and functions its feature, jobs, tasks and `about`-linked rows name; BM25/vector recall of each item's words), expands them along callers, importers, imports and callees (two hops, hub-damped), caps by count and bytes, and explains each file.

## Interfaces in

| Caller | Shape |
|---|---|
| SessionStart hook | stdin JSON `{"cwd": ...}` |
| UserPromptSubmit hook | stdin JSON `{"prompt": ...}` |
| `shapa get ID` | CLI arg |
| MCP `search`/`get` | tool call |
| `shapa map`, `shapa partition [F<n>\|slot]` | CLI; MCP `partition` (`action` partition or map) |

## Interfaces out

| To | Shape |
|---|---|
| harness | `additionalContext` JSON (bootstrap), `<shapa-memory>` summary block (fetch) |
| caller | full body text (get), ranked list (MCP search) |

## Invariants

- Tier 1 (bootstrap) loads metadata only (`id/type/summary/tags/consequence/locus`), never a body. By default it loads no row at all: only the focus record and the fires' tree, each text one masked line (a record cannot add a heading, a wrapper tag or a line). A fire on a closed feature is skipped. No skill body ever reaches it. Measured on a fixture of 12 features, 80 jobs and 140 rows: 8546 bytes with `--full`, 1763 by default.
- Focus records and skill versions are documents but are left out of the derived recall index (`db.doc_rows`).
- Tier 2 (fetch) is summary-only, snippet-capped and budget-capped; a confidence floor drops below-floor results rather than padding to a fixed `k`.
- Tier 3 (get, MCP) returns full bodies; agent-invoked only, never injected automatically.
- Reads fan out across every wiki root in scope (this repo's own wiki, then the global wiki); an id that exists in more than one root is reported ambiguous, never silently picked.
- `.shapa-index.db` is derived and gitignored; any consumer falls back to a full scan plus hand-rolled BM25 if it is missing, stale or corrupt.
- Reads never write a row's or note's content - only the index's usage counters move.
- The code map is local and derived: it needs the checkout, so the hosted store neither holds nor serves it (`hosted.md`). Session start opens it read-only (0.5 s lock timeout), never builds or creates it, prints at most one `code map:` line per fire inside the budget, and lists only files that still exist as safe paths; a missing, corrupt, foreign or locked map is silence. Only `shapa map`, `shapa partition` (unless `--no-refresh`) and the MCP tool write it.
- Code map bounds: hidden, vendored (`node_modules`, `vendor`, `dist`, `build`, ...) and generated folders, nested checkouts, symlinks (never followed), non-regular files, `*.d.ts`/`*.min.js`, files over 400 KB or marked generated are skipped; at most 20,000 files; parsing stops after 30 s and the next run resumes; a path with a control, format, markup or backtick character, and every credential file (`.env*` except templates, `.envrc`, `.dev.vars`, `remote.token*`, keys, `auth.json`, ...), is never read, stored or printed. No source text or constant is stored: names, import specs, docstring first lines (redacted). A corrupt or foreign index is deleted and rebuilt.
- A partition is at most 24 files and 120 KB (both overridable per call, hard maxima 200 and 4 MB), best-first by score; a seed is 1.0 for a named path, 0.9 for a named function, at most 0.5 for recall (a recalled file must share 3 query words, or 2 for a short text, or have a vector cosine of 0.35); impact weights are callers of a named function 0.85, importers 0.55 (0.8 when they import the named symbol, 0.2 when they import something else), imports 0.3, callees 0.4, halved for the second hop and divided by 1 + log2(neighbours) for a hub. Call edges are name-level (an unresolved dynamic call, a tsconfig alias or a re-export chain is missed): treat a partition as where to start reading, not the proof of what a change cannot reach.
- Measured on this repo (133 code files, 1.68 MB): map cold 0.53 s, warm 0.00 s (stat only), 2,576 symbols, 861 import edges, 5,119 call edges, 2.1 MB index; the partition of F23 is 14 files and 117 KB (6.8% of repo code), of F14 8 files and 112 KB. On a synthetic 20,000-file tree: cold 3.9 s, warm 0.25 s, session start +56 ms.
- Never blocks the session: any error, or an empty/unreachable wiki, yields empty output and exit 0.
