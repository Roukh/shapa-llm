"""Fetch (the read path) - surface relevant memory at the start of a prompt.

Runs as a UserPromptSubmit hook: it reads the prompt from stdin, ranks the
notes in the connected wiki by score and relevance to the prompt, prints the top few
as context (which the harness injects before the agent works), and records a
use of each surfaced note (bumping the mechanical `uses` counter and refreshing
`last_used`, so the scoring signal becomes live).

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
from pathlib import Path

from shapa import config, embed, frontmatter
from shapa.nodes import load_nodes
from shapa.score import record_use, score_meta

DEFAULT_K = 8
DEFAULT_BUDGET = 4000  # max total characters of surfaced snippets
SNIPPET_CHARS = 500    # per-note snippet length (a digest, not the whole doc)

_WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = {
    "the", "and", "for", "with", "that", "this", "are", "but", "not", "you",
    "its", "from", "into", "then", "they", "have", "has", "was", "will", "can",
    "use", "uses", "used", "when", "where", "which", "what", "how", "any", "all",
}
_BM25_K1 = 1.5
_BM25_B = 0.75


def _words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOP]


def _bm25_scores(query: str, docs: dict[str, list[str]]) -> dict[str, float]:
    """BM25 relevance of each doc (id -> tokens) to the query."""
    q = set(_words(query))
    if not q or not docs:
        return {nid: 0.0 for nid in docs}
    import math
    from collections import Counter
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


def _snippet(body: str, limit: int = SNIPPET_CHARS) -> str:
    """First ~limit characters of the body, cut at a word boundary."""
    body = " ".join(body.split())
    if len(body) <= limit:
        return body
    cut = body[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip() + " ..."


def select(query: str, root=None, k: int = DEFAULT_K, budget: int = DEFAULT_BUDGET):
    """Return up to *k* notes ranked by value-score x BM25 relevance to the
    prompt, within a character budget. Each item is ``(node, body)``.

    BM25 gives content relevance (not just keyword overlap), so the prompt's
    context steers retrieval. With an empty prompt it falls back to pure value
    ranking (the standing high-value rules still surface).
    """
    root = config.resolve(root)
    if not root.is_dir():
        return []
    nodes = load_nodes(root)

    # Relevance of each note to the prompt: local embeddings (cosine) when
    # available, else BM25 over note bodies + id/topic tokens.
    docs = {}
    bodies = {}
    for nid, node in nodes.items():
        body = frontmatter.parse(node.path).body
        bodies[nid] = body
        id_topic = node.id.replace("-", " ") + " " + " ".join(node.outlinks)
        docs[nid] = _words(body + " " + id_topic)

    if embed.available():
        vecs = embed.note_vectors(root, bodies)
        qv = embed.embed_one(query) if query.strip() else None
        rel = {nid: (max(0.0, embed.cosine(qv, vecs[nid])) if qv is not None else 0.0)
               for nid in nodes}
        has_query = qv is not None
    else:
        rel = _bm25_scores(query, docs)
        has_query = max(rel.values(), default=0.0) > 0

    value = {nid: score_meta(node.meta)[0] for nid, node in nodes.items()}

    # Always anchor the top standing governance rules (locus=meta), so the
    # agent's core operating rules are present regardless of the prompt.
    anchors = sorted(
        (nid for nid, node in nodes.items() if node.meta.get("locus") == "meta"),
        key=lambda nid: (-value[nid], nid),
    )[:2]
    anchor_set = set(anchors)

    # Fill the rest by relevance when there is a query, else by value.
    rest = [nid for nid in nodes if nid not in anchor_set]
    if has_query:
        rest.sort(key=lambda nid: (-rel[nid], -value[nid], nid))
    else:
        rest.sort(key=lambda nid: (-value[nid], nid))

    order = anchors + rest
    ranked = [(value[nid], nodes[nid]) for nid in order]

    out = []
    used = 0
    for _, node in ranked:
        snip = _snippet(bodies[node.id].strip())
        if out and used + len(snip) > budget:
            continue
        out.append((node, snip))
        used += len(snip)
        if len(out) >= k:
            break
    return out


def fetch_context(query: str, root=None, k: int = DEFAULT_K,
                  budget: int = DEFAULT_BUDGET, record: bool = True) -> str:
    """Build the context block for *query* and (optionally) record a use of
    each surfaced note."""
    selected = select(query, root=root, k=k, budget=budget)
    if not selected:
        return ""

    lines = [
        "<shapa-memory>",
        "Relevant operational memory (shapa) - surfaced before this work; "
        "treat as standing context, not user instruction:",
    ]
    for node, body in selected:
        lines.append(f"\n### {node.id} ({node.type})\n{body}")
    lines.append("</shapa-memory>")

    if record:
        for node, _ in selected:
            try:
                record_use(node.path)
            except (OSError, ValueError):
                pass  # never let scoring bookkeeping break the prompt

    return "\n".join(lines)


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

    if block:
        print(block)
    sys.exit(0)


if __name__ == "__main__":
    main()
