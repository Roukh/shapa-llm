"""Database primitives the hosted-sync apply step needs (``shapa.sync``).

All writers here run inside the caller's open write transaction
(:func:`shapa.db.write`) so one sync applies as one atomic unit. Input records
are the already validated, redacted wire records of ``shapa.syncrec``.

- :func:`snapshot` reads every F/J/T item, M/R/I row and D document as wire
  records (``{id, uid, kind, created, updated, rev, deleted, device, data,
  tags, links}``; ``rev``/``device`` are the server's and left neutral here; a
  record with no stored uid reports its legacy uid).
- :func:`upsert` writes a record under its explicit id and raises the counter.
- :func:`relabel` renames an id everywhere (the item's own row, its children's
  ``parent``, tags, links); foreign keys must be deferred by the caller.
- tombstones and counters.
"""

from __future__ import annotations

import re
import sqlite3

from shapa import db

ITEM_DATA = ("title", "body", "parent", "status", "closed_reason", "git_ref", "alias",
             "claimed_by", "closed")
ROW_DATA = ("summary", "body", "alias", "source", "session")
DOC_DATA = ("title", "body", "alias", "mime", "source")
COUNTER_KINDS = db.ITEM_KINDS + db.MRI_KINDS + (db.DOC_KIND,)


def _wire(row, kind: str, fields, tags, links, uids) -> dict:
    return {"id": row["id"], "uid": uids.get(row["id"]) or db.legacy_uid(row["id"], row["created"]),
            "kind": kind, "created": row["created"], "updated": row["updated"],
            "rev": 0, "deleted": False, "device": "",
            "data": {f: row[f] for f in fields},
            "tags": sorted(tags.get(row["id"], ())),
            "links": sorted([dst, rel] for dst, rel in links.get(row["id"], ()))}


def snapshot(conn: sqlite3.Connection) -> dict[str, dict]:
    tags: dict[str, list[str]] = {}
    for rid, tag in conn.execute("SELECT id, tag FROM tags"):
        tags.setdefault(rid, []).append(tag)
    links: dict[str, list[tuple[str, str]]] = {}
    for src, dst, rel in conn.execute("SELECT src, dst, rel FROM links"):
        links.setdefault(src, []).append((dst, rel))
    uids = ({rid: uid for rid, uid in conn.execute("SELECT id, uid FROM uids")}
            if db._has_table(conn, "uids") else {})
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM items"):
        out[r["id"]] = _wire(r, r["kind"], ITEM_DATA, tags, links, uids)
    for r in conn.execute("SELECT * FROM mri"):
        out[r["id"]] = _wire(r, r["kind"], ROW_DATA, tags, links, uids)
    if db._has_table(conn, "docs"):
        for r in conn.execute("SELECT * FROM docs"):
            out[r["id"]] = _wire(r, db.DOC_KIND, DOC_DATA, tags, links, uids)
    return out


def counters(conn: sqlite3.Connection) -> dict[str, int]:
    return {k: int(n) for k, n in conn.execute("SELECT kind, next FROM counters")
            if k in COUNTER_KINDS}


def raise_counter(conn: sqlite3.Connection, kind: str, next_value: int) -> None:
    """Make *kind*'s next number at least *next_value* (never lowers it)."""
    conn.execute("INSERT OR IGNORE INTO counters(kind, next) VALUES (?, 1)", (kind,))
    conn.execute("UPDATE counters SET next = ? WHERE kind = ? AND next < ?",
                 (next_value, kind, next_value))


def raise_counters(conn: sqlite3.Connection, remote: dict[str, int]) -> None:
    for kind, n in remote.items():
        if kind in COUNTER_KINDS:
            raise_counter(conn, kind, n)


def local_tombstones(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{id: {id, kind, created, deleted_at, uid}}``."""
    return {t["id"]: t for t in db.tombstones(conn)}


def free_id(conn: sqlite3.Connection, kind: str, avoid=()) -> str:
    """A label of *kind* that no record, tombstone or *avoid* entry uses, taken
    from the counter, which is raised past it."""
    row = conn.execute("SELECT next FROM counters WHERE kind = ?", (kind,)).fetchone()
    n = int(row[0]) if row else 1
    while True:
        candidate = f"{kind}{n}"
        stone = conn.execute("SELECT 1 FROM tombstones WHERE id = ?", (candidate,)).fetchone()
        if not exists(conn, candidate) and not stone and candidate not in avoid:
            raise_counter(conn, kind, n + 1)
            return candidate
        n += 1


def drop_tombstones(conn: sqlite3.Connection, ids) -> None:
    if db._has_table(conn, "tombstones"):
        for i in ids:
            conn.execute("DELETE FROM tombstones WHERE id = ?", (i,))


def exists(conn: sqlite3.Connection, record_id: str) -> bool:
    return any(conn.execute(f"SELECT 1 FROM {t} WHERE id = ?", (record_id,)).fetchone()
               for t in ("items", "mri", "docs") if db._has_table(conn, t))


def _alias_free(conn, table: str, record_id: str, alias) -> str | None:
    """*alias* unless a different row already uses it (aliases are unique for
    rows and documents; the incoming record keeps its id, loses the alias)."""
    if not alias:
        return None
    clash = conn.execute(f"SELECT 1 FROM {table} WHERE alias = ? AND id != ?",
                         (alias, record_id)).fetchone()
    return None if clash else alias


_HASH_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


def safe_git_ref(item_id: str, ref) -> bool:
    """A remote ``git_ref`` is trusted only as a commit hash or as a branch named
    for this very item: ``shapa ledger reconcile`` tests it for "merged" and then
    closes the feature, so a value like ``main~1`` must not get in."""
    if not isinstance(ref, str):
        return False
    return bool(_HASH_RE.match(ref)) or (bool(_BRANCH_RE.match(ref))
                                         and db.feature_of_branch(ref) == item_id)


class UidConflict(ValueError):
    """An upsert that would change the uid of the record holding that id."""


def upsert(conn: sqlite3.Connection, rec: dict) -> None:
    """Write *rec* under its own id: insert, or update in place when the id is
    held by the same uid. Raises the kind's counter. It never changes a
    record's uid; the caller moves a different record away first."""
    rid, kind, d = rec["id"], rec["kind"], rec["data"]
    held = conn.execute("SELECT uid FROM uids WHERE id = ?", (rid,)).fetchone()
    if held and held[0] and held[0] != rec["uid"] and exists(conn, rid):
        raise UidConflict(rid)
    conn.execute("INSERT OR REPLACE INTO uids(id, uid) VALUES (?, ?)", (rid, rec["uid"]))
    if kind in db.ITEM_KINDS:
        parent = d["parent"] if d["parent"] and conn.execute(
            "SELECT 1 FROM items WHERE id = ?", (d["parent"],)).fetchone() else None
        local = conn.execute("SELECT git_ref FROM items WHERE id = ?", (rid,)).fetchone()
        git_ref = d["git_ref"] if safe_git_ref(rid, d["git_ref"]) else (local[0] if local else None)
        vals = (kind, parent, d["title"], d["body"], d["status"], d["closed_reason"],
                git_ref, d["alias"], d["claimed_by"], rec["created"], rec["updated"], d["closed"])
        if local is not None:  # the local `verify` is never touched
            conn.execute("UPDATE items SET kind=?, parent=?, title=?, body=?, status=?, "
                         "closed_reason=?, git_ref=?, alias=?, claimed_by=?, created=?, updated=?, "
                         "closed=? WHERE id=?", (*vals, rid))
        else:
            conn.execute("INSERT INTO items(kind, parent, title, body, status, closed_reason, "
                         "git_ref, alias, claimed_by, created, updated, closed, id) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*vals, rid))
    elif kind in db.MRI_KINDS:
        alias = _alias_free(conn, "mri", rid, d["alias"])
        vals = (kind, alias, d["summary"], d["body"], d["source"], d["session"],
                db.content_hash(d["summary"], d["body"]), rec["created"], rec["updated"])
        if conn.execute("SELECT 1 FROM mri WHERE id = ?", (rid,)).fetchone():
            conn.execute("UPDATE mri SET kind=?, alias=?, summary=?, body=?, source=?, session=?, "
                         "hash=?, created=?, updated=? WHERE id=?", (*vals, rid))
        else:
            conn.execute("INSERT INTO mri(kind, alias, summary, body, source, session, hash, "
                         "created, updated, id) VALUES (?,?,?,?,?,?,?,?,?,?)", (*vals, rid))
    else:
        alias = _alias_free(conn, "docs", rid, d["alias"])
        vals = (alias, d["title"], d["body"], d["mime"], d["source"],
                db.content_hash(d["title"], d["body"]), rec["created"], rec["updated"])
        if conn.execute("SELECT 1 FROM docs WHERE id = ?", (rid,)).fetchone():
            conn.execute("UPDATE docs SET alias=?, title=?, body=?, mime=?, source=?, hash=?, "
                         "created=?, updated=? WHERE id=?", (*vals, rid))
        else:
            conn.execute("INSERT INTO docs(alias, title, body, mime, source, hash, created, "
                         "updated, id) VALUES (?,?,?,?,?,?,?,?,?)", (*vals, rid))
    conn.execute("DELETE FROM tags WHERE id = ?", (rid,))
    conn.execute("DELETE FROM links WHERE src = ?", (rid,))
    for tag in rec["tags"]:
        conn.execute("INSERT OR IGNORE INTO tags(id, tag) VALUES (?, ?)", (rid, tag))
    for dst, rel in rec["links"]:
        conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) VALUES (?, ?, ?)", (rid, dst, rel))
    # the project tag and its `about` link are one relation: keep them in step for a pulled record
    key = db.project_of(rec["tags"]) if kind == db.DOC_KIND else None
    if key:
        db._link_project_members(conn, rid, key)
    else:
        db.sync_project_link(conn, rid)
    raise_counter(conn, kind, int(rid[1:]) + 1)


def relabel(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Rename *old* to *new* everywhere. The caller guarantees *new* is free
    and of the same kind, and has deferred foreign keys."""
    table = {"F": "items", "J": "items", "T": "items", "D": "docs"}.get(old[:1], "mri")
    row = conn.execute(f"SELECT created FROM {table} WHERE id = ?", (old,)).fetchone()
    if row is not None:  # a pre-uid record keeps the uid its old label derived
        conn.execute("INSERT OR IGNORE INTO uids(id, uid) VALUES (?, ?)",
                     (old, db.legacy_uid(old, row[0])))
    conn.execute("DELETE FROM uids WHERE id = ?", (new,))
    conn.execute("UPDATE uids SET id = ? WHERE id = ?", (new, old))
    conn.execute(f"UPDATE {table} SET id = ? WHERE id = ?", (new, old))
    if table == "items":
        conn.execute("UPDATE items SET parent = ? WHERE parent = ?", (new, old))
    conn.execute("UPDATE tags SET id = ? WHERE id = ?", (new, old))
    conn.execute("UPDATE OR IGNORE links SET src = ? WHERE src = ?", (new, old))
    conn.execute("UPDATE OR IGNORE links SET dst = ? WHERE dst = ?", (new, old))
    conn.execute("DELETE FROM links WHERE src = ? OR dst = ?", (old, old))
    raise_counter(conn, old[:1], int(new[1:]) + 1)
