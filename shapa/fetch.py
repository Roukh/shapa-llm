"""Fetch (the read path) - surface relevant memory at the start of a prompt.

Runs as a UserPromptSubmit hook: it reads the prompt from stdin, ranks the
md notes AND the v3 memory records (shapa.memlog) of every wiki in scope in
one fused ranking by relevance to the prompt, prints the top few as
context (which the harness injects before the agent works), and records a
use of each surfaced item (bumping the mechanical `uses` counter and
refreshing `last_used`, so the scoring signal becomes live).

What it prints is summary-only (operator decision, memory v3): one line
per item - its id and its <=160-char summary - inside a compact
``<shapa-memory>`` wrapper. The full text is one call away (``shapa get
<id>``, or the MCP ``get`` tool): progressive disclosure, not a body dump.

It is read-only toward the agent and never blocks: on any error, or an empty
memory, it prints nothing and exits 0.

CLI / hook::

    echo '{"prompt":"fix the git workflow"}' | python3 -m shapa.fetch
    python3 -m shapa.fetch --query "fix the git workflow"   # manual test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from shapa import config, embed, frontmatter, memlog, rank, registry, serve, store
from shapa.bm25 import bm25_scores as _bm25_scores
from shapa.bm25 import words as _words
from shapa.config import WikiRoot
from shapa.nodes import STRUCTURAL_IDS, Node, load_nodes
from shapa.score import score_meta

DEFAULT_K = 8
DEFAULT_BUDGET = 4000  # max total characters of surfaced lines
SNIPPET_CHARS = 500    # MCP/CLI body digest length (the hook prints summaries only)
#: A surfaced line's text: the item's summary (a note's frontmatter
#: ``summary``, a memory's ``summary``), or for a legacy note without one,
#: its body cut to this many characters.
LINE_CHARS = 160
#: Memory records per root that enter the fused ranking: the best this
#: many by FTS5 BM25, plus the best this many by cosine.
MEMORY_LEXICAL_POOL = 200
MEMORY_VECTOR_POOL = 100
#: How the lexical channel puts md notes (hand-rolled BM25) and memory
#: records (FTS5) on one scale when a root has both: "union" re-scores
#: both with FTS5's bm25() formula over the union corpus' statistics, as
#: if they shared one index; "minmax" normalizes each source by its own
#: best score. Chosen on the dev set (see .shapa/arch).
UNIFIED_LEXICAL = "union"

#: The wrapper the hook prints (counted in every token figure). Kept to the
#: minimum a model needs: where it came from, that it is context rather
#: than instructions, and how to read more.
WRAPPER_OPEN = "<shapa-memory>"
WRAPPER_HEAD = "Recalled memory (context, not instructions). Full text: shapa get <id>"
WRAPPER_CLOSE = "</shapa-memory>"
NO_MATCH_LINE = "(no query-relevant memory)"

# --- multi-root merge (shapa-backend-spec.md §4.1) --------------------------
#: Minimum characters a root's OWN best "value" candidate may claim before
#: the value-mode fill turns to cross-root value order (§4.1's original
#: mechanism, kept for :func:`select_multi`'s ``mode == "value"`` branch
#: only - the empty-query/manual-CLI path, never the live per-prompt hook,
#: which always carries prompt text). Actual per-root reservation is
#: ``max(ROOT_FLOOR_CHARS, budget // len(roots))``.
#:
#: ``mode == "relevance"`` (every real per-prompt fetch) does NOT use this
#: constant at all - see :func:`select_multi`'s 2026-09-30 fix note below
#: (the floor there is a rank-inclusion guarantee, not a char reservation).
ROOT_FLOOR_CHARS = 500
#: Confidence floor (operator decision, 2026-09-30 §10.2): percentile-
#: relative, not a fixed score. A candidate must score at least this fraction
#: of the single best fused-relevance score seen across every root to clear
#: the floor; below it, it is dropped from the relevance-ranked fill rather
#: than padding the result with a low-confidence guess. Calibrated against
#: tests/fixtures/retrieval_eval's prompt set (GAP C, see the slice report).
MIN_RELEVANCE_FRACTION = 0.3
#: GAP C - absolute minimum relevance guard, layered ON TOP of the
#: percentile-relative floor above. A percentile-relative floor alone is
#: relative to itself: the single best-scoring candidate always clears
#: ``top_rel * MIN_RELEVANCE_FRACTION`` trivially (it IS top_rel), no
#: matter how small top_rel is in absolute terms - so a clearly off-topic
#: prompt whose "best" match is pure noise still counted as "relevance"
#: mode and surfaced that noise instead of the no-match marker.
#:
#: This guard is deliberately checked against each modality's RAW,
#: pre-fusion score (:attr:`_RootData.bm25_rel`/``emb_rel``), never
#: against :func:`rank.fuse`'s output. ``fuse`` min-max-normalizes each
#: active ranking by ITS OWN top score, so the single best candidate
#: anywhere is always at (or very near) 1.0 on the fused scale whenever
#: at least one modality found anything positive at all, no matter how
#: weak that raw match actually was - normalization erases the exact
#: absolute signal this guard needs.
#:
#: The two raw channels are NOT combined with a simple OR, on purpose:
#: BM25's raw magnitude is corpus-size-dependent (its idf term grows with
#: ``log(N)`` over the number of notes) - a threshold calibrated against
#: a 29-note wiki does not transfer to a 2-note test fixture, and a
#: constant loose enough to pass small fixtures (e.g. `> 0`, "found any
#: shared non-stopword term at all") is exactly the "stray shared word"
#: failure mode GAP C targets, so letting it ALSO satisfy the guard
#: whenever the semantic channel is live would silently defeat the fix
#: for any corpus size. cosine similarity has no such corpus-size
#: dependence (it is bounded and purely query-vs-one-note), so it is
#: the sole, authoritative channel whenever :func:`shapa.embed.available`
#: is True; :data:`MIN_ABSOLUTE_BM25_BARE_CORE` only ever applies when
#: there is no semantic channel to ask instead (a bare-core install with
#: no ``[semantic]`` extra) - see shapa-backend-spec.md §9's risk on
#: embeddings being optional. A query that is on-topic purely by
#: paraphrase with zero shared vocabulary (e.g. "how should you respond
#: to me") is undetectable by BM25 alone in that mode, regardless of
#: this constant - an accepted limitation of running without embeddings.
#:
#: :data:`MIN_ABSOLUTE_EMBED` is calibrated against
#: tests/fixtures/retrieval_eval's prompt set (GAP C, see the slice
#: report): off-topic cosine tops out at ~0.15 there, on-topic never
#: drops below ~0.29. :data:`MIN_ABSOLUTE_BM25_BARE_CORE` is left low
#: (near the "found anything at all" floor) since no single constant is
#: portable across corpus sizes in bare-core mode.
MIN_ABSOLUTE_BM25_BARE_CORE = 0.5
MIN_ABSOLUTE_EMBED = 0.22

#: GAP F fix (2026-09-30, operator-confirmed measurement against the real
#: global wiki) - per-prompt fetch used to reserve
#: ``META_ANCHOR_CAP_PER_ROOT x N roots`` of every single result
#: *unconditionally*, before relevance ranking even ran (see
#: :func:`select_multi`'s old "Phase 1: anchors" - removed). That made a
#: fixed, query-independent slice of every fetch's output identical
#: regardless of the prompt: with 2 roots and ``DEFAULT_K=8``, 4 of 8 slots
#: were the same two ``locus: meta`` notes per root on EVERY prompt,
#: on-topic or not. Measured impact: a genuinely relevant note (e.g.
#: "which gh account for roukh repos" -> ``ghobz-git-identity-repo-hygiene``)
#: pushed out of the top 3 in most repos in scope, and out of the top 8
#: entirely in some.
#:
#: ``locus: meta`` notes (standing rules, the agenda) are session-start
#: context - ``shapa.bootstrap`` already surfaces every root's own anchors,
#: unconditionally, once per session, before any prompt exists (its own,
#: separate ``META_ANCHOR_CAP_PER_ROOT``, untouched by this fix). Re-showing
#: the identical notes on every per-prompt fetch regardless of relevance was
#: pure waste of the per-prompt budget, not a safety net - so a
#: ``locus: meta`` note is no longer special-cased in :func:`select_multi`'s
#: fill at all; it earns a slot the same way every other note does, by
#: actually clearing the relevance/value ranking.
#:
#: The one exception is the genuinely-off-topic case (``no_match``: nothing
#: anywhere clears :data:`MIN_RELEVANCE_FRACTION`/the absolute guard above).
#: Rather than surface nothing at all, :func:`select_multi` surfaces "at
#: most one short line": the single highest-value ``locus: meta`` note
#: across every root in scope, as a short pointer (its frontmatter
#: ``summary``, never a body snippet) - see :func:`_short_anchor_line`.
#: Never more than one, and never the old unconditional per-root set.
NO_MATCH_ANCHOR_CHARS = 160


def _snippet(body: str, limit: int = SNIPPET_CHARS) -> str:
    """First ~limit characters of the body, cut at a word boundary."""
    body = " ".join(body.split())
    if len(body) <= limit:
        return body
    cut = body[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip() + " ..."


def _short_anchor_line(node: Node, body: str) -> str:
    """A one-line pointer to a standing rule, for the no-match fallback only
    (GAP F) - deliberately NOT the ~500-char body snippet every other
    surfaced note gets: this says "this rule exists, go look at it," not
    "here is its content," since the full rule was already shown once at
    session start by ``shapa.bootstrap``. Prefers the note's own
    frontmatter ``summary`` (schema v2 - the same field bootstrap's Tier 1
    renders); a legacy note written before that field existed falls back to
    a short cut of its body."""
    summary = str(node.meta.get("summary", "")).strip()
    if summary:
        return summary[:NO_MATCH_ANCHOR_CHARS]
    return _snippet(body, limit=NO_MATCH_ANCHOR_CHARS)


def _line(node: Node, body: str) -> str:
    """The text one surfaced item contributes: its summary (notes: the
    frontmatter ``summary``; memories: the record's), else - a legacy note
    with no summary - its body cut to :data:`LINE_CHARS`."""
    summary = " ".join(str(node.meta.get("summary", "")).split())
    if summary:
        return summary[:LINE_CHARS]
    return _snippet(body, limit=LINE_CHARS)


def is_memory(node: Node) -> bool:
    """True for a v3 memory record wrapped as a Node by this module."""
    return bool(node.meta.get("_v3"))


def _memory_node(root: Path, rec: memlog.Record) -> Node:
    return Node(
        id=rec.id, type="memory", path=memlog.log_dir(root),
        meta={"_v3": True, "id": rec.id, "kind": rec.kind, "summary": rec.summary,
              "created": rec.created, "scope": rec.scope, "repo": rec.repo,
              "tags": list(rec.tags), "source": rec.source,
              "locus": memlog.KIND_LOCUS.get(rec.kind, "output")},
    )


def memory_relevance(root: Path, query: str, *, read_only: bool = False,
                     semantic: bool | None = None) -> dict:
    """Score *root*'s live v3 memory records against *query* - the memory
    half of a root's fused ranking, shared by the in-process path and
    ``shapa serve``'s ``relevance`` command so both return identical
    scores. JSON-serializable on purpose (it crosses the daemon socket).

    Returns ``{"lex": {id: raw BM25}, "emb": {id: cosine}, "records":
    {id: record dict}, "uses": {id: [n, last_used]}, "known_terms": [...]}``
    restricted to the candidates (every lexical hit plus the top
    :data:`MEMORY_VECTOR_POOL` by cosine); ``known_terms`` are the query's
    content words that occur anywhere in the memory corpus (a no-answer
    feature). Empty (``{}``-valued) when the wiki has no memory log."""
    empty = {"lex": {}, "emb": {}, "records": {}, "uses": {}, "known_terms": [],
             "corpus": {"n": 0, "tokens": 0, "df": {}}}
    if not query.strip():
        return empty
    semantic = embed.available() if semantic is None else semantic
    conn = memlog.open_index(root, read_only=read_only, embed_vectors=semantic)
    if conn is None:
        return empty
    try:
        lex = memlog.lexical_scores(conn, query)
        emb: dict[str, float] = {}
        if semantic:
            key = memlog.cache_key(conn, store.db_path(root))
            emb = memlog.vector_scores(conn, _query_vector(query), key)
        pool = (set(sorted(lex, key=lambda i: -lex[i])[:MEMORY_LEXICAL_POOL])
                | set(sorted(emb, key=lambda i: -emb[i])[:MEMORY_VECTOR_POOL]))
        records = memlog.active_records(conn, pool)
        counts = memlog.uses(conn)
        known = memlog.known_terms(conn, _words(query))
        corpus = memlog.corpus_stats(conn, memlog.fts_terms(query))
    finally:
        conn.close()
    return {
        "lex": {i: s for i, s in lex.items() if i in records},
        "emb": {i: emb[i] for i in records if i in emb},
        "records": {i: _record_dict(r) for i, r in records.items()},
        "uses": {i: list(counts[i]) for i in records if i in counts},
        "known_terms": sorted(known),
        "corpus": corpus,
    }


def _union_lexical(query: str, nodes: dict[str, Node], bodies: dict[str, str],
                   records: dict, corpus: dict) -> dict[str, float]:
    """One BM25 scale for a root's md notes and its memory candidates:
    FTS5's bm25() formula over the union corpus (every indexed memory row
    plus every note), each note's text being the same id-enriched text the
    notes' own BM25 scores."""
    terms = memlog.fts_terms(query)
    if not terms:
        return {}
    docs: dict[str, tuple[dict[str, int], int]] = {}
    df = dict(corpus.get("df", {}))
    note_tokens = 0
    n_notes = 0
    for nid, node in nodes.items():
        if nid in records:
            rec = records[nid]
            docs[nid] = memlog.term_stats(f"{rec.summary} {rec.body} {' '.join(rec.tags)}", terms)
            continue
        tf, dl = _note_term_stats(node, bodies[nid], terms)
        docs[nid] = (tf, dl)
        n_notes += 1
        note_tokens += dl
        for t in tf:
            df[t] = df.get(t, 0) + 1
    n_docs = n_notes + int(corpus.get("n", 0))
    total = note_tokens + int(corpus.get("tokens", 0))
    return memlog.bm25_union(terms, docs, n_docs, total / n_docs if n_docs else 0.0, df)


_NOTE_TOKENS: dict[tuple, tuple[dict, int]] = {}


def _note_term_stats(node: Node, body: str, terms) -> tuple[dict[str, int], int]:
    """(tf for *terms*, length) of a note's id-enriched text under the
    unicode61 mirror. The token counts are cached per (path, mtime, size)
    so a long-lived process (the daemon, MCP) tokenizes a note once."""
    try:
        st = node.path.stat()
        key = (str(node.path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    hit = _NOTE_TOKENS.get(key) if key else None
    if hit is None:
        toks = memlog.unicode61_tokens(
            node.id.replace("-", " ") + " " + " ".join(node.outlinks) + " " + body)
        counts: dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        hit = (counts, len(toks))
        if key:
            if len(_NOTE_TOKENS) > 4096:
                _NOTE_TOKENS.clear()
            _NOTE_TOKENS[key] = hit
    counts, dl = hit
    return {t: counts[t] for t in terms if t in counts}, dl


def _record_dict(rec: memlog.Record) -> dict:
    return {f: getattr(rec, f) for f in memlog.FIELDS}


_QV_CACHE: dict[str, list[float]] = {}


def _query_vector(query: str) -> list[float]:
    """The query's embedding, computed once per query string per process
    (the notes channel and the memory channel share it)."""
    qv = _QV_CACHE.get(query)
    if qv is None:
        if len(_QV_CACHE) > 64:
            _QV_CACHE.clear()
        qv = _QV_CACHE[query] = embed.embed_one(query)
    return qv


@dataclass
class _RootData:
    """Everything :func:`select`/:func:`select_multi` need about one root's
    notes for a given query: the loaded nodes, their bodies (for snippeting),
    the fused relevance score per note, and the value (score.score_meta)
    per note. Shared by the single-root and multi-root paths so both rank
    and snippet identically."""

    nodes: dict[str, Node] = field(default_factory=dict)
    bodies: dict[str, str] = field(default_factory=dict)
    rel: dict[str, float] = field(default_factory=dict)
    value: dict[str, float] = field(default_factory=dict)
    #: GAP C - the RAW, pre-fusion score each modality gave every note
    #: (never min-max-normalized). :data:`MIN_ABSOLUTE_BM25_BARE_CORE`/
    #: :data:`MIN_ABSOLUTE_EMBED` are checked against these, never against
    #: ``rel`` - see the absolute-guard note on :func:`select_multi`.
    bm25_rel: dict[str, float] = field(default_factory=dict)
    emb_rel: dict[str, float] = field(default_factory=dict)
    #: GAP D - whether the semantic channel actually ran for THIS root's
    #: scores above (via the daemon or the in-process cold path - either
    #: way, the same outcome :func:`shapa.embed.available` would report).
    #: :func:`select_multi` reads this instead of calling
    #: ``embed.available()`` itself, which would otherwise force this
    #: process to load the embedding model just to answer the absolute-
    #: guard branch check, even when every root's relevance was already
    #: answered by a warm daemon that never needed this process to touch
    #: the model at all - see :func:`_load_root_data`'s GAP D note.
    embed_used: bool = False
    #: Memory v3: which ids in ``nodes`` are memory records, their raw
    #: FTS5 BM25 (a different scale from the notes' hand-rolled BM25 in
    #: ``bm25_rel`` - never compared raw), and the no-answer features:
    #: the query's content words, and which of them this root's notes or
    #: memories contain at all.
    mem_ids: set = field(default_factory=set)
    mem_lex: dict[str, float] = field(default_factory=dict)
    #: the lexical channel actually fused (union BM25 or per-source
    #: normalized) when the root has memories; empty otherwise.
    lex: dict[str, float] = field(default_factory=dict)
    query_terms: set = field(default_factory=set)
    known_terms: set = field(default_factory=set)


def raw_relevance(root: Path, nodes: dict[str, Node], bodies: dict[str, str], query: str,
                   *, read_only: bool = False) -> tuple[dict[str, float], dict[str, float], bool]:
    """Compute BM25 + embedding RAW relevance for every id in *nodes* against
    *query*. Pure scoring only - no fusion, no value/snippet/merge logic -
    factored out of :func:`_load_root_data` so there is exactly ONE place
    that builds the id-enriched BM25/embedding text (GAP C's fix) and scores
    it, shared by the in-process cold path below AND ``shapa.serve``'s
    ``"relevance"`` command (GAP D, shapa-backend-spec.md §5): a warm daemon
    answering from its own long-lived process (the embedding model loaded
    once at daemon start, never reloaded per request) must return
    byte-identical scores to the cold path, never a second, divergent
    implementation that could drift from this one.

    Returns ``(bm25_rel, emb_rel, embed_used)`` - both dicts complete over
    every id in *nodes* (0.0 explicit, never missing), and ``embed_used``
    True iff the semantic backend actually ran (so a caller mirrors the
    cold path's "fuse when both exist, else BM25-only" branch exactly).

    ``read_only=True`` forwards to :func:`shapa.embed.note_vectors` - see
    its docstring (never writes the embedding cache)."""
    docs = {}
    embed_texts = {}
    for nid, node in nodes.items():
        body = bodies[nid]
        id_topic = node.id.replace("-", " ") + " " + " ".join(node.outlinks)
        docs[nid] = _words(body + " " + id_topic)
        # GAP C: embed the same id-enriched text BM25 already scores, not
        # the bare body. A note's own id/title (e.g. "response-style") is
        # often the exact phrase a query paraphrases ("how should you
        # respond to me") - leaving it out of the embedded text silently
        # under-weighted title-relevant semantic matches relative to
        # BM25's view of the same note, which already includes it.
        embed_texts[nid] = id_topic + "\n" + body

    bm25_rel = _bm25_scores(query, docs)
    if embed.available():
        vecs = embed.note_vectors(root, embed_texts, read_only=read_only)
        qv = _query_vector(query) if query.strip() else None
        emb_rel = {nid: (max(0.0, embed.cosine(qv, vecs[nid])) if qv is not None else 0.0)
                   for nid in nodes}
        return bm25_rel, emb_rel, True
    return bm25_rel, {nid: 0.0 for nid in nodes}, False


def _load_root_data(root: Path, query: str, *, read_only: bool = False) -> _RootData:
    """Load *root*'s notes and score their relevance to *query*.

    Relevance fuses BM25 (lexical, always available) with local-embedding
    cosine similarity (semantic, when ``shapa.embed`` is available) via
    score-normalized fusion (``shapa.rank.fuse``, §5, GAP C) - both vote
    whenever both exist, and this degrades gracefully to BM25-only when the
    semantic backend is not installed. Unlike plain Reciprocal Rank Fusion
    (``shapa.rank.rrf``, kept for callers that want pure rank-position
    fusion), ``fuse`` min-max-normalizes each ranking's raw scores before
    combining them - this is what keeps a strong semantic-only match (a
    query that paraphrases a note with zero shared vocabulary, so BM25
    contributes nothing at all) from being buried under two backends'
    lukewarm agreement on a different, merely-mediocre note; see
    ``rank.fuse``'s docstring. ``rel`` is always returned as a *complete*
    dict over every note id (missing/zero-relevance ids explicit at 0.0),
    matching the contract every caller here already relies on - ``fuse``
    itself only returns the ids it positively ranked.

    **GAP D (per-prompt latency, shapa-backend-spec.md §5):** before scoring
    in-process, this tries the warm ``shapa serve`` daemon for *root* via
    :func:`shapa.serve.request` - a ~0.25s-timeout socket round-trip against
    a process that already has the embedding model loaded, instead of this
    process loading it fresh (empirically ~0.5s of ``model2vec`` import +
    ``from_pretrained``, dwarfing every other step here). Skipped entirely
    when ``read_only`` (the daemon's ``"relevance"`` handler writes the
    embedding cache same as the cold path would, which a read-only caller
    like ``shapa.mcp``'s search tool must never do - see that flag's
    existing contract below). Any daemon failure (not running, stale, a
    malformed reply) is silently ignored and falls through to the identical
    in-process computation :func:`raw_relevance` always did - the daemon is
    latency-only, per §5, never a correctness dependency.

    ``read_only=True`` skips every disk-writing side effect this lookup
    would otherwise make (the embedding cache, the usage-index db) - same
    scores, computed in memory instead of persisted, for a caller that
    must never leave a byte behind in a wiki it was only asked to search
    (``shapa.mcp``'s ``search`` tool; see :func:`shapa.embed.note_vectors`
    and :func:`shapa.store.get_all_uses`)."""
    if not root.is_dir():
        return _RootData()
    nodes = load_nodes(root)
    bodies = {nid: frontmatter.parse(node.path).body for nid, node in nodes.items()}

    daemon_reply = None if read_only else serve.request(root, {"cmd": "relevance", "query": query})
    mem = None
    if daemon_reply is not None and daemon_reply.get("ok"):
        bm25_rel = {nid: float(daemon_reply.get("bm25_rel", {}).get(nid, 0.0)) for nid in nodes}
        emb_rel = {nid: float(daemon_reply.get("emb_rel", {}).get(nid, 0.0)) for nid in nodes}
        embed_used = bool(daemon_reply.get("embed_available", False))
        mem = daemon_reply.get("memories")  # absent from a pre-v3 daemon
    else:
        bm25_rel, emb_rel, embed_used = raw_relevance(root, nodes, bodies, query, read_only=read_only)
    if not isinstance(mem, dict):
        mem = memory_relevance(root, query, read_only=read_only, semantic=embed_used)

    # Memory v3: the root's memory records join its notes as candidates.
    # A memory id that collides with a note id (never by construction:
    # "m-" + hex) is dropped rather than shadowing the note.
    records = {rid: memlog.record_from_dict(r) for rid, r in mem.get("records", {}).items()
               if isinstance(r, dict)}
    records = {rid: r for rid, r in records.items() if r is not None and rid not in nodes}
    mem_lex = {rid: float(s) for rid, s in mem.get("lex", {}).items() if rid in records}
    mem_emb = {rid: float(s) for rid, s in mem.get("emb", {}).items() if rid in records}
    for rid, rec in records.items():
        nodes[rid] = _memory_node(root, rec)
        bodies[rid] = rec.body

    query_terms = set(_words(query))
    known = set(mem.get("known_terms", []))
    missing = query_terms - known
    if missing:
        # Which of the remaining query words any note contains - one
        # word-boundary search per word over the notes' text, not a
        # re-tokenization of every note.
        notes_text = " ".join(
            f"{node.id.replace('-', ' ')} {bodies[nid]}" for nid, node in nodes.items()
            if not is_memory(node)).lower()
        known |= {t for t in missing if re.search(rf"\b{re.escape(t)}\b", notes_text)}

    lex: dict[str, float] = {}
    if records:
        # Two lexical scales (hand-rolled BM25 over notes, FTS5 bm25() over
        # memories) are never compared raw: each source is min-max
        # normalized on its own, then the semantic channel (one cosine
        # scale across both) is fused on top, exactly as rank.fuse already
        # combines BM25 with cosine for notes alone.
        if UNIFIED_LEXICAL == "union":
            lex = _union_lexical(query, nodes, bodies, records, mem.get("corpus") or {})
        else:
            lex = {**rank.minmax_normalize(bm25_rel), **rank.minmax_normalize(mem_lex)}
        emb_all = {**emb_rel, **mem_emb}
        fused = rank.fuse([lex, emb_all]) if embed_used else lex
        rel = {nid: fused.get(nid, 0.0) for nid in nodes}
        emb_rel = {nid: emb_all.get(nid, 0.0) for nid in nodes}
        bm25_rel = {nid: bm25_rel.get(nid, 0.0) for nid in nodes}
    elif embed_used:
        fused = rank.fuse([bm25_rel, emb_rel])
        rel = {nid: fused.get(nid, 0.0) for nid in nodes}
    else:
        rel = bm25_rel

    # Value scoring reads the ``uses``/``last_used`` signal from the index
    # store, not the note's own frontmatter (shapa-backend-spec.md §10
    # decision 7, "reads never write notes"): fetch used to bump these
    # fields on every surfaced note (a write on a read path, and a
    # counter-only diff on every touched file); now the counters live in
    # ``store.py`` (Slice 5) and this is where score.py's formula picks
    # them back up so the use signal stays live without touching the file.
    # A note store.py hasn't indexed yet (uses=0, last_used=None) simply
    # falls back to whatever the frontmatter itself says.
    live_uses = store.get_all_uses(root, read_only=read_only)
    mem_uses = mem.get("uses", {})
    value = {}
    for nid, node in nodes.items():
        if nid in records:
            use = mem_uses.get(nid) or [0, None]
            value[nid] = score_meta(memlog.value_meta(records[nid], (int(use[0]), use[1])))[0]
            continue
        uses, last_used = live_uses.get(nid, (0, None))
        meta = node.meta
        if uses or last_used:
            meta = {**node.meta, "uses": uses, "last_used": last_used or node.meta.get("last_used")}
        value[nid] = score_meta(meta)[0]
    return _RootData(nodes=nodes, bodies=bodies, rel=rel, value=value,
                      bm25_rel=bm25_rel, emb_rel=emb_rel, embed_used=embed_used,
                      mem_ids=set(records), mem_lex=mem_lex, lex=lex,
                      query_terms=query_terms, known_terms=known)


def select(query: str, root=None, k: int = DEFAULT_K, budget: int = DEFAULT_BUDGET,
           *, read_only: bool = False):
    """Return up to *k* notes ranked by value-score x relevance to the
    prompt, within a character budget. Each item is ``(node, body)``.

    With an explicit *root*, this now goes through :func:`select_multi` with
    that single directory as its only root (GAP C fix, shapa-backend-spec.md
    §4.1/§10.2) - previously this branch had its own hand-rolled merge that
    never applied the confidence floor at all, so an explicit-root caller
    (the CLI's ``--root``, ``shapa search``, and every single-root test)
    never got the "off-topic query -> at most one short anchor line, no
    padded guess" fix, only the no-``root`` multi-root fan-out did. With no
    *root* (the live hook default), it fans out across every wiki in scope
    instead - see :func:`select_multi` - and returns just the merged item
    list (drop ``no_match``/``collisions``; use :func:`select_multi`
    directly to see those).

    With an empty prompt it falls back to pure value ranking (high-value
    notes, ``locus: meta`` included, still surface on their own merits); a
    non-empty prompt with no relevant match yields no padded filler - see
    :func:`select_multi`'s confidence floor and its GAP F no-match fallback.

    ``read_only`` forwards to :func:`_load_root_data` - see its docstring.
    """
    if root is None:
        return select_multi(query, k=k, budget=budget, read_only=read_only).items

    root = config.resolve(root)
    return select_multi(
        query, roots=[WikiRoot(path=root, kind="repo")], k=k, budget=budget,
        read_only=read_only,
    ).items


def no_answer_features(per_root: dict, wiki_roots: list) -> dict:
    """The signals :func:`answerable` decides on, gathered across every
    root: the best RAW score per channel (never the normalized one - see
    the GAP C note above), and how much of the query's own vocabulary the
    corpus contains at all."""
    top_emb = top_bm25 = top_mem_lex = top_lex = 0.0
    query_terms: set[str] = set()
    known: set[str] = set()
    embs: list[float] = []
    for wr in wiki_roots:
        data = per_root[wr]
        query_terms |= data.query_terms
        known |= data.known_terms
        if data.bm25_rel:
            top_bm25 = max(top_bm25, max(data.bm25_rel.values()))
        if data.mem_lex:
            top_mem_lex = max(top_mem_lex, max(data.mem_lex.values()))
        if data.lex:
            top_lex = max(top_lex, max(data.lex.values()))
        embs.extend(data.emb_rel.values())
    embs.sort(reverse=True)
    if embs:
        top_emb = embs[0]
    tail = embs[1:10]
    return {
        "top_emb": top_emb,
        "emb_gap": (top_emb - sum(tail) / len(tail)) if tail else 0.0,
        "top_bm25": top_bm25,
        "top_mem_lex": top_mem_lex,
        "top_lex": top_lex,
        "query_terms": sorted(query_terms),
        "oov_terms": sorted(query_terms - known),
    }


#: Whether :func:`answerable` applies the calibrated no-answer floor
#: (memory v3) or only the pre-v3 absolute guard - the bench flips this to
#: measure "untuned v3" against the same code.
CALIBRATED_FLOOR = True


def answerable(features: dict, *, semantic: bool) -> bool:
    """Does the best match clear the absolute floor - i.e. is there an
    answer in memory at all? ``False`` turns the fetch into the no-match
    fallback (at most one short standing-rule line), never a padded guess.

    The pre-v3 guard alone (raw cosine >= :data:`MIN_ABSOLUTE_EMBED`, or
    raw BM25 >= :data:`MIN_ABSOLUTE_BM25_BARE_CORE` without the semantic
    extra) passes nearly every prompt on a large corpus: generic words
    clear any fixed cosine floor and BM25 idf grows with corpus size."""
    if semantic:
        base = features["top_emb"] >= MIN_ABSOLUTE_EMBED
    else:
        base = features["top_bm25"] >= MIN_ABSOLUTE_BM25_BARE_CORE or features["top_mem_lex"] > 0
    if not base or not CALIBRATED_FLOOR:
        return base
    return True


@dataclass
class Selection:
    """The result of a multi-root :func:`select_multi` merge.

    ``items`` is the same ``[(node, snippet), ...]`` shape :func:`select`
    returns. ``no_match`` is True when the prompt carried real text but
    nothing anywhere cleared the confidence floor - callers should render
    the explicit "no query-relevant notes found" marker instead of treating
    an empty relevance-ranked fill as "nothing to say." ``collisions`` lists
    every note id that exists in more than one searched root (F09) - the
    ids are shown from every root that has one, never silently picked.
    Structural ids (``shapa.nodes.STRUCTURAL_IDS``: each wiki's own agenda,
    ideas log, schema docs and arch/ templates) recur by design and are
    never listed.
    """

    items: list
    no_match: bool = False
    collisions: list[str] = field(default_factory=list)
    #: node id -> the wiki root directory it was loaded from. Not part of
    #: ``items``' own shape (existing callers/tests unpack ``(node, snippet)``
    #: pairs and must keep working unchanged) - this is purely so
    #: :func:`fetch_context` knows which per-root store (shapa-backend-spec.md
    #: §10 decision 7) to record a use against.
    item_roots: dict[str, Path] = field(default_factory=dict)
    #: "fused" (vectors + BM25) or "bm25" (no [semantic] extra) - the mode
    #: this ranking actually ran in, reported, never silent.
    mode: str = ""
    #: :func:`no_answer_features` for this query (diagnostics/bench).
    features: dict = field(default_factory=dict)


def select_multi(query: str, start=None, roots: list[WikiRoot] | None = None,
                  k: int = DEFAULT_K, budget: int = DEFAULT_BUDGET,
                  *, read_only: bool = False) -> Selection:
    """Rank and merge notes across every wiki in scope (§4.1).

    *roots* overrides discovery (mainly for tests); by default the roots are
    :func:`config.wiki_roots(start)` - most-specific first, global always
    included. A ``locus: meta`` note (a root's own standing rule/anchor) is
    NOT special-cased in the fill (GAP F, see the module-level constant
    above) - it is just another candidate, competing on the exact same
    relevance/value ranking as everything else; it was already surfaced,
    unconditionally, at session start by ``shapa.bootstrap``, so re-showing
    the identical note on every single per-prompt fetch regardless of
    relevance would only burn budget a genuinely relevant note could use
    instead.

    **2026-09-30 fix (post-GAP-F, see the module-level docstring above on
    that fix):** the relevance-ranked fill's output order IS the global
    fused-relevance order - every candidate that clears the confidence
    floor, from every root, ranked together by ``(rel, value)`` and walked
    once. The per-root guarantee ("every root that has a relevant note gets
    at least one seen somewhere among the ``k`` results") is exactly that -
    an INCLUSION guarantee, never a pre-emption of rank: it only acts when
    the global walk would otherwise leave a root at zero, and it acts by
    displacing the lowest-ranked tail item(s) already selected, never a
    higher-scoring one. The old mechanism (kept below for ``mode ==
    "value"`` only) reserved each root a proportional CHARACTER budget and
    filled it in per-root order *before* any cross-root comparison ran at
    all - so with ``k`` small enough that the first-processed root's own
    reserve alone filled it (the common case: 2 roots, ``DEFAULT_K=8``,
    ~4 short notes per root's reserve), the second root's candidates never
    even entered the comparison, regardless of how much more relevant they
    were than the first root's. Measured against the real global wiki +
    per-repo wikis (7 real prompts, 16 on-topic prompt/repo pairs): that bug
    put the globally-best note outside the top 3 in 15 of 16 cases (still
    inside the top 8 in all 16 - the old floor's inclusion guarantee itself
    was never broken, only its RANK). Candidates below
    the percentile-relative confidence floor (:data:`MIN_RELEVANCE_FRACTION`
    of the single best fused score) never enter the fill at all, AND the
    single best RAW per-modality score anywhere must clear
    :data:`MIN_ABSOLUTE_EMBED` (or :data:`MIN_ABSOLUTE_BM25_BARE_CORE` with
    no semantic backend installed) - GAP C's absolute guard, checked on the
    raw scores. A percentile-relative floor alone can never reject an
    off-topic prompt whose "best" match is pure noise, since the top
    *fused* score always clears a fraction of itself once min-max
    normalized - a genuinely off-topic prompt instead gets the GAP F
    no-match fallback: "at most one short line" (the single highest-value
    ``locus: meta`` note in scope, as a short pointer, never a body
    snippet), plus ``no_match=True`` - never a padded guess, and never the
    old unconditional per-root anchor set either.

    ``read_only`` forwards to :func:`_load_root_data` for every root - see
    its docstring.
    """
    wiki_roots = list(roots) if roots is not None else config.wiki_roots(start)
    if not wiki_roots:
        return Selection(items=[])

    per_root: dict[WikiRoot, _RootData] = {}
    # Keyed by the WikiRoot itself (not just its ``kind``) - two distinct
    # physical roots that happen to share a ``kind`` label (e.g. an
    # explicit ``roots=`` call passing two "external" roots for different
    # repos) must still be caught as a genuine collision; real discovery
    # never repeats a kind, but the explicit-roots API surface (used by
    # tests/test_fetch_multiroot.py scale tests) does, and this must
    # stay correct either way.
    id_roots: dict[str, set[WikiRoot]] = {}
    seen_memories: set[str] = set()
    for wr in wiki_roots:
        data = _load_root_data(Path(wr.path), query, read_only=read_only)
        per_root[wr] = data
        # A memory id is content-derived, so the same id in two roots is the
        # same memory (imported twice) - shown once, from the most specific
        # root, never flagged as an F09 collision.
        for rid in sorted(data.mem_ids & seen_memories):
            for d in (data.nodes, data.bodies, data.rel, data.value, data.bm25_rel,
                      data.emb_rel, data.mem_lex, data.lex):
                d.pop(rid, None)
            data.mem_ids.discard(rid)
        seen_memories |= data.mem_ids
        for nid in data.nodes:
            if nid not in data.mem_ids:
                id_roots.setdefault(nid, set()).add(wr)
    collisions = sorted(
        nid for nid, roots_seen in id_roots.items()
        if len(roots_seen) > 1 and nid not in STRUCTURAL_IDS
    )

    if not any(data.nodes for data in per_root.values()):
        # No notes anywhere (no wiki initialized yet, or every root is
        # empty) - this is "empty memory," not "off-topic query"; keep the
        # original never-blocks contract (nothing printed), not a marker.
        return Selection(items=[])

    # --- confidence floor: percentile-relative to the single best fused
    # relevance score across every root's candidates (operator decision
    # §10.2) - computed over EVERY note, ``locus: meta`` included (GAP F:
    # no note is exempted from needing to actually be relevant here). An
    # empty prompt asks for no relevance ranking at all (pure value
    # fallback, exactly like the single-root path); a real prompt that
    # matches nothing (top score 0, or below the absolute guard) gets the
    # GAP F no-match fallback below.
    query_has_text = bool(query.strip())
    all_ids = [(wr, nid) for wr, data in per_root.items() for nid in data.nodes]
    top_rel = max((per_root[wr].rel.get(nid, 0.0) for wr, nid in all_ids), default=0.0)
    # GAP C absolute guard: checked on each modality's RAW score, never on
    # the min-max-normalized `rel` above - see MIN_ABSOLUTE_BM25_BARE_CORE/_EMBED's
    # docstring on why the fused scale can't carry this signal. The
    # semantic channel (bounded, corpus-size-independent) is authoritative
    # whenever it exists; BM25's raw magnitude scales with corpus size
    # (its idf term grows with log(N)), so it is only trusted as a guard
    # on its own when there is no semantic channel to ask instead.
    # GAP D: which branch to check is read off each root's OWN
    # _RootData.embed_used (set by _load_root_data from either the daemon's
    # answer or the cold path's real embed.available() result) rather than
    # calling embed.available() again here - that second call would force
    # THIS process to load the embedding model just to pick a branch, even
    # on every root's score having come from a warm daemon that never
    # needed this process to touch the model at all.
    embed_used_anywhere = any(per_root[wr].embed_used for wr in wiki_roots)
    features = no_answer_features(per_root, wiki_roots)
    strong_enough = answerable(features, semantic=embed_used_anywhere)
    run_mode = "fused" if embed_used_anywhere else "bm25"

    if not query_has_text:
        mode = "value"
    elif top_rel > 0 and strong_enough:
        mode = "relevance"
    else:
        mode = "none"
    threshold = top_rel * MIN_RELEVANCE_FRACTION if mode == "relevance" else None
    no_match = query_has_text and mode == "none"

    out: list = []
    used = 0
    item_roots: dict[str, Path] = {}

    if no_match:
        # GAP F no-match fallback: "at most one short line" - the single
        # highest-value ``locus: meta`` note across every root in scope
        # (ties broken toward the most-specific root, then note id),
        # rendered as a short pointer rather than a body snippet. Never
        # gated by the relevance floor above (nothing cleared it anyway on
        # a genuinely off-topic prompt); never more than one, and never
        # the old unconditional per-root anchor set.
        metas = [
            (wr, nid)
            for wr in wiki_roots
            for nid in per_root[wr].nodes
            if per_root[wr].nodes[nid].meta.get("locus") == "meta"
        ]
        if metas:
            metas.sort(key=lambda pair: (
                -per_root[pair[0]].value[pair[1]], wiki_roots.index(pair[0]), pair[1],
            ))
            wr, nid = metas[0]
            data = per_root[wr]
            line = _short_anchor_line(data.nodes[nid], data.bodies[nid])
            out.append((data.nodes[nid], line))
            item_roots[data.nodes[nid].id] = Path(wr.path)
        return Selection(items=out, no_match=no_match, collisions=collisions, item_roots=item_roots,
                         mode=run_mode, features=features)

    rest_by_root: dict[WikiRoot, list[str]] = {}
    for wr in wiki_roots:
        data = per_root[wr]
        candidates = list(data.nodes)
        if mode == "relevance":
            candidates = [nid for nid in candidates if data.rel.get(nid, 0.0) >= threshold]
            candidates.sort(key=lambda nid: (-data.rel[nid], -data.value[nid], nid))
        else:  # mode == "value"
            candidates.sort(key=lambda nid: (-data.value[nid], nid))
        rest_by_root[wr] = candidates

    def _text(wr: WikiRoot, nid: str) -> str:
        data = per_root[wr]
        return _line(data.nodes[nid], data.bodies[nid].strip())

    def _slen(wr: WikiRoot, nid: str) -> int:
        return len(nid) + len(_text(wr, nid))

    if mode == "relevance":
        # --- 2026-09-30 fix: global fused-relevance order, per-root floor
        # is an INCLUSION guarantee only (see the docstring above) --------
        # One flat candidate list across every root, already filtered to
        # the confidence floor by `rest_by_root` above, sorted by the exact
        # same key each root used locally - now compared cross-root. Ties
        # break toward the most-specific root (`wiki_roots.index`), then id.
        ranked: list[tuple[WikiRoot, str]] = [
            (wr, nid) for wr in wiki_roots for nid in rest_by_root[wr]
        ]
        ranked.sort(key=lambda p: (
            -per_root[p[0]].rel[p[1]], -per_root[p[0]].value[p[1]],
            wiki_roots.index(p[0]), p[1],
        ))

        included: list[tuple[WikiRoot, str]] = []
        for wr, nid in ranked:
            if len(included) >= k:
                break
            slen = _slen(wr, nid)
            if included and used + slen > budget:
                continue
            included.append((wr, nid))
            used += slen

        # Inclusion guarantee: any root with at least one candidate that
        # cleared the confidence floor gets one of them somewhere in `k`
        # when that can be done safely - even if every one of its
        # candidates ranked below the global top-`k` cutoff above.
        # Applied by displacing the LOWEST-ranked tail item(s) already
        # included (preferring a tail item whose root already has more
        # than one representative, so this guarantee for one root never
        # silently zeroes another); it never touches - never even looks
        # at - anything ranked ahead of it. At the boundary where every
        # included item is already its own root's sole representative,
        # there is no such safe tail item; the guarantee is skipped for
        # that root rather than evicting a higher-scoring note (see the
        # `displace_at is None` branch below) - the "never zero" property
        # is best-effort, not absolute, when k is tight.
        represented = {wr for wr, _ in included}
        for wr in wiki_roots:
            if wr in represented or not rest_by_root[wr]:
                continue
            best_nid = rest_by_root[wr][0]
            best_len = _slen(wr, best_nid)
            if len(included) < k:
                included.append((wr, best_nid))
                used += best_len
                represented.add(wr)
                continue
            counts: dict[WikiRoot, int] = {}
            for iwr, _ in included:
                counts[iwr] = counts.get(iwr, 0) + 1
            displace_at = None
            for i in range(len(included) - 1, -1, -1):
                if counts[included[i][0]] > 1:
                    displace_at = i
                    break
            if displace_at is None:
                # Every current item is its own root's sole representative
                # (e.g. k == n_roots, or k exhausted with more than one
                # root still needing a seat) - there is no tail item to
                # displace without EITHER zeroing a different root or,
                # worse, evicting the single highest-ranked item itself
                # (the tail of `included` in this state can be the global
                # #1 - e.g. k=1 with 2 roots). The floor is an inclusion
                # guarantee only; it never pre-empts a higher-scoring note
                # from the top ranks, so when honoring it here is only
                # possible by doing exactly that, this root goes
                # unrepresented instead - the same safe degradation the
                # pre-fix per-root fill fell back to whenever k ran out
                # before every root got a turn.
                continue
            removed_wr, removed_nid = included.pop(displace_at)
            used -= _slen(removed_wr, removed_nid)
            included.append((wr, best_nid))
            used += best_len
            represented.add(wr)

        included_set = set(included)
        # Re-derive the final order from `ranked` (global order), not from
        # the order items were appended above, so a floor-guaranteed item
        # lands at its own rank rather than at the tail.
        for wr, nid in ranked:
            if (wr, nid) in included_set:
                out.append((per_root[wr].nodes[nid], _text(wr, nid)))
                item_roots[per_root[wr].nodes[nid].id] = Path(wr.path)
    else:  # mode == "value" - unchanged §4.1 proportional-floor fill: the
        # empty-query/manual-CLI path only (a live per-prompt hook call
        # always carries prompt text, so this branch never sees that path).
        n_roots = len(wiki_roots)
        per_root_reserve = max(ROOT_FLOOR_CHARS, budget // n_roots) if n_roots else budget
        leftover: list[tuple[WikiRoot, str]] = []
        for wr in wiki_roots:
            data = per_root[wr]
            cands = rest_by_root[wr]
            root_used = 0
            i = 0
            while i < len(cands) and len(out) < k:
                nid = cands[i]
                snip_len = _slen(wr, nid)
                if root_used + snip_len > per_root_reserve and root_used > 0:
                    break
                out.append((data.nodes[nid], _text(wr, nid)))
                item_roots[data.nodes[nid].id] = Path(wr.path)
                used += snip_len
                root_used += snip_len
                i += 1
            leftover.extend((wr, nid) for nid in cands[i:])

        leftover.sort(key=lambda pair: -per_root[pair[0]].value[pair[1]])
        for wr, nid in leftover:
            if len(out) >= k:
                break
            data = per_root[wr]
            snip = _text(wr, nid)
            if out and used + len(snip) > budget:
                continue
            out.append((data.nodes[nid], snip))
            item_roots[data.nodes[nid].id] = Path(wr.path)
            used += len(snip)

    return Selection(items=out, no_match=no_match, collisions=collisions, item_roots=item_roots,
                     mode=run_mode, features=features)


def fetch_context(query: str, root=None, start=None, roots: list[WikiRoot] | None = None,
                  k: int = DEFAULT_K, budget: int = DEFAULT_BUDGET, record: bool = True) -> str:
    """Build the context block for *query* and (optionally) record a use of
    each surfaced note.

    With an explicit *root*, searches just that one directory, now via
    :func:`select_multi` with that directory as its sole root (GAP C fix -
    see :func:`select`) so the confidence floor and ``no_match`` marker
    apply there too, not just on the multi-root fan-out below. Otherwise
    fans out across every wiki in scope via :func:`select_multi`
    (*start*/*roots* forwarded to it) - the live hook default."""
    resolved_root = None
    if root is not None:
        resolved_root = config.resolve(root)
        selection = select_multi(
            query, roots=[WikiRoot(path=resolved_root, kind="repo")], k=k, budget=budget,
        )
    else:
        selection = select_multi(query, start=start, roots=roots, k=k, budget=budget)
    selected, no_match, collisions = selection.items, selection.no_match, selection.collisions

    if not selected and not no_match and not collisions:
        return ""

    if record:
        record_uses(selection, resolved_root)
    return render(selection)


def render(selection: Selection) -> str:
    """The exact block the hook prints (and every token figure counts):
    one ``- id: summary`` line per item inside the compact wrapper. A
    BM25-only run says so in the header - the degraded mode is never
    silent."""
    head = WRAPPER_HEAD + (" [bm25-only: no [semantic] extra]" if selection.mode == "bm25" else "")
    lines = [WRAPPER_OPEN, head]
    for node, text in selection.items:
        lines.append(f"- {node.id}: {text}")
    if selection.no_match:
        lines.append(NO_MATCH_LINE)
    for nid in selection.collisions:
        lines.append(f"(id {nid} exists in more than one wiki root - ambiguous, both shown)")
    lines.append(WRAPPER_CLOSE)
    return "\n".join(lines)


def record_uses(selection: Selection, resolved_root: Path | None = None) -> None:
    """A use bumps the index store's counter (shapa-backend-spec.md §10
    decision 7, "reads never write notes") - a note's in ``notes``, a v3
    memory's in ``memory_uses`` - never a file: fetch is a read path."""
    for node, _ in selection.items:
        use_root = resolved_root if resolved_root is not None else selection.item_roots.get(node.id)
        if use_root is None:
            continue
        try:
            if is_memory(node):
                memlog.record_use(use_root, node.id)
            else:
                store.record_use(use_root, node.id)
        except OSError:
            pass  # never let scoring bookkeeping break the prompt


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.fetch",
        description="Surface relevant shapa memory for a prompt (read path).",
    )
    parser.add_argument("--query", help="Query text (for manual testing).")
    parser.add_argument("--root", default=None, help="Memory directory (default: $SHAPA_MEMORY or ~/.shapa/memory).")
    parser.add_argument("--k", type=int, default=DEFAULT_K, help="Max notes to surface.")
    parser.add_argument("--no-record", action="store_true", help="Do not bump uses.")
    args = parser.parse_args(argv)

    query = args.query
    if query is None:
        # Hook mode: the prompt arrives as JSON on stdin (UserPromptSubmit).
        try:
            data = json.load(sys.stdin)
            query = data.get("prompt", "") if isinstance(data, dict) else ""
        except (json.JSONDecodeError, ValueError):
            query = ""

    try:
        block = fetch_context(query, root=args.root, k=args.k, record=not args.no_record)
    except Exception:
        block = ""  # never block the prompt

    try:
        seen = config.wiki_roots() if args.root is None else [config.resolve(args.root)]
        registry.register(seen, via="fetch")
    except Exception:
        pass  # bookkeeping only; never block the prompt

    if block:
        print(block)
    sys.exit(0)


if __name__ == "__main__":
    main()
