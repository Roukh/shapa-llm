"""``shapa partition`` and ``shapa map``: a big fire's files as one bounded partition.

``shapa map`` builds the code map (``shapa/codemap.py``: a derived, gitignored graph
of the checkout's files, functions, imports and calls). A partition is what a fire
needs of it, found without reading the rest of the code:

1. Seeds. Files the fire's text names: a path in the feature, in one of its open
   jobs or tasks, or in a memory/rule/issue row linked ``about`` any of them; a
   function or class named there, which also marks *what changes* in its file; and
   the files the map's search recalls for each item's own words (BM25 over each
   file's path, symbol and docstring-line words, fused with model2vec vectors when
   the ``[semantic]`` extra is installed). Each link carries a score.
2. Impact. From each seed, along the graph: the callers of a named function (the
   consequence of changing it), the importers of the file (all of them when no
   function is named, those importing a named symbol most), what the file imports
   and what the named functions call, then one decayed hop further. A node with many
   neighbours spreads its weight over them, so a hub does not flood the result.
3. Bounds. Best-first, at most :data:`MAX_MEMBERS` files and :data:`MAX_BYTES` bytes
   of source on disk now; a file that no longer exists (or is not a safe path) is
   never listed.

The partition is local: it needs the checkout, so nothing here is synced or
hosted. Session start prints the top files of each fire's partition, in its budget;
``shapa partition`` prints all of them with why each is there.

CLI::

    shapa map [--seconds N] [--max-files N] [--rebuild] [--json]
    shapa partition [F<n> | 1 | 2 | 3] [--max-files N] [--max-bytes N] [--no-refresh] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import stat
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from shapa import codemap, db, focus, memlog, rank
from shapa.ledger import CommandError, repo_root, run

MAX_MEMBERS = 24
MAX_BYTES = 120_000      # about 30k tokens of source: what one fire may ask an agent to read
SESSION_FILES = 6
REFRESH_SECONDS = 10.0
MAX_SOURCES = 60          # open jobs and tasks read per fire
MAX_ROWS = 20             # linked rows read per fire
TEXT_CHARS = 4000
MIN_SCORE = 0.12
RECALL_ITEMS = 12
RECALL_TOP = 4
RECALL_FLOOR = 0.6        # a recalled file must score at least this fraction of the item's best
RECALL_WEIGHT = 0.5
TEST_FACTOR = 0.8         # a recalled test file counts a little less than the code it tests
VECTOR_FLOOR = 0.35

W_CALLER = 0.85
W_IMPORTER_NAMED = 0.8
W_IMPORTER = 0.55
W_IMPORTER_OTHER = 0.2
W_IMPORTS = 0.3
W_CALLEE = 0.4
DECAY = 0.5

_PATH_RE = re.compile(
    r"(?<![\w/.:-])((?:[\w.-]+/)*[\w.-]+\.(?:py|tsx|ts|jsx|js|mjs|cjs|mts|cts))(?![\w/-])")
_TICKED = re.compile(r"`([A-Za-z_$][\w$.]{1,80}?)(?:\(\))?`")
_CALLISH = re.compile(r"\b([A-Za-z_][\w]{2,60})\(\)")
_SNAKE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_CAMEL = re.compile(r"\b((?:[A-Z][a-z0-9]+){2,}|[a-z]+(?:[A-Z][a-z0-9]*)+)\b")
_DOTTED = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*){1,3})\b")


@dataclass
class Source:
    id: str
    text: str
    weight: float = 1.0
    recall: bool = False


@dataclass
class Node:
    score: float = 0.0
    why: dict = field(default_factory=dict)      # reason -> the score it contributed
    symbols: set = field(default_factory=set)    # functions the fire names in this file
    seed: bool = False

    def bump(self, score: float, reason: str) -> bool:
        if score > self.why.get(reason, 0.0):
            self.why[reason] = score
        if score > self.score:
            self.score = score
            return True
        return False


@dataclass
class Link:
    id: str
    path: str
    score: float
    via: str   # path, symbol or recall


# --- reading a fire's text -----------------------------------------------------------------------

def _clip(text, limit: int) -> str:
    return memlog.display_line(" ".join(str(text or "").split()))[:limit]


def _sources(conn, fire: focus.Fire) -> list[Source]:
    """The fire's feature, its open jobs and tasks, and the rows linked ``about`` any of them."""
    if not fire.feature:
        return [Source("fire", fire.text, 1.0, True)] if fire.text else []
    feature = db.get_item(conn, fire.feature)
    if feature is None:
        return []
    out = [Source(feature.id, f"{feature.title}\n{(feature.body or '')[:TEXT_CHARS]}", 1.0, True)]
    ids = [feature.id]
    for job in db.items(conn, parent=feature.id):
        if job.status == "closed" or len(out) >= MAX_SOURCES:
            continue
        out.append(Source(job.id, f"{job.title}\n{(job.body or '')[:TEXT_CHARS]}", 1.0, True))
        ids.append(job.id)
        for task in db.items(conn, parent=job.id):
            if task.status != "closed" and len(out) < MAX_SOURCES:
                out.append(Source(task.id, f"{task.title}\n{(task.body or '')[:TEXT_CHARS]}", 1.0, False))
                ids.append(task.id)
    rows: list[str] = []
    for item_id in ids:
        for src, dst, rel in db.links_of(conn, item_id):
            other = dst if src == item_id else src
            if rel == "about" and other[:1] in db.MRI_KINDS and other not in rows:
                rows.append(other)
    for row_id in rows[:MAX_ROWS]:
        row = db.get_row(conn, row_id)
        if row is not None:
            out.append(Source(row.id, f"{row.summary}\n{(row.body or '')[:TEXT_CHARS]}", 0.8, False))
    return out


def _symbol_tokens(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for rx in (_TICKED, _CALLISH, _SNAKE, _CAMEL, _DOTTED):
        for m in rx.finditer(text):
            tok = m.group(1).strip(".")
            if tok and not _PATH_RE.fullmatch(tok):
                seen.setdefault(tok, None)
    return list(seen)[:60]


def _resolve_symbol(graph: codemap.Graph, token: str) -> list[tuple[str, str]]:
    parts = token.split(".")
    name, hint = parts[-1], parts[-2] if len(parts) > 1 else ""
    if not name or not codemap._IDENT.match(name):
        return []
    found = graph.symbols_named(name)
    if hint:
        found = [(p, q, k) for p, q, k in found
                 if q.startswith(hint + ".") or Path(p).stem == hint or p.endswith(f"/{hint}.py")]
    elif len(found) > 3 or (len(name) < 4 and not name[0].isupper()):
        return []
    return [(p, q) for p, q, _k in found[:3]]


def _recall(graph: codemap.Graph, text: str) -> list[tuple[str, float]]:
    """``(path, 0..1)`` of the files the map recalls for *text*: BM25 over file terms, fused with
    vector cosine when the map holds vectors. Weak or one-word matches are dropped."""
    counts = Counter(t for t in codemap.tokens(text) if len(t) >= 3)
    terms = [t for t, _n in counts.most_common(30)]
    if len(terms) < 2:
        return []
    lex = graph.lexical(terms)
    vec = graph.vectors(text[:1200])
    fused = rank.fuse([lex, vec]) if lex and vec else (rank.minmax_normalize(lex) if lex else
                                                       rank.minmax_normalize(vec))
    if not fused:
        return []
    top = max(fused.values())
    need = 3 if len(terms) >= 6 else 2   # distinct query words a file's own words must share
    out = []
    for path, score in sorted(fused.items(), key=lambda kv: (-kv[1], kv[0])):
        rel = score / top if top else 0.0
        if rel < RECALL_FLOOR or len(out) >= RECALL_TOP:
            break
        if graph.term_overlap(path, terms) < need and vec.get(path, 0.0) < VECTOR_FLOOR:
            continue
        out.append((path, rel * (TEST_FACTOR if _is_test(path) else 1.0)))
    return out


# --- the partition -----------------------------------------------------------------------------------

def _is_test(path: str) -> bool:
    low = path.lower()
    return low.startswith(("test/", "tests/")) or "/test" in low or "/__tests__/" in low \
        or low.rsplit("/", 1)[-1].startswith(("test_", "test.")) \
        or any(low.endswith(s) for s in (".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js", "_test.py"))


def _damp(n: int) -> float:
    return 1.0 if n <= 1 else 1.0 / (1.0 + math.log2(n))


def _base(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _seeds(conn, graph, sources: list[Source], nodes: dict[str, Node], links: list[Link]) -> None:
    recalled = 0
    for src in sources:
        for mention in dict.fromkeys(_PATH_RE.findall(src.text)):
            hits = graph.resolve_mention(mention)
            for p in hits:
                exact = p == mention.lstrip("./")
                s = (1.0 if exact else 0.8 if len(hits) == 1 else 0.5) * src.weight
                node = nodes.setdefault(p, Node())
                node.seed = True
                node.bump(s, f"named by {src.id}")
                links.append(Link(src.id, p, round(s, 2), "path"))
        for token in _symbol_tokens(src.text):
            for p, qual in _resolve_symbol(graph, token):
                s = 0.9 * src.weight
                node = nodes.setdefault(p, Node())
                node.seed = True
                node.symbols.add(qual)
                node.bump(s, f"names {qual} ({src.id})")
                links.append(Link(src.id, p, round(s, 2), "symbol"))
        if src.recall and recalled < RECALL_ITEMS:
            recalled += 1
            for p, rel in _recall(graph, src.text):
                s = RECALL_WEIGHT * rel
                node = nodes.setdefault(p, Node())
                node.seed = True
                node.bump(s, f"recall {rel:.2f} ({src.id})")
                links.append(Link(src.id, p, round(s, 2), "recall"))


def _expand(graph, nodes: dict[str, Node]) -> None:
    """Spread each seed's weight along the graph by impact: two hops, the second decayed."""
    frontier = {p: n.score for p, n in nodes.items()}
    for depth in (1, 2):
        decay = 1.0 if depth == 1 else DECAY
        nxt: dict[str, float] = {}
        ranked = sorted(frontier, key=lambda p: (-frontier[p], p))[:12 if depth == 2 else 40]
        for path in ranked:
            node = nodes[path]
            s0 = frontier[path]
            if depth == 2 and s0 < 0.3:
                continue
            base = _base(path)

            def push(other: str, w: float, reason: str) -> None:
                if other == path or other not in graph.files():
                    return
                target = nodes.setdefault(other, Node())
                if target.bump(s0 * w * decay, reason):
                    nxt[other] = target.score

            if depth == 1 and node.symbols:
                for qual in sorted(node.symbols):
                    callers = [c for c in graph.callers(path, qual) if c[0] != path]
                    for cpath, csym, dsym, w in callers[:codemap.FANOUT_CAP]:
                        push(cpath, W_CALLER * w * _damp(len(callers)),
                             f"{csym or 'module code'} calls {dsym}")
                    callees = graph.callees(path, qual)
                    for cpath, _s, dsym, w in callees[:codemap.FANOUT_CAP]:
                        push(cpath, W_CALLEE * w * _damp(len(callees)), f"{qual} calls {dsym}")
            importers = graph.importers(path)
            names = {q.split(".")[-1] for q in node.symbols} | node.symbols
            for ipath, dsym, w in importers[:codemap.FANOUT_CAP]:
                if depth == 1 and node.symbols:
                    named = dsym in names
                    weight = W_IMPORTER_NAMED if named else W_IMPORTER_OTHER
                    reason = f"imports {dsym} from {base}" if named else f"imports {base}"
                else:
                    weight, reason = W_IMPORTER, f"imports {base}"
                push(ipath, weight * w * _damp(len(importers)), reason)
            imports = graph.imports_of(path)
            for ipath, w in imports[:codemap.FANOUT_CAP]:
                push(ipath, W_IMPORTS * w * _damp(len(imports)), f"imported by {base}")
        frontier = nxt
        if not frontier:
            break


def _lstat_size(repo: Path, rel: str) -> int | None:
    try:
        st = os.lstat(repo / rel)
    except OSError:
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


def partition_fire(conn, graph: codemap.Graph, fire: focus.Fire, slot: int = 0, *,
                   max_files: int = MAX_MEMBERS, max_bytes: int = MAX_BYTES) -> dict:
    """The partition of one fire, as a plain dict (also the JSON the CLI and MCP print)."""
    max_files = max(1, min(int(max_files), 200))
    max_bytes = max(1, min(int(max_bytes), 4_000_000))
    t = focus.tree(conn, fire, slot)
    sources = _sources(conn, fire)
    nodes: dict[str, Node] = {}
    links: list[Link] = []
    _seeds(conn, graph, sources, nodes, links)
    _expand(graph, nodes)
    ranked = sorted(((p, n) for p, n in nodes.items() if n.score >= MIN_SCORE),
                    key=lambda kv: (-kv[1].score, kv[0]))
    members, used, stale = [], 0, 0
    for path, node in ranked:
        size = _lstat_size(graph.repo, path)
        if size is None or not codemap.safe_path(path):
            stale += 1
            continue
        if len(members) >= max_files or used + size > max_bytes:
            continue
        used += size
        why = sorted(node.why.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        members.append({"path": path, "score": round(node.score, 2), "bytes": size,
                        "why": [r for r, _w in why]})
    candidates = len(ranked) - stale
    return {"slot": slot, "feature": fire.feature, "title": _clip(t.title, 160), "status": t.status,
            "files": members, "bytes": used, "omitted": max(0, candidates - len(members)),
            "stale": stale, "repo_bytes": graph.total_bytes(), "repo_files": len(graph.files()),
            "links": [{"id": lk.id, "path": lk.path, "score": lk.score, "via": lk.via}
                      for lk in sorted(links, key=lambda x: (-x.score, x.id, x.path))[:40]
                      if any(m["path"] == lk.path for m in members)]}


def _human(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1048576:.2f} MB"


def render(p: dict) -> str:
    """The full partition as text, each file with why it is in it."""
    share = (100.0 * p["bytes"] / p["repo_bytes"]) if p["repo_bytes"] else 0.0
    head = (f"{p['feature'] + ' ' if p['feature'] else ''}{p['title']}  "
            f"[{len(p['files'])} of {p['repo_files']} files, {_human(p['bytes'])} of "
            f"{_human(p['repo_bytes'])} repo code ({share:.1f}%)]")
    lines = [head]
    for m in p["files"]:
        lines.append(f"  {m['score']:.2f}  {m['path']}  {_human(m['bytes'])}  " + "; ".join(m["why"]))
    if p["omitted"]:
        lines.append(f"  ... {p['omitted']} more related files over the caps (--max-files, --max-bytes)")
    if p["stale"]:
        lines.append(f"  ({p['stale']} mapped files are gone or unsafe: run `shapa map`)")
    return "\n".join(lines)


def session_line(p: dict, shown: int = SESSION_FILES) -> str:
    """One line for session start: the partition's top files, its size against the repo's."""
    files = p["files"]
    if not files:
        return ""
    top = ", ".join(memlog.display_line(m["path"])[:70] for m in files[:shown])
    more = f" (+{len(files) - shown})" if len(files) > shown else ""
    share = (100.0 * p["bytes"] / p["repo_bytes"]) if p["repo_bytes"] else 0.0
    ref = p["feature"] or f"{p['slot']}"
    return (f"   code map: {top}{more} - {len(files)} files, {_human(p['bytes'])}, {share:.1f}% of repo code; "
            f"all with why: shapa partition {ref}")


# --- running it ---------------------------------------------------------------------------------------

def fire_partitions(conn, graph, target: str | None = None, **caps) -> list[dict]:
    """Partitions for *target*: ``None`` (every fire of the wiki), a slot ``1``-``3``, or a feature id."""
    f = focus.load(conn)
    if target is None:
        return [partition_fire(conn, graph, fire, i, **caps)
                for i, fire in enumerate(f.fires, 1) if fire is not None
                and not (fire.feature and focus.tree(conn, fire, i).status in ("closed", "missing"))]
    t = str(target).strip()
    if t.isdigit():
        n = focus._slot(t)
        if f.fires[n - 1] is None:
            raise db.LedgerError(f"fire {n} is free")
        return [partition_fire(conn, graph, f.fires[n - 1], n, **caps)]
    item = db.get_item(conn, t)
    if item is None or item.kind != "F":
        raise db.LedgerError(f"{t} is not a feature in this wiki")
    slot = next((i for i, x in enumerate(f.fires, 1) if x and x.feature == item.id), 0)
    return [partition_fire(conn, graph, focus.Fire(feature=item.id), slot, **caps)]


def compute(wiki_root, target: str | None = None, *, refresh: bool = True,
            max_files: int = MAX_MEMBERS, max_bytes: int = MAX_BYTES,
            seconds: float = REFRESH_SECONDS) -> dict:
    """What ``shapa partition`` and the MCP tool return: ``{"map": ..., "fires": [...]}`` or ``{"error": ...}``.
    A missing map is built first when *refresh* (bounded by *seconds*); an existing one is refreshed
    incrementally, which is a stat of every file when nothing changed."""
    wiki_root = Path(wiki_root)
    info = None
    if refresh:
        try:
            info = codemap.build(wiki_root, seconds=seconds).as_dict()
        except (sqlite3.Error, OSError) as exc:  # another build holds the map: read it as it is
            info = {"error": f"map not refreshed ({type(exc).__name__})"}
    graph = codemap.open_graph(wiki_root)
    if graph is None:
        return {"error": "no code map here: run `shapa map` first"}
    try:
        conn = db.connect(wiki_root)
        if conn is None:
            return {"error": f"{wiki_root} has no database - run `shapa upgrade {wiki_root}`"}
        try:
            try:
                fires = fire_partitions(conn, graph, target, max_files=max_files, max_bytes=max_bytes)
            except (db.LedgerError, ValueError) as exc:
                return {"error": str(exc)}
        finally:
            conn.close()
        out = {"fires": fires}
        if info is not None:
            out["map"] = info
        return out
    finally:
        graph.close()


def _wiki(cwd) -> Path:
    return repo_root(cwd)


def run_map(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa map", description="Build or refresh the code map of this checkout "
                                "(a derived, gitignored .shapa-code.db beside the wiki's index).")
    p.add_argument("--cwd", default=None)
    p.add_argument("--seconds", type=float, default=codemap.BUILD_SECONDS,
                   help="stop parsing after this long; run again to continue")
    p.add_argument("--max-files", type=int, default=codemap.MAX_FILES)
    p.add_argument("--rebuild", action="store_true", help="discard the map and start over")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    wiki = _wiki(args.cwd)
    if args.rebuild:
        for suffix in ("", "-journal"):
            try:
                os.unlink(str(codemap.index_path(wiki)) + suffix)
            except OSError:
                pass
    try:
        st = codemap.build(wiki, seconds=args.seconds, max_files=args.max_files)
    except (sqlite3.Error, OSError) as exc:
        raise CommandError(f"the code map could not be built ({type(exc).__name__}): is another "
                           "`shapa map` running?") from None
    if args.json:
        print(json.dumps(st.as_dict()))
        return 0
    print(f"code map: {st.files} files, {st.symbols} symbols, {st.imports} import edges, "
          f"{st.calls} call edges ({st.parsed} parsed, {st.reused} unchanged, {st.removed} removed, "
          f"{st.skipped} skipped) in {st.seconds:.2f}s")
    if st.partial:
        print("time budget reached: run `shapa map` again to continue")
    if st.truncated:
        print(f"file cap reached ({args.max_files}): the rest of the tree is not mapped")
    if st.vectors:
        print(f"{st.vectors} file vectors stored")
    return 0


def run_partition(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa partition", description="A fire's related files as one bounded "
                                "partition: named files, retrieval hits and graph impact, each with why.")
    p.add_argument("target", nargs="?", help="F<n>, or a fire slot 1-3; default every fire")
    p.add_argument("--cwd", default=None)
    p.add_argument("--max-files", type=int, default=MAX_MEMBERS)
    p.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    p.add_argument("--no-refresh", action="store_true", help="read the map as it is")
    p.add_argument("--seconds", type=float, default=REFRESH_SECONDS)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    out = compute(_wiki(args.cwd), args.target, refresh=not args.no_refresh, max_files=args.max_files,
                  max_bytes=args.max_bytes, seconds=args.seconds)
    if "error" in out:
        raise CommandError(out["error"])
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    if not out["fires"]:
        print("no fires are set: `shapa focus fire 1 F<n> --scope repo`, or name a feature: shapa partition F<n>")
    print("\n\n".join(render(f) for f in out["fires"]))
    return 0


def main_map(argv: list[str] | None = None) -> None:
    run(run_map, list(sys.argv[1:] if argv is None else argv))


def main_partition(argv: list[str] | None = None) -> None:
    run(run_partition, list(sys.argv[1:] if argv is None else argv))
