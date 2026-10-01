---
id: shapa-index-backend
type: reference
created: "2026-09-30T02:00:00Z"
consequence: 8
locus: output-meta
scope: repo
summary: shapa spec §5 - per-root sqlite FTS5 index with scan fallback, model2vec vectors, RRF ranking, optional warm daemon, and perf budgets.
---

The per-wiki index, retrieval ranking and latency budgets behind fetch, bootstrap and MCP search. Part of [[shapa-backend-spec]] (§5).

## 5. Index/backend

`shapa/store.py`, one sqlite file per wiki root (`<root>/.shapa-index.db`), never a hard dependency — every consumer falls back to the full-directory scan + hand-rolled BM25 (zero new dependency) if the index is missing, stale, or corrupt. The store also holds the usage counters (`uses`/`last_used`) keyed by root + note id, so reads never rewrite a note (operator decision 7, [[shapa-operator-decisions]]).

- **FTS5 availability probe = the `CREATE VIRTUAL TABLE ... USING fts5(...)` call itself**, not `PRAGMA compile_options` — the pragma is unreliable across CPython builds; attempting the real table creation and catching the failure is the honest probe. False → fall back to the hand-rolled `_bm25_scores()`.
- Vector side stays the JSON-sidecar cosine cache (`.shapa-vectors.json`), fed by `model2vec` instead of `sentence-transformers` — `sqlite-vec` is explicitly **not** adopted (it needs `load_extension`, as fragile as FTS5, for zero benefit at this corpus size).
- `embed.py`: backend is `model2vec`/`minishlab/potion-base-8M` (MIT, numpy-only, no model forward pass, ~94.7% of MiniLM's MTEB at a fraction of the install/latency cost) behind the **identical** `available()`/`embed_one()`/`note_vectors()`/`cosine()` contract — zero call-site changes in `fetch.py`/`maintain.py`.
- **Ranking**: `shapa/rank.py::rrf()` — Reciprocal Rank Fusion (`score(d) = Σ 1/(k+rank_i(d))`, k=60) replaces the old `if embed.available(): cosine else: bm25` either/or branch, so both vote whenever both exist and degrade gracefully to BM25-only otherwise. RRF's rank-position semantics are scale-invariant across backends, so the merge decision stops depending on which similarity metric happened to be active (the original `MERGE_THRESHOLD=0.9` was calibrated only against Jaccard; it is now split into `_EMBED`/`_JACCARD`).
- Reranking (cross-encoder) is deliberately **not** in the hot path (`UserPromptSubmit`/Tier 2) — 100–2000ms for modest gains blows the budget below. Reserved for Tier 3 only, and only if the operator later wants it.
- **Daemon** (optional, latency only, correctness unaffected either way): `shapa serve`, one Unix domain socket per wiki root (mode `0600`, owner-checked), holding the loaded model + open sqlite connection warm. `fetch`/`bootstrap`/`mcp` try the socket first (~0.25s connect timeout), fall back to the in-process cold path on any failure. **Fatal-flaw fix (staleness):** every request re-checks each candidate note's `(mtime_ns, size)` signature before trusting cached index rows — a cheap stat, not a re-embed — so a `capture`/`save` mid-session is reflected on the *next* request. `install.sh` autostarts it lazily on first `fetch`/`bootstrap`; absence never breaks correctness, only latency.

## Perf budgets

Soft regression guards, machine-dependent, not hard CI gates:

| Path | Budget | Measured by |
|---|---|---|
| `shapa bootstrap` (SessionStart) | p50 < 200ms, hard cap 2s | `tests/test_perf.py` — synthetic 500-note fixture |
| `shapa fetch` (UserPromptSubmit) | p50 < 200ms warm, < 1.5s cold | same fixture, daemon on/off |
| `reindex()` on an unchanged wiki | < 50ms (mtime short-circuit) | `test_store.py::test_reindex_noop_is_cheap` |
| `reindex()` incremental (N changed notes) | linear in N, ~5ms/note (model2vec, no GPU) | `test_store.py::test_reindex_incremental_only_touches_changed` |
| MCP tool round-trip | p50 < 300ms | `test_mcp.py` |

Empirical baseline (2026-09-30, single-root, no daemon, ~85 notes): 0.06–0.3s per `shapa fetch`. Today's corpus has latency headroom; the daemon buys margin as corpora grow toward thousands of notes, not for today's size.
