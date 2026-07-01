"""Heartbeat engine for shapa.

Provides a random walk over the connection graph, orphan detection, and orphan
pruning.  All random choices are made via an explicit :class:`random.Random`
instance so that a fixed seed produces a fully deterministic result regardless
of dict or filesystem ordering.

CLI usage::

    python3 -m shapa.heartbeat <directory> [--seed N] [--max-steps N] [--dry-run]
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from shapa import config
from shapa.nodes import Graph, build_graph, is_protected, load_nodes


def random_walk(graph: Graph, max_steps: int, rng: random.Random) -> list[str]:
    """Perform a random walk on *graph* for up to *max_steps* nodes.

    The walk starts at a randomly chosen node and hops to a randomly chosen
    neighbour at each step.  It stops early when the current node has no
    neighbours.  All collections are sorted before any random choice so that
    a given seed reproduces the same walk regardless of dict or filesystem order.

    Args:
        graph: the graph to walk.
        max_steps: maximum number of nodes to visit (including the start node).
        rng: seeded random instance used for all choices.

    Returns:
        List of visited node ids.  Empty list when the graph has no nodes.
    """
    if not graph.nodes:
        return []

    current = rng.choice(sorted(graph.nodes))
    visited: list[str] = [current]

    for _ in range(max_steps - 1):
        neighbours = sorted(graph.edges[current])
        if not neighbours:
            break
        current = rng.choice(neighbours)
        visited.append(current)

    return visited


def find_orphans(graph: Graph) -> list[str]:
    """Return a sorted list of real files that have no link in or out.

    Only real files (``graph.nodes``) can be orphans; phantom topic/type
    targets are never pruned. A real note that links to any topic, type, or
    peer - resolved or not - is connected and is never an orphan. Curated
    design docs (``type: reference``) are exempt: they are never orphans.
    """
    return sorted(
        nid for nid, node in graph.nodes.items()
        if not graph.edges.get(nid) and not is_protected(node)
    )


def prune_orphans(
    graph: Graph,
    orphan_ids: list[str],
    dry_run: bool = False,
) -> list[str]:
    """Delete the files of orphan nodes (or simulate deletion in dry-run mode).

    Args:
        graph: the graph whose nodes hold file paths.
        orphan_ids: list of node ids to prune (as returned by
            :func:`find_orphans`).
        dry_run: when ``True`` no files are deleted; the function still returns
            the ids that *would* have been pruned.

    Returns:
        List of pruned (or would-be-pruned) node ids.
    """
    pruned: list[str] = []
    for node_id in orphan_ids:
        node = graph.nodes[node_id]
        if not dry_run:
            node.path.unlink()
        pruned.append(node_id)
    return pruned


def heartbeat(
    directory: str | Path,
    max_steps: int = 10,
    seed: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Run one heartbeat cycle: build graph, walk, find orphans, prune.

    Args:
        directory: directory of shapa node files.
        max_steps: maximum walk length (number of nodes visited).
        seed: random seed for reproducibility; ``None`` means non-deterministic.
        dry_run: when ``True``, orphan files are not deleted.

    Returns:
        Dictionary with keys:
            ``walk``    – list of node ids visited during the random walk.
            ``orphans`` – sorted list of orphan node ids found.
            ``pruned``  – list of node ids whose files were deleted (or would
                          be deleted when *dry_run* is ``True``).
            ``seed``    – the seed value used (echoed back for logging).
    """
    rng = random.Random(seed)
    nodes = load_nodes(directory)
    graph = build_graph(nodes)
    walk = random_walk(graph, max_steps=max_steps, rng=rng)
    orphans = find_orphans(graph)
    pruned = prune_orphans(graph, orphans, dry_run=dry_run)

    return {
        "walk": walk,
        "orphans": orphans,
        "pruned": pruned,
        "seed": seed,
    }


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for the heartbeat engine.

    Example::

        python3 -m shapa.heartbeat /path/to/nodes --seed 42 --max-steps 8 --dry-run
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.heartbeat",
        description="Run a shapa heartbeat: random walk + orphan pruning.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="Memory directory (default: $SHAPA_MEMORY or ~/.shapa/memory).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help="Random seed for a reproducible walk (default: non-deterministic).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=10,
        metavar="N",
        dest="max_steps",
        help="Maximum number of nodes to visit during the walk (default: 10).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Identify orphans but do NOT delete their files.",
    )

    args = parser.parse_args(argv)

    result = heartbeat(
        directory=config.resolve(args.directory),
        max_steps=args.max_steps,
        seed=args.seed,
        dry_run=args.dry_run,
    )

    walk: list[str] = result["walk"]
    orphans: list[str] = result["orphans"]
    pruned: list[str] = result["pruned"]

    print("=== shapa heartbeat ===")
    print()

    if walk:
        print(f"Walk ({len(walk)} step{'s' if len(walk) != 1 else ''}):")
        print("  " + " -> ".join(walk))
    else:
        print("Walk: (no nodes in graph)")

    print()

    if orphans:
        print(f"Orphans found ({len(orphans)}):")
        for oid in orphans:
            print(f"  - {oid}")
    else:
        print("Orphans found: none")

    print()

    action = "Would prune" if args.dry_run else "Pruned"
    if pruned:
        print(f"{action} ({len(pruned)}):")
        for pid in pruned:
            print(f"  - {pid}")
    else:
        print(f"{action}: none")

    if args.dry_run and pruned:
        print()
        print("(dry-run: no files were deleted)")

    sys.exit(0)


if __name__ == "__main__":
    main()
