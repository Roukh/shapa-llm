"""Reciprocal Rank Fusion - shapa-backend-spec.md §5.

    score(d) = Σ 1/(k+rank_i(d))    over every ranking d appears in
                                     (rank 1 = best in that ranking)

Rank-position semantics are scale-invariant across wildly different scoring
backends - BM25's unbounded, corpus-dependent scores vs. cosine similarity's
bounded [-1, 1] range can never be compared as raw numbers, but "2nd most
relevant of 40 candidates" means the same thing regardless of which backend
said so. This is what lets ``fetch.py`` let "both vote whenever both exist"
(§5) instead of the old ``if embed.available(): cosine else: bm25`` either/or
branch: BM25 and the embedding backend (``shapa.embed``) each rank the notes
they consider relevant, and :func:`rrf` fuses the two rankings into one score
per note, degrading gracefully to whichever single ranking exists otherwise.

A ranking is a plain ``{id: score}`` dict (higher = more relevant) - the same
shape ``shapa.bm25.bm25_scores`` and ``fetch.py``'s cosine dict already use,
so no caller needs to build a separate "ranked list" representation first.
Only entries with a *positive* score are treated as "retrieved" by that
ranking (see :func:`rrf`) - an id merely present in a dict at 0.0 (BM25's
"no shared terms" value, or a filtered-out embedding score) contributes
nothing from that source, so a fused score of 0.0 still cleanly means "no
ranking found this relevant," matching every existing zero-relevance/
confidence-floor check in ``fetch.py`` (which is itself percentile-relative,
per shapa-backend-spec.md §10 decision 2, precisely so it stays correct
regardless of which backend(s) contributed).
"""

from __future__ import annotations

#: The standard RRF constant - large enough that a single ranking's top few
#: results dominate the fused score (rank 1 vs. rank 2 differ by
#: 1/61 - 1/62 ≈ 0.00026, small in absolute terms but still the largest gap
#: in the series), while still letting an item ranked further down in one
#: list but well-ranked in another accumulate a comparable total. This is
#: the constant used in the original RRF paper (Cormack et al., 2009) and
#: in most production hybrid-search implementations; shapa has no corpus of
#: its own large enough to justify recalibrating it.
RRF_K = 60


def _ranks(scores: dict[str, float]) -> dict[str, int]:
    """1-based rank of each id in *scores*, best (highest) score first.

    Ties break on id for a deterministic order (matches every other
    tie-break in this codebase - fetch.py's own sorts do the same)."""
    order = sorted(scores, key=lambda nid: (-scores[nid], nid))
    return {nid: i + 1 for i, nid in enumerate(order)}


def rrf(rankings: list[dict[str, float]], k: int = RRF_K) -> dict[str, float]:
    """Fuse any number of ``{id: score}`` rankings into one ``{id: score}``
    via Reciprocal Rank Fusion.

    Only the entries with a score strictly greater than 0 in a given ranking
    are considered "retrieved" by it and receive a rank within it - an id
    tied at 0.0 with everything else in a ranking (no evidence at all from
    that source) never gets an artificial rank-and-therefore-nonzero term
    just because every other id also scored 0.0. An id absent from a
    ranking's dict entirely is equivalent to a 0.0 score there. A ranking
    with no positive entries (or an empty dict) contributes nothing and is
    skipped. An id that clears the bar in more than one ranking naturally
    accumulates more terms, rewarding agreement between backends - an id
    only one ranking retrieved still comes through on that ranking's term
    alone (graceful degradation to single-backend behavior).

    Returns a dict containing only ids that were positively retrieved by at
    least one input ranking - never a full dict padded with 0.0 for every
    id in the corpus, so ``rel.get(nid, 0.0)`` at the call site still means
    exactly what it always has.
    """
    fused: dict[str, float] = {}
    for scores in rankings:
        positive = {nid: s for nid, s in scores.items() if s > 0}
        if not positive:
            continue
        for nid, rank in _ranks(positive).items():
            fused[nid] = fused.get(nid, 0.0) + 1.0 / (k + rank)
    return fused
