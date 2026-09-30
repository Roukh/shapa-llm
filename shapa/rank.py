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


def minmax_normalize(scores: dict[str, float]) -> dict[str, float]:
    """Scale one ranking's raw scores to ``[0, 1]`` by dividing by its own
    top (positive) score.

    Only entries with a score strictly greater than 0 are "retrieved" by
    this ranking (same convention as :func:`rrf`) and get scaled; anything
    at 0.0 (or the whole ranking having no positive entry at all) stays
    0.0, so the "0.0 means no evidence from this source" contract every
    caller already relies on holds after normalization too. The top
    entry always lands at exactly 1.0, which is what lets :func:`fuse`
    compare a *magnitude*, not just a rank position, across two rankings
    on wildly different raw scales (BM25's unbounded score vs. cosine's
    bounded range) - the same scale-invariance :func:`rrf` gets from
    ranks, but keeping the shape of the distribution instead of flattening
    every gap to a fixed ``1/(k+rank)`` step.
    """
    top = max((s for s in scores.values() if s > 0), default=0.0)
    if top <= 0:
        return {nid: 0.0 for nid in scores}
    return {nid: (s / top if s > 0 else 0.0) for nid, s in scores.items()}


def fuse(rankings: list[dict[str, float]], weights: list[float] | None = None) -> dict[str, float]:
    """Fuse rankings via score-normalized (min-max) weighted combination -
    shapa-backend-spec.md GAP C's fix for RRF burying a strong single-
    modality match.

    :func:`rrf` fuses by rank *position* alone, which discards how much
    better rank 1 is than rank 15 - a note that is merely mediocre in two
    rankings (say, rank 15 in both BM25 and the embedding ranking) can
    outscore a note that is the single best match in exactly one ranking
    (rank 1, zero overlap in the other), because RRF sums two small
    ``1/(k+15)`` terms past one ``1/(k+1)`` term. That is exactly backwards
    for a semantic-only hit with no lexical overlap at all (e.g. a query
    that paraphrases a note's content in different words) - it is a
    genuinely strong match, not a weak one, and RRF's rank-only view has
    no way to tell the two apart.

    :func:`minmax_normalize` keeps each ranking's *relative magnitude*
    instead of collapsing it to a rank: a true standout normalizes to (or
    near) 1.0, a mediocre match normalizes far below it. Combining the
    normalized rankings with a weighted sum then lets one ranking's clear
    best result compete on its own merits against another ranking's
    lukewarm agreement, rather than always losing to "found by more than
    one source."

    *weights* default to equal weight per ranking (``[1.0, ...]``,
    renormalized over only the rankings that actually retrieved anything -
    a ranking with no positive entries contributes nothing and its weight
    is not wasted on an all-zero term, matching :func:`rrf`'s graceful
    degradation to whichever single ranking exists). An id need only clear
    the bar in ONE ranking to receive a nonzero fused score; ids never
    positively ranked by anything are omitted entirely (same contract as
    :func:`rrf` - see its own docstring on why callers must not rely on a
    full-id-universe dict coming back).
    """
    if weights is None:
        weights = [1.0] * len(rankings)
    active = [(w, scores) for w, scores in zip(weights, rankings)
              if any(s > 0 for s in scores.values())]
    total_w = sum(w for w, _ in active)
    if total_w <= 0:
        return {}
    fused: dict[str, float] = {}
    for w, scores in active:
        norm = minmax_normalize(scores)
        share = w / total_w
        for nid, s in norm.items():
            if s <= 0:
                continue
            fused[nid] = fused.get(nid, 0.0) + share * s
    return fused
