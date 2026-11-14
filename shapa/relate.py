"""Tags are the relations: the tag index, fetch by tag, and related records.

Everything here reads the database's own ``tags`` and ``links`` tables (and ``tag_counts``, the
per-tag count the triggers of :mod:`shapa.db` keep), so each read costs the size of its answer,
not of the wiki: a tag lookup is an index range, a count is one row per tag and kind.

* :func:`tag_index` - every tag with its record counts per type (``shapa tags``).
* :func:`topics` - the busiest non-system tags with rule, skill and memory counts (session start).
* :func:`fetch_by_tags` - the records carrying every tag listed (``shapa fetch --tag``).
* :func:`related` - records linked to one, then by shared non-system tags, then newest
  (``shapa related``).

CLI::

    shapa tags [--wiki KEY] [--json] [--all]
    shapa related ID [--limit N] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from shapa import db
from shapa.ledger import CommandError, run

TYPE_BY_LETTER = {"R": "rule", "M": "memory", "I": "issue", "T": "task", "J": "job", "F": "feature"}
COUNT_KEYS = ("rule", "memory", "issue", "skill", "document", "task", "job", "feature", "note")
TOPICS_SHOWN = 40
RELATED_DEFAULT, RELATED_MAX = 10, 50
#: ``related`` weighs at most this many candidate records: it takes them from the rarest shared
#: tags first, so a wiki that grows still gets an answer from the same bounded work.
RELATED_POOL = 600
#: Rows of the tag-count index a session start's topics list reads, however many tags exist.
TOPICS_ROWS_READ = 3000
MAX_TAGS_QUERIED = 8
TAG_FETCH_POOL = 2000  # records ranked for one tag fetch


def is_system_tag(tag: str) -> bool:
    return tag in db.SYSTEM_TAGS or tag.startswith(db.SYSTEM_TAG_PREFIXES)


def _skill_ids(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """``(every skill version, the active ones)``: a few documents, found by tag."""
    allv = {r[0] for r in conn.execute("SELECT id FROM tags WHERE tag = ? AND id >= 'D' AND id < 'E'",
                                       (db.SKILL_TAG,))}
    active = {r[0] for r in conn.execute("SELECT id FROM tags WHERE tag = 'status:active' "
                                         "AND id >= 'D' AND id < 'E'")} & allv
    return allv, active


def _counts_by_tag(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """``{tag: {type: records}}`` from the tag counts (a scan of the table for a file written
    before them)."""
    if db._has_table(conn, "tag_counts"):
        rows = conn.execute("SELECT tag, k, n FROM tag_counts").fetchall()
    else:
        rows = conn.execute("SELECT tag, substr(id, 1, 1), count(*) FROM tags "
                            "GROUP BY tag, substr(id, 1, 1)").fetchall()
    out: dict[str, dict[str, int]] = {}
    for tag, k, n in rows:
        name = "document" if k == db.DOC_KIND else TYPE_BY_LETTER.get(k)
        if name:
            out.setdefault(tag, {})[name] = out.get(tag, {}).get(name, 0) + int(n)
    allv, active = _skill_ids(conn)
    if allv:
        for tag, n in conn.execute(f"SELECT tag, count(*) FROM tags WHERE id IN ({','.join('?' * len(allv))}) "
                                   "GROUP BY tag", sorted(allv)):
            if tag in out:
                out[tag]["document"] = max(0, out[tag].get("document", 0) - int(n))
        if active:
            for tag, n in conn.execute(f"SELECT tag, count(*) FROM tags WHERE id IN "
                                       f"({','.join('?' * len(active))}) GROUP BY tag", sorted(active)):
                if tag in out:
                    out[tag]["skill"] = int(n)
    return out


def tag_index(conn: sqlite3.Connection) -> list[dict]:
    """``[{tag, total, counts: {rule, memory, issue, skill, document, task, job, feature, note}}]``,
    busiest first. Every tag appears, system tags too; skills count their active versions."""
    out = []
    for tag, by in _counts_by_tag(conn).items():
        counts = {k: by.get(k, 0) for k in COUNT_KEYS}
        total = sum(counts.values())
        if total:
            out.append({"tag": tag, "total": total, "counts": counts})
    return sorted(out, key=lambda e: (-e["total"], e["tag"]))


def topics(conn: sqlite3.Connection, limit: int = TOPICS_SHOWN) -> list[dict]:
    """The busiest tags that tie rules, skills and memories together (no system tag, none with
    no rule, skill or memory), for a session start."""
    if not db._has_table(conn, "tag_counts"):  # a schema-1 file: no index to walk
        out = []
        for e in tag_index(conn):
            c = e["counts"]
            if is_system_tag(e["tag"]) or not (c["rule"] or c["skill"] or c["memory"]):
                continue
            out.append({"tag": e["tag"], "rules": c["rule"], "skills": c["skill"],
                        "memories": c["memory"], "total": c["rule"] + c["skill"] + c["memory"]})
        out.sort(key=lambda e: (-e["total"], e["tag"]))
        return out[:limit]
    # Walk the count index busiest first and stop after a fixed number of rows: the cost does
    # not grow with the number of distinct tags (a hostile wiki can have hundreds of thousands).
    seen: dict[str, int] = {}
    for tag, k, n in conn.execute("SELECT tag, k, n FROM tag_counts WHERE k IN ('R', 'M', 'D') "
                                  "ORDER BY n DESC LIMIT ?", (TOPICS_ROWS_READ,)):
        if not is_system_tag(tag):
            seen[tag] = seen.get(tag, 0) + int(n)
    cands = sorted(seen, key=lambda t: (-seen[t], t))[:limit * 2]
    if not cands:
        return []
    counts: dict[str, dict[str, int]] = {t: {} for t in cands}
    for tag, k, n in conn.execute(f"SELECT tag, k, n FROM tag_counts WHERE tag IN ({','.join('?' * len(cands))}) "
                                  "AND k IN ('R', 'M', 'D')", cands):
        counts[tag][k] = int(n)
    allv, active = _skill_ids(conn)
    skills: dict[str, int] = {}
    if active and any(c.get("D") for c in counts.values()):
        act = sorted(active)
        for tag, n in conn.execute(f"SELECT tag, count(*) FROM tags WHERE tag IN ({','.join('?' * len(cands))}) "
                                   f"AND id IN ({','.join('?' * len(act))}) GROUP BY tag", [*cands, *act]):
            skills[tag] = int(n)
    out = []
    for t in cands:
        c = counts[t]
        rules, memories, skill = c.get("R", 0), c.get("M", 0), skills.get(t, 0)
        if rules or memories or skill:
            out.append({"tag": t, "rules": rules, "skills": skill, "memories": memories,
                        "total": rules + skill + memories})
    out.sort(key=lambda e: (-e["total"], e["tag"]))
    return out[:limit]


# --- reading records by id ---------------------------------------------------------------------

def _table(letter: str) -> str:
    return {"F": "items", "J": "items", "T": "items", "D": "docs"}.get(letter, "mri")


def describe(conn: sqlite3.Connection, ids) -> dict[str, dict]:
    """``{id: {id, type, summary, updated, created, status}}`` for the stored ids among *ids*."""
    out: dict[str, dict] = {}
    by_table: dict[str, list[str]] = {}
    for i in ids:
        by_table.setdefault(_table(i[:1]), []).append(i)
    for table, group in by_table.items():
        col = "summary" if table == "mri" else "title"
        extra = ", status" if table == "items" else ""
        for n in range(0, len(group), 400):
            chunk = group[n:n + 400]
            for r in conn.execute(f"SELECT id, {col}, updated, created{extra} FROM {table} "
                                  f"WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                letter = r[0][:1]
                out[r[0]] = {"id": r[0], "type": TYPE_BY_LETTER.get(letter, "document"), "summary": r[1],
                             "updated": r[2], "created": r[3], "status": r[4] if table == "items" else ""}
    allv = None
    for i, d in out.items():
        if i[:1] == db.DOC_KIND:
            if allv is None:
                allv = _skill_ids(conn)[0]
            if i in allv:
                d["type"] = "skill"
    return out


def stages_of(conn: sqlite3.Connection, ids) -> dict[str, int]:
    ids = list(ids)
    out: dict[str, int] = {}
    for n in range(0, len(ids), 400):
        chunk = ids[n:n + 400]
        for rid, tag in conn.execute(f"SELECT id, tag FROM tags WHERE id IN ({','.join('?' * len(chunk))}) "
                                     "AND tag >= 'stage:' AND tag < 'stage;'", chunk):
            s = db.stage_of([tag])
            if s is not None:
                out[rid] = s
    return out


def clean_query_tags(tags) -> list[str]:
    """The tags of a query as stored (collapsed whitespace, cut at 80), at most a few, none empty."""
    out = list(dict.fromkeys(t for t in (db._clean_tag(x) for x in (tags or []) if isinstance(x, str)) if t))
    if not out:
        raise db.LedgerError("give at least one tag")
    if len(out) > MAX_TAGS_QUERIED:
        raise db.LedgerError(f"at most {MAX_TAGS_QUERIED} tags in one query")
    return out


def ids_with_tags(conn: sqlite3.Connection, tags: list[str], limit: int = TAG_FETCH_POOL) -> list[str]:
    """Ids carrying every one of *tags*, found from the rarest tag outward."""
    weight = {t: 0 for t in tags}
    if db._has_table(conn, "tag_counts"):
        for t, n in conn.execute(f"SELECT tag, sum(n) FROM tag_counts WHERE tag IN ({','.join('?' * len(tags))}) "
                                 "GROUP BY tag", tags):
            weight[t] = int(n)
    order = sorted(tags, key=lambda t: (weight[t], t))
    if weight[order[0]] == 0:
        return []
    sql = "SELECT t0.id FROM tags t0 WHERE t0.tag = ?"
    args: list = [order[0]]
    for t in order[1:]:
        sql += " AND EXISTS (SELECT 1 FROM tags x WHERE x.id = t0.id AND x.tag = ?)"
        args.append(t)
    return [r[0] for r in conn.execute(sql + " LIMIT ?", (*args, limit))]


def stage_key(stage: int | None) -> int:
    """A record with no stage is on demand: it ranks after every staged one."""
    return db.STAGE_MAX + 1 if stage is None else stage


def fetch_by_tags(conn: sqlite3.Connection, tags, *, k: int = 8) -> list[dict]:
    """Records carrying every tag, lowest stage first, then newest: ``[{id, type, summary,
    stage, updated}]`` (at most *k*)."""
    ids = ids_with_tags(conn, clean_query_tags(tags))
    meta = describe(conn, ids)
    stages = stages_of(conn, meta)
    focus = {r[0] for r in conn.execute("SELECT id FROM tags WHERE tag = ?", (db.FOCUS_TAG,))}
    rows = [{**m, "stage": stages.get(i)} for i, m in meta.items() if i not in focus]
    rows.sort(key=lambda r: r["id"])
    rows.sort(key=lambda r: r["updated"], reverse=True)  # stable: newest first, then by stage
    rows.sort(key=lambda r: stage_key(r["stage"]))
    return rows[:max(1, k)]


# --- related ------------------------------------------------------------------------------------

def related(conn: sqlite3.Connection, record_id: str, limit: int = RELATED_DEFAULT) -> list[dict]:
    """Records related to *record_id*: those linked to it in either direction first, then those
    sharing the most non-system tags, then the newest."""
    limit = max(1, min(RELATED_MAX, int(limit)))
    links: dict[str, list[str]] = {}
    for src, dst, rel in db.links_of(conn, record_id):
        other = dst if src == record_id else src
        if other != record_id:
            links.setdefault(other, []).append(rel)
    mine = [t for t in db._tags_of(conn, record_id) if not is_system_tag(t)]
    weight: dict[str, int] = {}
    if db._has_table(conn, "tag_counts") and mine:
        weight = dict(conn.execute(f"SELECT tag, sum(n) FROM tag_counts WHERE tag IN ({','.join('?' * len(mine))}) "
                                   "GROUP BY tag", mine).fetchall())
    # candidates come from the rarest shared tags first, at most RELATED_POOL of them; a tag on
    # thousands of records still answers, with the newest ids of the tag
    pool: set[str] = set()
    for t in sorted(mine, key=lambda t: (weight.get(t, 0), t)):
        if len(pool) >= RELATED_POOL:
            break
        for (i,) in conn.execute("SELECT id FROM tags WHERE tag = ? ORDER BY id DESC LIMIT ?",
                                 (t, RELATED_POOL - len(pool) + 1)):
            if i != record_id and i not in links:
                pool.add(i)
    shared: dict[str, list[str]] = {}
    if pool and mine:
        ids = sorted(pool)
        for n in range(0, len(ids), 300):
            chunk = ids[n:n + 300]
            for i, t in conn.execute(f"SELECT id, tag FROM tags WHERE id IN ({','.join('?' * len(chunk))}) "
                                     f"AND tag IN ({','.join('?' * len(mine))})", [*chunk, *mine]):
                shared.setdefault(i, []).append(t)
    ordered = sorted(shared, key=lambda i: (-len(shared[i]), i))
    if len(ordered) > limit:  # only the tiers that can reach the cut are looked up for "newest"
        cut = len(shared[ordered[limit - 1]])
        ordered = [i for i in ordered if len(shared[i]) >= cut]
    meta = describe(conn, [*links, *ordered])
    def newest(ids):
        ids = sorted(ids)
        return sorted(ids, key=lambda i: meta[i]["updated"], reverse=True)  # stable: ties by id

    linked = newest(i for i in links if i in meta)
    rest = sorted(newest(i for i in ordered if i in meta), key=lambda i: -len(shared[i]))
    out = []
    for i in [*linked, *rest][:limit]:
        m = meta[i]
        out.append({"id": i, "type": m["type"], "summary": m["summary"], "links": sorted(set(links.get(i, []))),
                    "shared": sorted(shared.get(i, []))})
    return out


# --- the CLI --------------------------------------------------------------------------------------

def _roots(cwd, everything: bool = False):
    from shapa import tasks

    return tasks.scope_roots(cwd, everything)


def run_tags(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa tags", description="Every tag in the wikis in scope with "
                                "its record counts: tags are how rules, memories, skills, tasks and "
                                "notes relate.")
    p.add_argument("--cwd", default=None)
    p.add_argument("--wiki", default=None, help="only this wiki (global, or a repo wiki's key)")
    p.add_argument("--all", action="store_true", help="every registered wiki")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    from shapa import tasks

    found = []
    for root in _roots(args.cwd, args.all):
        label = tasks.wiki_label(root)
        if args.wiki and args.wiki != label:
            continue
        conn = db.connect(root)
        if conn is None:
            continue
        try:
            found.append((label, tag_index(conn)))
        finally:
            conn.close()
    merged: dict[str, dict] = {}
    for _, index in found:
        for e in index:
            m = merged.setdefault(e["tag"], {"tag": e["tag"], "total": 0, "counts": {k: 0 for k in COUNT_KEYS}})
            m["total"] += e["total"]
            for k, n in e["counts"].items():
                m["counts"][k] += n
    tags = sorted(merged.values(), key=lambda e: (-e["total"], e["tag"]))
    if args.json:
        print(json.dumps({"tags": tags}, indent=2))
        return 0
    for e in tags:
        parts = ", ".join(f"{k} {n}" for k, n in e["counts"].items() if n)
        print(f"{e['tag']}  {e['total']}  ({parts})")
    if not tags:
        print("no tags yet")
    return 0


def run_related(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa related", description="Records related to one: linked "
                                "first, then those sharing tags, then the newest.")
    p.add_argument("--cwd", default=None)
    p.add_argument("id")
    p.add_argument("--limit", type=int, default=RELATED_DEFAULT)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    for root in _roots(args.cwd):
        conn = db.connect(root)
        if conn is None:
            continue
        try:
            rid = _resolve(conn, args.id)
            if rid is None:
                continue
            found = related(conn, rid, args.limit)
        finally:
            conn.close()
        if args.json:
            print(json.dumps({"id": rid, "related": found}, indent=2))
        else:
            for r in found:
                why = ", ".join([*(f"linked {x}" for x in r["links"]), *(f"tag {t}" for t in r["shared"])])
                print(f"{r['id']} ({r['type']}) {r['summary']}  [{why}]")
            if not found:
                print(f"nothing related to {rid}")
        return 0
    raise CommandError(f"no record {args.id!r} in a wiki in scope")


def _resolve(conn: sqlite3.Connection, ident: str) -> str | None:
    ident = (ident or "").strip()
    for getter in (db.get_row, db.get_doc, db.get_item):
        found = getter(conn, ident)
        if found is not None:
            return found.id
    return None


def resolve(conn: sqlite3.Connection, ident: str) -> str | None:
    return _resolve(conn, ident)


def main_tags(argv: list[str] | None = None) -> None:
    run(run_tags, list(sys.argv[1:] if argv is None else argv))


def main_related(argv: list[str] | None = None) -> None:
    run(run_related, list(sys.argv[1:] if argv is None else argv))
