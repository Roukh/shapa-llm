"""``shapa node`` - every repo wiki reflects a node of itself into the global wiki.

The global wiki is the operator's one brain, and information flows to it from the single wikis. A **node**
is ONE project document in the global wiki per repo (the one-document-per-project rule; project key = the
repo's wiki key, alias ``node:KEY``, mime ``application/json``) whose body is compact JSON::

    {"key", "updated", "northstar", "fires": [...], "principles": [[id, date, text], ...],
     "mistakes": [[id, date, text], ...], "counts": {"rules", "memories", "issues", "open_tasks"}}

``principles`` are the repo wiki's own principles (project and subproject scope, newest first, at most
:data:`MAX_PRINCIPLES`); ``mistakes`` its open corrections (at most :data:`MAX_MISTAKES`). Every text is one
masked line (:func:`shapa.learn.flatten`) and the body is written with ``json.dumps``, so a principle whose
text looks like JSON stays a string. :func:`sync` is idempotent: no write when nothing but ``updated``
would change. When a project of that key already holds a human document, the node goes to the key
``node/KEY`` instead: it never overwrites one.

**Promotion.** A principle that appears (same normalised text, or a near duplicate by the learning loop's
word-overlap check) in the nodes of two or more repos is filed in the global wiki as a PENDING candidate:
an issue tagged ``correction``, ``soft`` and ``promote`` whose body names the source ids. ``shapa learn
pending`` lists it and ``shapa learn apply`` turns it into a global principle as for any correction;
``shapa learn dismiss`` removes it and it is not filed again (a per-device note in the derived index).
Nothing is promoted automatically. At most :data:`MAX_NEW_PER_SYNC` are filed per sync and
:data:`MAX_OPEN` stay open, however many repos agree.

``sync`` runs at the end of a session (``shapa commit --hook``); it is bounded (:data:`BUDGET_S`),
swallows every error and never blocks a session.

CLI::

    shapa node sync [--cwd DIR] [--json] [--hook]
    shapa node list [--cwd DIR] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from shapa import db, learn, metrics

BUDGET_S = 2.0
MAX_PRINCIPLES = 40
MAX_MISTAKES = 20
MAX_NODES = 200            # node documents one promotion pass reads
MAX_NEW_PER_SYNC = 3
MAX_OPEN = 5
PROMOTE_MIN_REPOS = 2
MAX_SOURCES = 8            # sources named in one candidate
ALIAS_PREFIX = "node:"
MIME = "application/json"
FALLBACK_PREFIX = "node/"
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,79}$")
_ID_RE = re.compile(r"^[A-Z][1-9][0-9]{0,8}$")
_DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
_PROMOTE_SQL = "CREATE TABLE IF NOT EXISTS n_promoted (fp TEXT PRIMARY KEY, ts TEXT NOT NULL)"


class Deadline:
    def __init__(self, budget: float):
        self.end = time.monotonic() + budget

    def left(self) -> float:
        return self.end - time.monotonic()

    def over(self) -> bool:
        return self.left() <= 0

    def busy_ms(self) -> int:
        return max(1, int(self.left() * 1000))


def node_key(root) -> str:
    """The repo's wiki key as a project key: what :func:`shapa.db.clean_project` accepts."""
    key = re.sub(r"[^a-z0-9._/-]+", "-", metrics.repo_key(root).lower()).strip("-./")
    return key[:70] or "repo"


# --- the node ----------------------------------------------------------------------------------------

def _row_date(conn, rid: str) -> str:
    r = conn.execute("SELECT updated, created FROM mri WHERE id = ?", (rid,)).fetchone()
    return str((r[0] or r[1]) if r else "")[:10]


def build(conn, key: str) -> dict:
    """The node of a repo wiki, without its ``updated`` stamp."""
    from shapa import focus

    node: dict = {"key": key, "updated": "", "northstar": "", "fires": []}
    try:
        f = focus.as_dict(conn)
        node["northstar"] = learn.flatten(f.get("northstar") or "", 240)
        for fire in f.get("fires") or []:
            if not fire:
                continue
            label = f"{fire['feature']}: {fire.get('title') or fire.get('text') or ''}" if fire.get("feature") \
                else str(fire.get("text") or "")
            if label.strip():
                node["fires"].append(learn.flatten(label, 160))
    except Exception:
        pass
    principles = []
    for rid in learn.live_principles(conn):
        r = conn.execute("SELECT summary, updated, created FROM mri WHERE id = ?", (rid,)).fetchone()
        if r is not None:
            principles.append([rid, str(r["updated"] or r["created"])[:10], learn.flatten(r["summary"])])
    principles.sort(key=lambda x: (x[1], x[0]), reverse=True)
    node["principles"] = principles[:MAX_PRINCIPLES]
    mistakes = []
    for rid, created, summary, body in conn.execute(
            "SELECT m.id, m.created, m.summary, m.body FROM tags c JOIN mri m ON m.id = c.id "
            "WHERE c.tag = 'correction' AND NOT EXISTS (SELECT 1 FROM tags x WHERE x.id = c.id AND "
            "x.tag IN ('learned','dismissed')) ORDER BY m.created DESC, m.rowid DESC LIMIT ?", (MAX_MISTAKES,)):
        row = db.Row(rid, "I", None, summary, body, "", "", "", created, created, [])
        mistakes.append([rid, str(created)[:10], learn.flatten(learn.issue_text(row))])
    node["mistakes"] = mistakes
    n = db.counts(conn)
    node["counts"] = {"rules": n["R"], "memories": n["M"], "issues": n["I"], "open_tasks": n["T"]}
    return node


def dump(node: dict) -> str:
    return json.dumps(node, separators=(",", ":"), ensure_ascii=False)


def parse(doc: db.Doc, expect_key: str | None = None) -> dict | None:
    """A node document's body as a validated dict, or ``None``: the alias, the mime and the shape must all
    hold, and the key in the body must be the one the alias names (a document cannot speak for another
    repo). Every text is flattened again: what is stored here is never trusted."""
    alias = doc.alias or ""
    if not alias.startswith(ALIAS_PREFIX) or doc.mime != MIME or len(doc.body) > 400_000:
        return None
    try:
        raw = json.loads(doc.body)
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("key"), str) or not _KEY_RE.match(raw["key"]):
        return None
    named = alias[len(ALIAS_PREFIX):]
    if named not in (raw["key"], FALLBACK_PREFIX + raw["key"][:70]) or (expect_key and raw["key"] != expect_key):
        return None

    def triples(v, cap):
        if not isinstance(v, list):
            return None
        out = []
        for t in v[:cap]:
            if (not isinstance(t, list) or len(t) != 3 or not all(isinstance(x, str) for x in t)
                    or not _ID_RE.match(t[0]) or not _DATE_RE.match(t[1])):
                return None
            out.append([t[0], t[1], learn.flatten(t[2])])
        return out

    principles, mistakes = triples(raw.get("principles"), MAX_PRINCIPLES), triples(raw.get("mistakes"), MAX_MISTAKES)
    if principles is None or mistakes is None or not isinstance(raw.get("updated"), str):
        return None
    counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    fires = raw.get("fires") if isinstance(raw.get("fires"), list) else []
    return {"key": raw["key"], "updated": raw["updated"][:32],
            "northstar": learn.flatten(str(raw.get("northstar") or ""), 240),
            "fires": [learn.flatten(str(f), 160) for f in fires[:3] if isinstance(f, str)],
            "principles": principles, "mistakes": mistakes,
            "counts": {k: int(counts[k]) for k in ("rules", "memories", "issues", "open_tasks")
                       if isinstance(counts.get(k), int)}}


def _write(gconn, key: str, node: dict, now: datetime) -> tuple[str, str]:
    """Create or update the node document: ``(status, doc id)``. A project of this key held by a document
    that is not a node moves the node to ``node/KEY``; an alias held by something else is skipped over."""
    for project in (key, FALLBACK_PREFIX + key):
        alias = ALIAS_PREFIX + project
        held = db.project_doc(gconn, project)
        if held is not None:
            if held.alias != alias or held.mime != MIME:
                continue                                   # a human document: never overwritten
            stored = parse(held)
            if stored is not None and stored["key"] == key:
                same = {k: v for k, v in stored.items() if k != "updated"}
                if same == {k: v for k, v in node.items() if k != "updated"}:
                    return "unchanged", held.id
            db.update_doc(gconn, held.id, title=f"node: {key}", body=dump(node), mime=MIME)
            return "updated", held.id
        if gconn.execute("SELECT 1 FROM docs WHERE alias = ? OR id = ?", (alias, alias)).fetchone():
            continue
        did = db.add_doc(gconn, f"node: {key}", dump(node), alias=alias, mime=MIME, source="node", now=now,
                         project=project)
        return "created", did
    return "conflict", ""


# --- promotion -----------------------------------------------------------------------------------------

def read_nodes(gconn, deadline: Deadline | None = None) -> list[dict]:
    out = []
    for (did,) in gconn.execute("SELECT id FROM docs WHERE alias LIKE 'node:%' ORDER BY updated DESC, id "
                                "LIMIT ?", (MAX_NODES,)).fetchall():
        if deadline is not None and deadline.over():
            break
        d = db.get_doc(gconn, did)
        n = parse(d) if d is not None else None
        if n is not None:
            out.append(n)
    return out


def _norm(text: str) -> str:
    return " ".join(sorted(learn.tokens(text)))


def _fingerprint(text: str) -> str:
    return hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()[:20]


def clusters(nodes: list[dict], deadline: Deadline | None = None) -> list[list[tuple]]:
    """Principles of different repos that say the same thing, grouped: ``[[(repo, id, date, text), ...]]``
    for each group of at least :data:`PROMOTE_MIN_REPOS` repos. Same normalised text, or word overlap of a
    repeat (:data:`shapa.learn.LEX_REPEAT`) without opposite polarity."""
    items = [(n["key"], p[0], p[1], p[2]) for n in nodes for p in n["principles"]]
    toks = [learn.tokens(it[3]) for it in items]
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    posting: dict[str, list[int]] = {}
    for i, ts in enumerate(toks):
        if deadline is not None and i % 200 == 0 and deadline.over():
            return []
        cand: set[int] = set()
        for t in sorted(ts, key=lambda t: len(posting.get(t, ())))[:6]:
            cand.update(posting.get(t, ())[-60:])
        for j in cand:
            if items[j][0] == items[i][0] or find(i) == find(j):
                continue
            if toks[i] == toks[j] or (learn.dice(toks[i], toks[j]) >= learn.LEX_REPEAT
                                      and not learn.opposite(items[i][3], items[j][3])):
                parent[find(i)] = find(j)
        for t in ts:
            posting.setdefault(t, []).append(i)
    groups: dict[int, list[tuple]] = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    out = [g for g in groups.values() if len({m[0] for m in g}) >= PROMOTE_MIN_REPOS]
    for g in out:
        g.sort(key=lambda m: (m[2], m[0], m[1]), reverse=True)
    return sorted(out, key=lambda g: (-len({m[0] for m in g}), g[0][3]))


def _open_promotions(gconn) -> int:
    return gconn.execute(
        "SELECT COUNT(*) FROM tags p WHERE p.tag = 'promote' AND NOT EXISTS (SELECT 1 FROM tags x WHERE "
        "x.id = p.id AND x.tag IN ('learned','dismissed'))").fetchone()[0]


def promote(gconn, groot, deadline: Deadline, now: datetime) -> list[str]:
    """File the promotion candidates of the nodes now in the global wiki; returns the issue ids."""
    open_n = _open_promotions(gconn)
    if open_n >= MAX_OPEN or deadline.over():
        return []
    groups = clusters(read_nodes(gconn, deadline), deadline)
    if not groups:
        return []
    filed: list[str] = []
    try:
        loc = metrics.open_local(groot, timeout=min(1.0, max(0.05, deadline.left())))
        loc.execute(_PROMOTE_SQL)
        loc.commit()
    except Exception:
        return []
    try:
        for g in groups:
            if deadline.over() or open_n + len(filed) >= MAX_OPEN or len(filed) >= MAX_NEW_PER_SYNC:
                break
            text = learn.flatten(g[0][3])
            fp = _fingerprint(text)
            if len(learn.tokens(text)) < 2 or loc.execute("SELECT 1 FROM n_promoted WHERE fp = ?", (fp,)).fetchone():
                continue
            if _known_globally(gconn, groot, text):
                continue
            seen, sources = set(), []
            for repo, rid, date, _ in g:
                if repo not in seen and len(sources) < MAX_SOURCES:
                    seen.add(repo)
                    sources.append(f"{repo}/{rid} ({date})")
            body = f"Sources: {', '.join(sources)}\nOperator: {text}"
            try:
                rid, created = db.add_row(gconn, "I", f"promote to global: {text}", body,
                                          tags=["correction", "soft", "promote", "repeat-checked"],
                                          source="node", now=now)
            except db.LedgerError:
                continue
            loc.execute("INSERT OR IGNORE INTO n_promoted(fp, ts) VALUES (?, ?)", (fp, db.now_iso(now)))
            loc.commit()
            if created:
                filed.append(rid)
    finally:
        loc.close()
    return filed


def _known_globally(gconn, groot, text: str) -> bool:
    """A global rule already says (nearly) this: nothing to promote."""
    try:
        with learn.LearnIndex(groot, timeout=0.5) as ix:
            ix.sync(gconn, max_rows=learn.CATCHUP_ROWS)
            return any(h.merge for h in ix.search(text, 3, kinds=("R",), wiki="global"))
    except sqlite3.Error:
        return False


# --- sync ------------------------------------------------------------------------------------------------

def sync_quietly(cwd=None, *, budget: float | None = None) -> None:
    """The SessionEnd step: :func:`sync`, bounded, every error swallowed."""
    try:
        sync(cwd, budget=budget)
    except Exception:
        pass


def sync(cwd=None, *, now: datetime | None = None, budget: float | None = None) -> dict:
    """Reflect this repo's node into the global wiki and file promotion candidates. Never raises for a
    locked database: ``{"status": "busy"}``."""
    now = now or datetime.now(timezone.utc)
    deadline = Deadline(BUDGET_S if budget is None else budget)
    ss = learn.scopes(cwd)
    repo = next((s for s in ss if s.label == "repo"), None)
    glob = next((s for s in ss if s.label == "global"), None)
    if repo is None or glob is None:
        return {"status": "skipped", "reason": "needs a repo wiki and the global wiki, both with a database"}
    if repo.root.resolve() == glob.root.resolve():
        return {"status": "skipped", "reason": "the global wiki has no node"}
    key = node_key(repo.root)
    try:
        rconn = db.connect(repo.root, busy_ms=min(deadline.busy_ms(), 1000))
        try:
            node = build(rconn, key)
        finally:
            rconn.close()
        if deadline.over():
            return {"status": "busy", "key": key}
        gconn = db.connect(glob.root, busy_ms=deadline.busy_ms())
        try:
            status, did = _write(gconn, key, {**node, "updated": now.strftime("%Y-%m-%d")}, now)
            filed = promote(gconn, glob.root, deadline, now) if status != "conflict" else []
        finally:
            gconn.close()
    except sqlite3.Error:
        return {"status": "busy", "key": key}
    except db.LedgerError:                      # a session ending beside this one made the document first
        return {"status": "conflict", "key": key}
    return {"status": status, "key": key, "doc": did, "promoted": filed}


def listing(cwd=None) -> list[dict]:
    glob = next((s for s in learn.scopes(cwd) if s.label == "global"), None)
    if glob is None:
        return []
    conn = glob.conn()
    try:
        return [{"key": n["key"], "updated": n["updated"], "principles": len(n["principles"]),
                 "mistakes": len(n["mistakes"]), "counts": n["counts"]} for n in read_nodes(conn)]
    finally:
        conn.close()


def run_node(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa node", description="Reflect this repo's wiki into the global wiki.")
    p.add_argument("--cwd", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sy = sub.add_parser("sync", help="write this repo's node into the global wiki; file promotion candidates")
    sy.add_argument("--json", action="store_true")
    sy.add_argument("--hook", action="store_true", help="bounded, silent, always exit 0")
    sy.add_argument("--cwd", default=argparse.SUPPRESS)
    ls = sub.add_parser("list", help="the nodes the global wiki holds")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--cwd", default=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.cmd == "list":
        res = listing(args.cwd)
        if args.json:
            print(json.dumps(res))
        for n in res:
            print(f"{n['key']}\t{n['updated']}\tprinciples {n['principles']}, mistakes {n['mistakes']}")
        return 0
    res = sync(args.cwd)
    if args.hook:
        return 0
    if args.json:
        print(json.dumps(res, sort_keys=True))
    else:
        print(f"node {res.get('key', '-')}: {res['status']}"
              + (f"; {len(res['promoted'])} promotion candidate(s) filed" if res.get("promoted") else ""))
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--hook" in argv:
        try:
            run_node(argv)
        except BaseException:
            pass
        sys.exit(0)
    try:
        code = run_node(argv)
    except (db.LedgerError, learn.CommandError) as exc:
        print(f"shapa: {exc}", file=sys.stderr)
        code = 2
    except sqlite3.Error as exc:
        print(f"shapa: the database is locked or unreadable ({type(exc).__name__}); try again", file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
