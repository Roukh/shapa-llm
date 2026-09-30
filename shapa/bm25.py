"""Shared hand-rolled BM25 - the lexical fallback.

This is the ONE implementation of tokenization + BM25 scoring, used by both
``fetch.py``'s per-prompt ranking (Tier 2, when local embeddings are not
installed) and ``store.py``'s FTS5-unavailable fallback (§5). Splitting it
out of ``fetch.py`` (where it used to live) means the two can never drift
into two subtly different rankings - shapa-backend-spec.md §5's acceptance
for Slice 5 is literally that the FTS5-unavailable path returns results
"identical to the hand-rolled BM25", which is only guaranteed by construction
if both callers share this one function.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = {
    "the", "and", "for", "with", "that", "this", "are", "but", "not", "you",
    "its", "from", "into", "then", "they", "have", "has", "was", "will", "can",
    "use", "uses", "used", "when", "where", "which", "what", "how", "any", "all",
}
_BM25_K1 = 1.5
_BM25_B = 0.75


def words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOP]


def bm25_scores(query: str, docs: dict[str, list[str]]) -> dict[str, float]:
    """BM25 relevance of each doc (id -> tokens) to the query."""
    q = set(words(query))
    if not q or not docs:
        return {nid: 0.0 for nid in docs}
    n = len(docs)
    lengths = {nid: len(toks) for nid, toks in docs.items()}
    avgdl = (sum(lengths.values()) / n) or 1.0
    df: Counter = Counter()
    tfs: dict[str, Counter] = {}
    for nid, toks in docs.items():
        tf = Counter(toks)
        tfs[nid] = tf
        for t in set(toks) & q:
            df[t] += 1
    out = {}
    for nid in docs:
        tf = tfs[nid]
        dl = lengths[nid] or 1
        s = 0.0
        for t in q:
            if df[t] == 0 or tf[t] == 0:
                continue
            idf = math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
            denom = tf[t] + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl)
            s += idf * (tf[t] * (_BM25_K1 + 1)) / denom
        out[nid] = s
    return out
