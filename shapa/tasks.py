"""``shapa tasks`` - the operator's universal task list, and the task type aliases.

A task is a ledger ``T`` item. ``human_task`` (the operator must do it) and ``agent_task`` (an
agent may take it) are type aliases that create a ``T`` carrying that tag, the way ``idea`` is a
memory carrying ``idea``. The global wiki accepts tasks, standalone, and each must carry exactly
one of the two tags; a repo wiki accepts them too. A ``project:KEY`` tag ties a task to a project
(and its ``about`` link to the project's document, kept by :mod:`shapa.db`).

One read answers "what do I need to do": every open ``human_task`` of every wiki in scope (the
global one and this repo's; ``--all``: every registered wiki), ``rank:N`` first (1-999, lower
first), then oldest. It reads by tag, so it costs the number of tasks, not of records.

CLI::

    shapa tasks [--owner human|agent] [--project KEY] [--status open|closed|all] [--all] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from shapa import db
from shapa.ledger import run
from shapa.redact import redact

LIMIT = 100
OWNERS = ("human", "agent")
STATUSES = ("open", "closed", "all")
_OWNER_TAG = {v: k for k, v in db.OWNER_TAGS.items()}
_RANK_RE = re.compile(r"[1-9][0-9]{0,2}")
#: The sentence a session start prints after the operator's open tasks (the Worker prints the same).
ASK_SENTENCE = ("When the operator asks what they need to do, answer from `tasks` "
                "(GET /v1/tasks?owner=human). When they mention something they must do, save it as a "
                "human_task.")


def owner_of(tags) -> str:
    for t in sorted(tags):
        if t in db.OWNER_TAGS:
            return db.OWNER_TAGS[t]
    return ""


def add_task(conn: sqlite3.Connection, title: str, owner: str, body: str = "", *,
             project: str | None = None, tags=(), standalone_only: bool = False,
             parent: str | None = None) -> tuple[str, bool]:
    """File a ``T`` item for *owner* (``human``, ``agent``, or ``""`` for a plain task in a repo
    wiki). Returns ``(id, created)``: identical open text of the same owner in this wiki is stored
    once (the existing id comes back, and gains the project tag when it had none).
    *standalone_only* is the global wiki's rule: no parent."""
    if owner and owner not in OWNERS:
        raise db.LedgerError(f"a task owner is human or agent (got {owner!r})")
    if standalone_only and not owner:
        raise db.LedgerError("a task in the global wiki is a human_task or an agent_task")
    if standalone_only and parent:
        raise db.LedgerError("a task in the global wiki is standalone: it has no parent")
    key = db.clean_project(project)
    want = [*tags]
    if owner:
        want.append(_OWNER_TAG[owner])
    if key:
        want.append(db.PROJECT_PREFIX + key)
    if sum(1 for t in want if t in db.OWNER_TAGS) > 1:
        raise db.LedgerError("a task is a human_task or an agent_task, not both")
    clean_title = db.one_line(redact(title), 200)
    if not clean_title:
        raise db.LedgerError("title is required")
    clean_body = redact(str(body or "").strip())
    found = None
    if owner:
        found = conn.execute(
            "SELECT i.id FROM items i JOIN tags t ON t.id = i.id AND t.tag = ? "
            "WHERE i.kind = 'T' AND i.status != 'closed' AND i.title = ? AND i.body = ? "
            "ORDER BY i.created, i.id LIMIT 1", (_OWNER_TAG[owner], clean_title, clean_body)).fetchone()
    if found:
        # identical text never moves a task to another project: only a task with none gains one
        if key and not any(t.startswith(db.PROJECT_PREFIX) for t in db._tags_of(conn, found[0])):
            db.tag(conn, found[0], [db.PROJECT_PREFIX + key])
        return found[0], False
    return db.add_item(conn, "T", clean_title, parent=parent, body=clean_body, tags=want), True


def _ranks(conn: sqlite3.Connection) -> dict[str, int]:
    """``{task id: rank}`` from one range read of the (few) ranked records."""
    ranks: dict[str, int] = {}
    for rid, t in conn.execute("SELECT id, tag FROM tags WHERE tag >= 'rank:' AND tag < 'rank;' "
                               "AND id >= 'T' AND id < 'U'"):
        n = t[len("rank:"):]
        if _RANK_RE.fullmatch(n):
            ranks[rid] = min(int(n), ranks.get(rid, 1000))
    return ranks


def _open_fast(conn: sqlite3.Connection, label: str, owner: str, limit: int) -> tuple[list[dict], int]:
    """The open tasks of one owner without a scan of them all: the oldest come off the
    ``(kind, status, created)`` index in order and stop at the cut; the count is the tag's task
    count less its closed ones (swept after a merge, so few)."""
    tag = _OWNER_TAG[owner]
    ranks = _ranks(conn)
    cols = "SELECT i.id, i.title, i.status, i.created FROM items i WHERE i.kind = 'T' AND i.status = ? "
    has = "AND EXISTS (SELECT 1 FROM tags t WHERE t.id = i.id AND t.tag = ?) "
    rows: dict[str, tuple] = {}
    for status in ("open", "claimed"):
        # ordered by the index alone (ties keep insertion order); a sort key of its own would
        # make the database read every open task first
        for r in conn.execute(cols + has + "ORDER BY i.created LIMIT ?", (status, tag, limit + len(ranks))):
            rows[r[0]] = r
    ranked = list(ranks)
    for n in range(0, len(ranked), 400):
        chunk = ranked[n:n + 400]
        for r in conn.execute(f"SELECT i.id, i.title, i.status, i.created FROM items i WHERE i.kind = 'T' "
                              f"AND i.status != 'closed' {has}AND i.id IN ({','.join('?' * len(chunk))})",
                              (tag, *chunk)):
            rows[r[0]] = r
    every = conn.execute("SELECT coalesce(sum(n), 0) FROM tag_counts WHERE tag = ? AND k = 'T'",
                         (tag,)).fetchone()[0]
    closed = conn.execute("SELECT count(*) FROM items i WHERE i.kind = 'T' AND i.status = 'closed' " + has,
                          (tag,)).fetchone()[0]
    return ([{"wiki": label, "id": tid, "title": title, "project": "", "owner": owner, "status": st,
              "created": created, "rank": ranks.get(tid)} for tid, title, st, created in rows.values()],
            max(0, int(every) - int(closed)))


def list_tasks(conn: sqlite3.Connection, label: str, *, owner: str = "human", project: str | None = None,
               status: str = "open", limit: int = LIMIT) -> tuple[list[dict], int]:
    """``(the tasks that can make the first *limit* of this wiki, how many match)``: the ranked
    ones, and the oldest *limit* (more by the number of ranks) of the rest. The database does the
    ordering and the cut, so a wiki with thousands of tasks hands over a hundred."""
    if owner not in OWNERS or status not in STATUSES:
        raise db.LedgerError("owner is human or agent; status is open, closed or all")
    if project:
        # drive from the project's own (few) tag rows, not from every task of the owner
        where = ("t.tag = ? AND t.id >= 'T' AND t.id < 'U' AND i.kind = 'T' "
                 "AND EXISTS (SELECT 1 FROM tags o WHERE o.id = i.id AND o.tag = ?)")
        args: list = [db.PROJECT_PREFIX + db.clean_project(project), _OWNER_TAG[owner]]
    else:
        where = "t.tag = ? AND i.kind = 'T'"
        args = [_OWNER_TAG[owner]]
    if status == "open":
        where += " AND i.status != 'closed'"
    elif status == "closed":
        where += " AND i.status = 'closed'"
    base = "SELECT i.id, i.title, i.status, i.created FROM tags t JOIN items i ON i.id = t.id WHERE " + where
    if status == "open" and not project:
        try:
            return _open_fast(conn, label, owner, limit)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                raise
            # a read-only schema-1 file has no tag counts: the plain query below answers
    total = conn.execute("SELECT count(*) FROM tags t JOIN items i ON i.id = t.id WHERE " + where,
                         args).fetchone()[0]
    # ranks come from one range read of the (few) ranked records, not one lookup per task
    ranks: dict[str, int] = {}
    for rid, t in conn.execute("SELECT id, tag FROM tags WHERE tag >= 'rank:' AND tag < 'rank;' "
                               "AND id >= 'T' AND id < 'U'"):
        n = t[len("rank:"):]
        if _RANK_RE.fullmatch(n):
            ranks[rid] = min(int(n), ranks.get(rid, 1000))
    rows = {r[0]: r for r in conn.execute(
        base + " ORDER BY i.created, CAST(substr(i.id, 2) AS INTEGER) LIMIT ?",
        (*args, limit + len(ranks))).fetchall()}
    ranked = list(ranks)
    for n in range(0, len(ranked), 400):
        chunk = ranked[n:n + 400]
        rows.update({r[0]: r for r in conn.execute(
            f"{base} AND i.id IN ({','.join('?' * len(chunk))})", (*args, *chunk)).fetchall()})
    return ([{"wiki": label, "id": tid, "title": title, "project": "", "owner": owner, "status": st,
              "created": created, "rank": ranks.get(tid)}
             for tid, title, st, created in rows.values()], int(total))


def _with_projects(conn: sqlite3.Connection, tasks: list[dict]) -> None:
    """Fill ``project`` of the tasks kept (a lookup each, so only after the cut)."""
    for t in tasks:
        row = conn.execute("SELECT tag FROM tags WHERE id = ? AND tag >= 'project:' AND tag < 'project;' "
                           "LIMIT 1", (t["id"],)).fetchone()
        t["project"] = row[0][len(db.PROJECT_PREFIX):] if row else ""


def order(tasks: list[dict]) -> list[dict]:
    """``rank:N`` first (lower first), then the oldest."""
    return sorted(tasks, key=lambda t: (t["rank"] is None, t["rank"] or 0, t["created"], t["wiki"],
                                        int(t["id"][1:])))


def wiki_label(root) -> str:
    from shapa import remotesync

    if remotesync.is_global(root):
        return "global"
    return remotesync.wiki_key(root) or remotesync.slug(remotesync.folder_name(root)) or "repo"


def scope_roots(cwd=None, everything: bool = False) -> list[Path]:
    from shapa import remotesync

    return [r for r in remotesync.wikis_in_scope(cwd, everything) if db.exists(r)]


def _key_of(root) -> str:
    """A wiki's identity: its resolved root path (never its label, which two wikis can share)."""
    try:
        return str(Path(root).resolve())
    except OSError:
        return str(root)


def labelled(roots) -> list[tuple[Path, str]]:
    """``[(root, label)]`` for *roots*, each wiki once (by path) with a label no other wiki here
    shares: ``global``, the wiki key, or - where two wikis have the same key - that key with the
    tail of the checkout's path added (``shared (a/shared)``)."""
    seen: dict[str, Path] = {}
    for r in roots:
        seen.setdefault(_key_of(r), Path(r))
    base = {k: wiki_label(p) for k, p in seen.items()}
    out = []
    for k, p in seen.items():
        label = base[k]
        rivals = [x for x, b in base.items() if b == label]
        if label != "global" and len(rivals) > 1:
            parts = {x: list(reversed(Path(x).parent.parts)) for x in rivals}  # <checkout>/.shapa
            for depth in range(1, 12):
                tails = {x: "/".join(reversed(parts[x][:depth])) for x in rivals}
                if len(set(tails.values())) == len(rivals) or depth >= max(len(v) for v in parts.values()):
                    label = f"{label} ({tails[k]})"
                    break
        out.append((p, label))
    return out


def open_tasks(roots, *, owner: str = "human", project: str | None = None, status: str = "open",
               wiki: str | None = None, limit: int = LIMIT, ranked: bool = True,
               conns: dict | None = None, busy_ms: int | None = None) -> dict:
    """``{"tasks": [...], "total": N}`` over *roots*, in the contract's order (``rank:N``, then
    oldest; *ranked* false: oldest only). Wikis are told apart by path: two with the same key show
    under distinct labels. *conns* lends open connections by root (a session start reuses its one);
    *busy_ms* makes a locked database skip instead of waiting."""
    found: list[dict] = []
    total = 0
    limit = max(1, min(LIMIT, int(limit)))
    reuse = {_key_of(p): c for p, c in (conns or {}).items()}
    opened: dict[str, sqlite3.Connection] = {}  # label -> the connection read through (own or lent)
    ours: list[sqlite3.Connection] = []
    try:
        for root, label in labelled(roots):
            if wiki and wiki not in (label, wiki_label(root)):
                continue
            conn = reuse.get(_key_of(root))
            if conn is None:
                conn = db.connect(root, busy_ms=busy_ms)
                if conn is not None:
                    ours.append(conn)
            if conn is None:
                continue
            try:
                part, n = list_tasks(conn, label, owner=owner, project=project, status=status, limit=limit)
            except sqlite3.OperationalError:
                if busy_ms is None:
                    raise
                continue  # a locked database is skipped by a read that chose not to wait
            found += part
            total += n
            opened[label] = conn
        if not ranked:
            for t in found:
                t["rank"] = None
        kept = order(found)[:limit]
        for label, conn in opened.items():
            mine = [t for t in kept if t["wiki"] == label]
            if mine:
                try:
                    _with_projects(conn, mine)
                except sqlite3.OperationalError:
                    if busy_ms is None:
                        raise
        return {"tasks": kept, "total": total}
    finally:
        for conn in ours:
            conn.close()


def render(task: dict) -> str:
    proj = f" [{task['project']}]" if task["project"] else ""
    rank = f" (rank {task['rank']})" if task["rank"] else ""
    return f"{task['wiki']}/{task['id']}{proj} {task['title']}{rank}"


def run_tasks(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa tasks", description="The operator's task list: open "
                                "human_task tasks across every wiki in scope.")
    p.add_argument("--cwd", default=None)
    p.add_argument("--owner", choices=OWNERS, default="human")
    p.add_argument("--project", default=None)
    p.add_argument("--status", choices=STATUSES, default="open")
    p.add_argument("--all", action="store_true", help="every registered wiki, not only the ones in scope")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    result = open_tasks(scope_roots(args.cwd, args.all), owner=args.owner, project=args.project,
                        status=args.status)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for t in result["tasks"]:
        print(render(t))
    shown = len(result["tasks"])
    if result["total"]:
        print(f"{result['total']} {args.status} {args.owner} task(s)"
              + (f", {shown} shown" if shown < result["total"] else ""))
    else:
        print(f"no {args.status} {args.owner} tasks")
    return 0


def main(argv: list[str] | None = None) -> None:
    run(run_tasks, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
