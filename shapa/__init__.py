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
    fetch        the read path - md notes + memory records, one fused ranking
    memlog       v3 memory records: the append-only JSONL log + derived index
    redact       secret redaction applied before any memory is written
    capture      the write path - atomic memories from a finished session
    get          the full text behind any surfaced id (shapa get)
    status       the live recall mode + wiki state (shapa status/doctor)
    maintain     expanded self-healing - prune + auto-merge + reconcile
    registry     wiki format marker + the registry of every known wiki
    upgrade      bring every wiki to the current format (shapa upgrade)
    cli          the unified ``shapa`` command
"""

__all__ = [
    "config", "frontmatter", "nodes", "heartbeat", "validate", "score",
    "fetch", "memlog", "redact", "capture", "get", "status", "maintain",
    "registry", "upgrade", "cli",
]
__version__ = "0.8.0"
