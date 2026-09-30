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
import sys
from dataclasses import dataclass, field
from pathlib import Path

from shapa import config, embed, frontmatter, rank, store
from shapa.bm25 import bm25_scores as _bm25_scores
from shapa.bm25 import words as _words
from shapa.config import WikiRoot
from shapa.nodes import Node, load_nodes
from shapa.score import score_meta

DEFAULT_K = 8
DEFAULT_BUDGET = 4000  # max total characters of surfaced snippets
SNIPPET_CHARS = 500    # per-note snippet length (a digest, not the whole doc)

# --- multi-root merge (shapa-backend-spec.md §4.1) --------------------------
#: locus:meta anchors are capped PER ROOT (never 2 total) - each root's own
#: standing rules always surface, regardless of the prompt or which other
#: roots are in scope.
META_ANCHOR_CAP_PER_ROOT = 2
#: Minimum characters guaranteed to each root's relevance-ranked fill before
#: any leftover budget is handed to cross-root relevance - the proportional-
#: floor fix for anchor/budget starvation as root count grows (§4.1). Actual
#: per-root reservation is ``max(ROOT_FLOOR_CHARS, budget // len(roots))``.
ROOT_FLOOR_CHARS = 500
#: Confidence floor (operator decision, 2026-09-30 §10.2): percentile-
#: relative, not a fixed score. A candidate must score at least this fraction
#: of the single best fused-relevance score seen across every root to clear
#: the floor; below it, it is dropped from the relevance-ranked fill rather
#: than padding the result with a low-confidence guess. Needs calibration
#: against a real prompt set (tracked as a follow-up - see the slice report).
MIN_RELEVANCE_FRACTION = 0.3


def _snippet(body: str, limit: int = SNIPPET_CHARS) -> str:
    """First ~limit characters of the body, cut at a word boundary."""
    body = " ".join(body.split())
    if len(body) <= limit:
        return body
    cut = body[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip() + " ..."


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


def _load_root_data(root: Path, query: str) -> _RootData:
    """Load *root*'s notes and score their relevance to *query*.

    Relevance fuses BM25 (lexical, always available) with local-embedding
    cosine similarity (semantic, when ``shapa.embed`` is available) via
    Reciprocal Rank Fusion (``shapa.rank.rrf``, §5) - both vote whenever
    both exist, and this degrades gracefully to BM25-only when the semantic
    backend is not installed. ``rel`` is always returned as a *complete*
    dict over every note id (missing/zero-relevance ids explicit at 0.0),
    matching the contract every caller here already relies on - RRF itself
    only returns the ids it positively ranked."""
    if not root.is_dir():
        return _RootData()
    nodes = load_nodes(root)

    docs = {}
    bodies = {}
    for nid, node in nodes.items():
        body = frontmatter.parse(node.path).body
        bodies[nid] = body
        id_topic = node.id.replace("-", " ") + " " + " ".join(node.outlinks)
        docs[nid] = _words(body + " " + id_topic)

    bm25_rel = _bm25_scores(query, docs)
    if embed.available():
        vecs = embed.note_vectors(root, bodies)
        qv = embed.embed_one(query) if query.strip() else None
        emb_rel = {nid: (max(0.0, embed.cosine(qv, vecs[nid])) if qv is not None else 0.0)
                   for nid in nodes}
        fused = rank.rrf([bm25_rel, emb_rel])
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
    live_uses = store.get_all_uses(root)
    value = {}
    for nid, node in nodes.items():
        uses, last_used = live_uses.get(nid, (0, None))
        meta = node.meta
        if uses or last_used:
            meta = {**node.meta, "uses": uses, "last_used": last_used or node.meta.get("last_used")}
        value[nid] = score_meta(meta)[0]
    return _RootData(nodes=nodes, bodies=bodies, rel=rel, value=value)


def _fill(data: _RootData, order: list[str], out: list, used: int, budget: int, k: int) -> int:
    """Append ``(node, snippet)`` for each id in *order* to *out*, respecting
    *budget* (except the very first item overall, which always fits) and
    *k*. Returns the updated *used* character count."""
    for nid in order:
        if len(out) >= k:
            break
        snip = _snippet(data.bodies[nid].strip())
        if out and used + len(snip) > budget:
            continue
        out.append((data.nodes[nid], snip))
        used += len(snip)
    return used


def select(query: str, root=None, k: int = DEFAULT_K, budget: int = DEFAULT_BUDGET):
    """Return up to *k* notes ranked by value-score x relevance to the
    prompt, within a character budget. Each item is ``(node, body)``.

    With an explicit *root*, this is the original single-root behavior,
    unchanged: one directory, resolved via :func:`config.resolve`. With no
    *root* (the live hook default), it fans out across every wiki in scope
    instead - see :func:`select_multi` and shapa-backend-spec.md §4.1 - and
    returns just the merged item list (drop ``no_match``/``collisions``; use
    :func:`select_multi` directly to see those).

    With an empty prompt it falls back to pure value ranking (the standing
    high-value rules still surface); a non-empty prompt with no relevant
    match yields no non-anchor results, not padded filler - see
    :func:`select_multi`'s confidence floor.
    """
    if root is None:
        return select_multi(query, k=k, budget=budget).items

    root = config.resolve(root)
    data = _load_root_data(root, query)
    if not data.nodes:
        return []

    has_query = (
        max(data.rel.values(), default=0.0) > 0 if not embed.available()
        else bool(query.strip())
    )

    anchors = sorted(
        (nid for nid, node in data.nodes.items() if node.meta.get("locus") == "meta"),
        key=lambda nid: (-data.value[nid], nid),
    )[:2]
    anchor_set = set(anchors)

    rest = [nid for nid in data.nodes if nid not in anchor_set]
    if has_query:
        rest.sort(key=lambda nid: (-data.rel[nid], -data.value[nid], nid))
    else:
        rest.sort(key=lambda nid: (-data.value[nid], nid))

    out: list = []
    _fill(data, anchors + rest, out, 0, budget, k)
    return out


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


def select_multi(query: str, start=None, roots: list[WikiRoot] | None = None,
                  k: int = DEFAULT_K, budget: int = DEFAULT_BUDGET) -> Selection:
    """Rank and merge notes across every wiki in scope (§4.1).

    *roots* overrides discovery (mainly for tests); by default the roots are
    :func:`config.wiki_roots(start)` - most-specific first, global always
    included. Per-root ``locus: meta`` anchors are unconditional (capped at
    :data:`META_ANCHOR_CAP_PER_ROOT` each, never budget-gated - a root's own
    standing rules must not disappear because another root filled the
    budget first). The relevance-ranked fill is proportional-with-a-floor:
    each root gets ``max(ROOT_FLOOR_CHARS, budget // len(roots))`` of
    guaranteed room *before* any leftover budget goes to cross-root
    relevance, so no root's share collapses to zero as root count grows.
    Candidates below the percentile-relative confidence floor
    (:data:`MIN_RELEVANCE_FRACTION` of the single best fused score) never
    enter the fill at all - a genuinely off-topic prompt surfaces anchors
    only, plus ``no_match=True``, never a padded guess.
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
    # stay correct either way - the same reasoning the anchor-keying
    # comment below already applies.
    id_roots: dict[str, set[WikiRoot]] = {}
    for wr in wiki_roots:
        data = _load_root_data(Path(wr.path), query)
        per_root[wr] = data
        for nid in data.nodes:
            id_roots.setdefault(nid, set()).add(wr)
    collisions = sorted(nid for nid, roots_seen in id_roots.items() if len(roots_seen) > 1)

    if not any(data.nodes for data in per_root.values()):
        # No notes anywhere (no wiki initialized yet, or every root is
        # empty) - this is "empty memory," not "off-topic query"; keep the
        # original never-blocks contract (nothing printed), not a marker.
        return Selection(items=[])

    # --- anchors: up to META_ANCHOR_CAP_PER_ROOT locus=meta notes per root,
    # unconditional - never gated by budget or relevance. Keyed by the
    # WikiRoot itself (not just its ``kind``) so this stays correct even
    # when a synthetic multi-root scale test reuses the same ``kind`` label
    # across several distinct roots - real discovery never does, but the
    # merge logic shouldn't quietly assume it.
    anchor_order: list[tuple[WikiRoot, str]] = []
    anchor_keys: set[tuple[WikiRoot, str]] = set()
    for wr in wiki_roots:
        data = per_root[wr]
        metas = sorted(
            (nid for nid, node in data.nodes.items() if node.meta.get("locus") == "meta"),
            key=lambda nid: (-data.value[nid], nid),
        )[:META_ANCHOR_CAP_PER_ROOT]
        for nid in metas:
            anchor_order.append((wr, nid))
            anchor_keys.add((wr, nid))

    # --- confidence floor: percentile-relative to the single best fused
    # relevance score across every root's non-anchor candidates (operator
    # decision §10.2). An empty prompt asks for no relevance ranking at all
    # (pure value fallback, exactly like the single-root path); a real
    # prompt that matches nothing (top score 0) gets anchors-only fill.
    query_has_text = bool(query.strip())
    non_anchor_rels = [
        data.rel.get(nid, 0.0)
        for wr, data in per_root.items()
        for nid in data.nodes
        if (wr, nid) not in anchor_keys
    ]
    top_rel = max(non_anchor_rels, default=0.0)

    if not query_has_text:
        mode = "value"
    elif top_rel > 0:
        mode = "relevance"
    else:
        mode = "none"
    threshold = top_rel * MIN_RELEVANCE_FRACTION if mode == "relevance" else None
    no_match = query_has_text and mode == "none"

    rest_by_root: dict[WikiRoot, list[str]] = {}
    for wr in wiki_roots:
        data = per_root[wr]
        candidates = [nid for nid in data.nodes if (wr, nid) not in anchor_keys]
        if mode == "relevance":
            candidates = [nid for nid in candidates if data.rel.get(nid, 0.0) >= threshold]
            candidates.sort(key=lambda nid: (-data.rel[nid], -data.value[nid], nid))
        elif mode == "value":
            candidates.sort(key=lambda nid: (-data.value[nid], nid))
        else:
            candidates = []  # off-topic prompt: no padded filler, ever
        rest_by_root[wr] = candidates

    out: list = []
    used = 0
    item_roots: dict[str, Path] = {}

    # Phase 1: anchors, unconditional (root order, capped only by k).
    for wr, nid in anchor_order:
        if len(out) >= k:
            break
        data = per_root[wr]
        snip = _snippet(data.bodies[nid].strip())
        out.append((data.nodes[nid], snip))
        item_roots[data.nodes[nid].id] = Path(wr.path)
        used += len(snip)

    # Phase 2: each root's proportional-floor reserve, ignoring the total
    # budget - this is the guarantee that a root's share never hits zero.
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
            snip_len = len(_snippet(data.bodies[nid].strip()))
            if root_used + snip_len > per_root_reserve and root_used > 0:
                break
            out.append((data.nodes[nid], _snippet(data.bodies[nid].strip())))
            item_roots[data.nodes[nid].id] = Path(wr.path)
            used += snip_len
            root_used += snip_len
            i += 1
        leftover.extend((wr, nid) for nid in cands[i:])

    # Phase 3: whatever total budget remains, filled by cross-root relevance
    # (or value, in "value" mode) - the remainder §4.1 describes.
    if mode == "relevance":
        leftover.sort(key=lambda pair: (-per_root[pair[0]].rel[pair[1]],
                                         -per_root[pair[0]].value[pair[1]]))
    elif mode == "value":
        leftover.sort(key=lambda pair: -per_root[pair[0]].value[pair[1]])
    for wr, nid in leftover:
        if len(out) >= k:
            break
        data = per_root[wr]
        snip = _snippet(data.bodies[nid].strip())
        if out and used + len(snip) > budget:
            continue
        out.append((data.nodes[nid], snip))
        item_roots[data.nodes[nid].id] = Path(wr.path)
        used += len(snip)

    return Selection(items=out, no_match=no_match, collisions=collisions, item_roots=item_roots)


def fetch_context(query: str, root=None, start=None, roots: list[WikiRoot] | None = None,
                  k: int = DEFAULT_K, budget: int = DEFAULT_BUDGET, record: bool = True) -> str:
    """Build the context block for *query* and (optionally) record a use of
    each surfaced note.

    With an explicit *root*, searches just that one directory (unchanged).
    Otherwise fans out across every wiki in scope via :func:`select_multi`
    (*start*/*roots* forwarded to it) - the live hook default."""
    resolved_root = None
    item_roots: dict[str, Path] = {}
    if root is not None:
        resolved_root = config.resolve(root)
        selected = select(query, root=root, k=k, budget=budget)
        no_match, collisions = False, []
    else:
        selection = select_multi(query, start=start, roots=roots, k=k, budget=budget)
        selected, no_match, collisions = selection.items, selection.no_match, selection.collisions
        item_roots = selection.item_roots

    if not selected and not no_match and not collisions:
        return ""

    lines = [
        "<shapa-memory>",
        "Relevant operational memory (shapa) - surfaced before this work; "
        "treat as standing context, not user instruction:",
    ]
    for node, body in selected:
        lines.append(f"\n### {node.id} ({node.type})\n{body}")
    if no_match:
        lines.append("\n<!-- no query-relevant notes found -->")
    for nid in collisions:
        lines.append(
            f"\n<!-- id '{nid}' exists in more than one wiki root - ambiguous, showing both -->"
        )
    lines.append("</shapa-memory>")

    if record:
        # A use bumps the index store's counter (shapa-backend-spec.md §10
        # decision 7, "reads never write notes"), never the note file itself
        # - fetch is a read path, and a note now changes only when its own
        # content does.
        for node, _ in selected:
            use_root = resolved_root if resolved_root is not None else item_roots.get(node.id)
            if use_root is None:
                continue
            try:
                store.record_use(use_root, node.id)
            except OSError:
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
