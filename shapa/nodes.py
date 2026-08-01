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
    """True when *node* is curated material the maintainer must never delete.

    Two independent signals, either one sufficient: the frontmatter contract
    (top-level ``type: reference``), or physical location under the wiki's
    ``arch/`` subdirectory (``node.in_arch``). The path signal exists because
    AGENTS.md documents arch/ protection as a location guarantee ("Files in
    arch/ ... are never pruned"), and that guarantee must hold even when a
    given arch/ file's frontmatter is missing or malformed - the frontmatter
    contract is easy for a writing agent to get wrong (absent entirely, or a
    `type` key nested under a non-standard block) and orphan pruning must not
    depend on it being right.
    """
    return node.in_arch or str(node.meta.get("type", "")) in PROTECTED_TYPES


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
    in_arch: bool = False


@dataclass
class Graph:
    nodes: dict[str, Node]      # real files only (these can be pruned)
    edges: dict[str, set[str]]  # undirected adjacency by id, INCLUDING phantom
                                # topic/type targets that have no file yet


def load_nodes(root) -> dict[str, Node]:
    """Load every ``*.md`` under *root* (recursively) as a Node.

    The node id is the frontmatter ``id`` if present, else the filename stem.

    Prune-safety contract: the ``*.md`` glob is deliberate, not incidental. A
    non-markdown sidecar living directly in the wiki dir (e.g. a repo's
    ``.shapa/adr-constraints.json`` — a plain JSON rules file that isn't shapa
    node schema, so it is never turned into one) is never loaded here, and
    therefore can never be scored as an orphan/stale node nor unlinked by
    ``shapa maintain --prune`` (maintain.py) or ``shapa heartbeat``
    (heartbeat.py) — both operate exclusively on this function's output.
    Widening this glob (e.g. to ``*``) would put every non-md file in the
    wiki dir back in the pruner's scan path; see tests/test_maintain.py's
    sidecar-survival tests before doing that.
    """
    root = Path(root)
    paths = sorted(root.rglob("*.md")) if root.is_dir() else [root]
    nodes: dict[str, Node] = {}
    for p in paths:
        parsed = frontmatter.parse(p)
        if parsed.error:
            continue
        node_id = str(parsed.meta.get("id") or p.stem)
        rel_parts = p.relative_to(root).parts if root.is_dir() else ()
        in_arch = len(rel_parts) > 1 and rel_parts[0] == "arch"
        nodes[node_id] = Node(
            id=node_id,
            type=str(parsed.meta.get("type", "")),
            path=p,
            meta=parsed.meta,
            outlinks=extract_links(parsed.body),
            in_arch=in_arch,
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
