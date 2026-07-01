"""shapa - a self-healing markdown-graph memory for an LLM agent.

Core engine is pure standard library. Memory lives OUTSIDE the tool
(``$SHAPA_MEMORY`` or ``~/.shapa/memory``). Every note carries uniform
frontmatter (id, type, created, consequence, locus, uses) and connects to
others through Obsidian ``[[wikilinks]]`` in the body.

Public modules:
    config       where memory lives ($SHAPA_MEMORY / ~/.shapa/memory)
    frontmatter  parse a note into (meta, body) - the shared contract
    nodes        Node model + link graph from body wikilinks
    heartbeat    random-walk pulse + orphan pruning over the link graph
    validate     uniform frontmatter-schema validation
    score        node value scoring (consequence, locus, freshness, uses)
    fetch        the read path - relevant memory at prompt start
    capture      the write path - distil a finished session into a note
    maintain     expanded self-healing - prune + auto-merge + reconcile
    cli          the unified ``shapa`` command
"""

__all__ = [
    "config", "frontmatter", "nodes", "heartbeat", "validate", "score",
    "fetch", "capture", "maintain", "cli",
]
__version__ = "0.6.0"
