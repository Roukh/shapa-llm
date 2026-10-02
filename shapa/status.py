"""``shapa status`` / ``shapa doctor`` - which retrieval mode is live, and the
state of every wiki in scope.

The mode line is the point (memory v3): with the ``[semantic]`` extra,
recall fuses float32 model2vec vectors with FTS5 BM25; without it, recall
runs BM25-only. Both work - but which one is running is always reported,
never silent. ``status`` loads the embedding model to prove the extra
actually works (an installed-but-broken model cache reads as bm25-only
here, exactly as every read path would treat it).

``doctor`` prints the same report and exits 1 when something needs a
hand: a wiki behind the current format, a malformed memory-log line, or a
wiki path in scope that is missing.

CLI::

    shapa status [--cwd DIR] [--json]
    shapa doctor [--cwd DIR] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from shapa import __version__, config, embed, memlog, registry, store
from shapa.nodes import load_nodes


def mode_report(load_model: bool = True) -> dict:
    semantic = embed.available() if load_model else embed.installed()
    lexical = memlog.lexical_backend()
    if semantic:
        desc = f"fused: {embed.model_name()} float32 vectors + {lexical} BM25"
    else:
        desc = (f"bm25-only: {lexical} - the [semantic] extra is not installed (or its model "
                "does not load); install shapa[semantic] for vectors")
    try:
        import mcp  # noqa: F401
        mcp_sdk = True
    except ImportError:
        mcp_sdk = False
    return {"mode": "fused" if semantic else "bm25", "description": desc,
            "lexical": lexical, "semantic": semantic, "fts5": store.fts5_available(),
            "mcp_transport": "stdio (mcp SDK installed)" if mcp_sdk else "stdio (stdlib JSON-RPC)"}


def _index_state(root: Path) -> dict:
    path = store.db_path(root)
    if not path.is_file():
        return {"exists": False, "fresh": False, "vectors": 0}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return {"exists": True, "fresh": False, "vectors": 0}
    try:
        fresh = memlog._fresh(conn, root) if memlog.has_log(root) else True
        try:
            vectors = conn.execute("SELECT count(*) FROM memory_vectors").fetchone()[0]
        except sqlite3.Error:
            vectors = 0
    finally:
        conn.close()
    return {"exists": True, "fresh": bool(fresh), "vectors": int(vectors),
            "bytes": path.stat().st_size}


def wiki_report(wr) -> dict:
    root = Path(wr.path)
    out = {"kind": wr.kind, "repo": wr.repo, "path": str(root), "exists": root.is_dir(),
           "is_wiki": registry.is_wiki(root)}
    if not out["exists"]:
        return out
    fmt = registry.read_format(root)
    out.update({
        "format": fmt, "current_format": registry.CURRENT_FORMAT,
        "notes": len(load_nodes(root)),
        "memory": memlog.stats(root),
        "index": _index_state(root),
    })
    return out


def gather(start=None, *, load_model: bool = True) -> dict:
    roots = config.wiki_roots(start)
    return {"shapa": __version__, "recall": mode_report(load_model),
            "wikis": [wiki_report(wr) for wr in roots]}


def problems(report: dict) -> list[str]:
    out = []
    for w in report["wikis"]:
        if not w["exists"]:
            if w["kind"] != "global":
                out.append(f"{w['path']}: wiki path in scope does not exist")
            continue
        if w.get("is_wiki") and w.get("format", 0) < registry.CURRENT_FORMAT:
            out.append(f"{w['path']}: format {w['format']}<{registry.CURRENT_FORMAT} - "
                       "run the shapa-upgrade skill (`shapa upgrade`)")
        bad = (w.get("memory") or {}).get("malformed", 0)
        if bad:
            out.append(f"{w['path']}: {bad} malformed memory-log line(s) - see `shapa upgrade --check`")
    return out


def render(report: dict, doctor: bool = False) -> str:
    r = report["recall"]
    lines = [f"shapa {report['shapa']}", f"recall mode: {r['description']}",
             f"mcp: {r['mcp_transport']}"]
    for w in report["wikis"]:
        tag = f" ({w['repo']})" if w.get("repo") else ""
        if not w["exists"]:
            lines.append(f"wiki[{w['kind']}{tag}]: {w['path']} - missing")
            continue
        m = w["memory"]
        idx = w["index"]
        idx_text = ("no index yet" if not idx["exists"] else
                    f"index {'fresh' if idx['fresh'] else 'stale (rebuilt on next read)'}, "
                    f"{idx['vectors']} vectors")
        lines.append(
            f"wiki[{w['kind']}{tag}]: {w['path']} - format {w['format']}/{w['current_format']}, "
            f"{w['notes']} notes, {m['active']} memories live ({m['superseded']} superseded, "
            f"{m['archived']} archived, {m['malformed']} malformed), log {m['log_bytes'] / 1024:.0f} KB, "
            f"{idx_text}")
    if doctor:
        probs = problems(report)
        lines.append("problems: " + ("none" if not probs else ""))
        lines.extend(f"  - {p}" for p in probs)
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, doctor: bool = False) -> None:
    parser = argparse.ArgumentParser(
        prog="shapa doctor" if doctor else "shapa status",
        description="Report the live retrieval mode (fused or bm25-only) and every wiki in scope.",
    )
    parser.add_argument("--cwd", default=None, help="Directory to resolve wikis from.")
    parser.add_argument("--json", action="store_true", help="Machine-readable report.")
    args = parser.parse_args(argv)
    report = gather(args.cwd)
    if args.json:
        if doctor:
            report["problems"] = problems(report)
        print(json.dumps(report, indent=2))
    else:
        print(render(report, doctor=doctor))
    sys.exit(1 if doctor and problems(report) else 0)


def doctor_main(argv: list[str] | None = None) -> None:
    main(argv, doctor=True)


if __name__ == "__main__":
    main()
