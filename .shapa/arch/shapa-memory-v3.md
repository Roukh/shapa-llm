---
id: shapa-memory-v3
type: reference
created: "2026-10-02T06:00:00Z"
consequence: 9
locus: output-meta
scope: repo
summary: Memory format v3 (wiki format 3) - captured memory as atomic records in an append-only JSONL log, a derived sqlite index, fused recall, no-answer floor.
---

How shapa stores and recalls captured memory from 0.8.0 on: atomic records in a committed log instead of one md note per session, one fused ranking over notes and memories, and a calibrated no-answer floor. Condensed from the memory lab's design; measured numbers are in [[shapa-memory-v3-results]]. Part of [[shapa-backend-spec]].

## Why

Up to format 2 the Stop hook wrote one `memory-session-<sid>.md` per session holding the operator's joined requests (600 chars), never what was done. Recall injected ~500-char body snippets per note (about 1,100 tokens a prompt) and could not say "nothing relevant": every prompt surfaced something, including off-topic ones.

## Records and storage

- **Source of truth:** `<wiki>/memory/YYYY-MM.jsonl`, one record per line, committed with the wiki so a fresh clone keeps every memory. `.gitattributes` sets `memory/*.jsonl merge=union`, so concurrent appends on two branches merge without conflict. Lines are never edited: an update appends a record whose `supersedes` names the old id; archiving appends `{"op":"archive","target":id}`.
- **Fields:** `id, created, session, repo, scope, kind, summary (<=160), body (<=600), tags, source, supersedes, hash`. `kind` is decision, fact, gotcha, outcome, open_question or preference. The id is `m-` plus 10 hex chars of the content hash, so the same memory captured twice, or on two clones, is one record.
- **Redaction first:** `shapa/redact.py` (provider keys, VCS tokens, cloud keys, JWTs, PEM blocks, Bearer, URL passwords, credential assignments) runs before the hash, before any vector exists, before the write.
- **Derived index:** tables inside the existing gitignored `.shapa-index.db`. An unreadable file is set aside as `.shapa-index.db.corrupt` and rebuilt. It is rebuilt from the log: a grown file is read from where the last sync stopped (prefix hash check); anything else rebuilds the record tables. FTS5 `unicode61` (hand-rolled BM25 without FTS5), float32 model2vec vectors cached by content hash, usage counters. Counters and vectors live only here and survive rebuilds.

## Capture (Stop/SubagentStop, `shapa capture`)

Heuristic and stdlib-only. Atomic records come from the final assistant message, read as the operator's three-paragraph job report (after-action becomes `outcome`, footprint becomes `fact` with path tags, each open decision becomes an `open_question`), and from preference or decision sentences in later operator messages. The first prompt is never stored, and neither is tool output. Exact and near duplicates are dropped (shingle Jaccard >= 0.9); an update in [0.6, 0.9) supersedes. Tokens with digits count, and two records whose numbers differ (an issue, port or version) never dedup or supersede each other. Redaction also catches a secret split by a line wrap, and it reads at most 8 KB of input, so a giant message costs no more than a small one. Dedup runs under the log's file lock against the synced index, whose shingle sets are cached by content hash, so a capture tokenizes only what is new; if the index can't be opened it falls back to parsing the log. Routing follows `placement.md`: a repo session writes its own repo's log, and only clearly global preferences go to the global wiki. A repo with no wiki of its own drops its repo-specific memories rather than parking them in the global wiki. Transcripts are read incrementally from a per-session byte offset kept in the index. An optional LLM distiller exists, off by default (`--distill` or `SHAPA_CAPTURE_DISTILL=1`): a detached worker gets the session over stdin, redacted, and never through argv. The hook never blocks or prints, and stays around 200 ms cold on a 2k-record log.

## Recall

- **One fused ranking per root** over md notes and live memory records: vectors (cosine, one scale) fused with BM25 by `rank.fuse` min-max weighting, the lab's best variant. Notes and memories share one BM25 scale: both are re-scored with FTS5's `bm25()` formula over the union corpus statistics (`fetch.UNIFIED_LEXICAL = "union"`), picked over per-source min-max normalization on the dev set. Roots without memories rank exactly as before.
- Per-root guarantees from `select_multi` are unchanged: global relevance order, a per-root inclusion guarantee, the percentile floor, and F09 collision reporting. Memory ids are content-derived, so they are shown once and never flagged as collisions.
- **Injection is summary-only:** `- <id>: <summary>` lines in a compact `<shapa-memory>` wrapper. `shapa get <id>` and MCP `get` return the full text (progressive disclosure). Session start lists notes plus at most 6 top-value memories per root.
- **Modes are reported, never silent:** `fused` with the `[semantic]` extra, `bm25` without it. Recall still works in bm25 mode. `shapa status`/`doctor`, the bootstrap header, the MCP search reply and the degraded hook header all name the mode. `shapa doctor` exits 1 when a wiki is behind format 3, a log has malformed lines, or a non-global root is missing.

## No-answer floor

The pre-v3 absolute guard (raw cosine >= 0.22, or raw BM25 >= 0.5 without vectors) passed 10/10 off-topic questions on a real-size corpus: generic words clear any fixed cosine floor, and BM25 idf grows with corpus size. v3 adds a calibrated floor (`fetch.answerable`), fitted on a separate dev set built by a different agent from different corpus rows, never on the held-out set. It rejects a prompt that names an unseen entity, or that has no content word memory knows, unless the semantic match is strong. The rules, thresholds and calibration are in [[shapa-memory-v3-results]].

## Maintenance and migration

- `shapa maintain --memories` lists hot memories (high use counts) as candidates for a curated md note. `--promote ID` writes the note through `shapa save` and archives the record; `--archive-memory ID` archives. Nothing is ever deleted.
- `shapa upgrade` (format 3) converts each `memory-session-*.md` into one record (redacted, use count carried over), archives the md file (`git mv`), and adds the union-merge attribute. `shapa upgrade --import-memri FILE` is the opt-in importer for memri JSON exports: rows are split into atomic records. A row memri superseded is written as history and archived in the same write. It never dedups against or supersedes live memory, so it can't hide its own successor. Re-importing a file writes nothing.
