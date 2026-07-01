"""Wiki file model and the link graph.

Every file in the wiki is a node with uniform frontmatter (id, type, created,
consequence, locus, uses) and a rich markdown body. Connections are Obsidian
``[[wikilinks]]`` in the body - the same links Obsidian renders in its graph
view - so there is one coherent style across ``arch/`` and ``memory/``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from shapa import frontmatter

# Matches [[target]], [[target|alias]], [[target#heading]]; captures the target.
_WIKILINK_RE = re.compile(r"\[\[\s*([^\]\|#\n]+?)\s*(?:[#|][^\]]*)?\]\]")

# Curated design/spec docs (the `arch/` cluster, installed by `shapa init`).
# The maintainer never prunes or auto-merges these - they are authored material,
# not ephemeral operational memory, so they are exempt from orphan/stale pruning
# and duplicate-merging.
PROTECTED_TYPES = frozenset({"reference"})


def is_protected(node: "Node") -> bool:
    """True when *node* is curated material the maintainer must never delete."""
    return str(node.meta.get("type", "")) in PROTECTED_TYPES


def extract_links(body: str) -> set[str]:
    """Return the set of wikilink targets (by id/stem) found in *body*."""
    return {m.strip() for m in _WIKILINK_RE.findall(body) if m.strip()}


@dataclass
class Node:
    id: str
    type: str
    path: Path
    meta: dict = field(default_factory=dict)
    outlinks: set[str] = field(default_factory=set)


@dataclass
class Graph:
    nodes: dict[str, Node]      # real files only (these can be pruned)
    edges: dict[str, set[str]]  # undirected adjacency by id, INCLUDING phantom
                                # topic/type targets that have no file yet


def load_nodes(root) -> dict[str, Node]:
    """Load every ``*.md`` under *root* (recursively) as a Node.

    The node id is the frontmatter ``id`` if present, else the filename stem.
    """
    root = Path(root)
    paths = sorted(root.rglob("*.md")) if root.is_dir() else [root]
    nodes: dict[str, Node] = {}
    for p in paths:
        parsed = frontmatter.parse(p)
        if parsed.error:
            continue
        node_id = str(parsed.meta.get("id") or p.stem)
        nodes[node_id] = Node(
            id=node_id,
            type=str(parsed.meta.get("type", "")),
            path=p,
            meta=parsed.meta,
            outlinks=extract_links(parsed.body),
        )
    return nodes


def build_graph(nodes: dict[str, Node]) -> Graph:
    """Build the undirected link graph from body wikilinks.

    Decentralized model: a wikilink to a *topic* or *type* (e.g. ``[[git]]``,
    ``[[rule]]``) counts as connectivity even when no file by that name exists
    yet. Those targets become *phantom* nodes - they cluster notes around a
    shared topic (exactly as Obsidian renders unresolved links) but are never
    pruned (only real files in ``nodes`` are). A real note is an orphan only
    when it has no link in or out at all.
    """
    edges: dict[str, set[str]] = {nid: set() for nid in nodes}
    for nid, node in nodes.items():
        for target in node.outlinks:
            if target == nid:
                continue
            edges.setdefault(nid, set()).add(target)
            edges.setdefault(target, set()).add(nid)  # phantom target gets an entry
    return Graph(nodes=nodes, edges=edges)
