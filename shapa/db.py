"""The wiki database - format 4's one store for work and memory.

``<wiki>/shapa.db`` holds two ledgers, both tracked in git as the file itself:

- **items** - the work ledger. ``F`` features hold ``J`` jobs, which hold
  ``T`` tasks. A feature is a branch and a PR and closes on merge; a job is
  one commit (message starts ``J<n>:``) and closes on that commit; a task
  has no git artifact and closes when an agent says so. IDs are a kind
  letter plus a per-wiki counter, never reused; the hierarchy lives in
  ``parent``.
- **mri** - memories (``M``: something the operator said that matters, or
  a strategy shift), rules (``R``) and issues (``I``: an agent mistake the
  operator corrected). Same ID scheme.

``tags`` and ``links`` relate any two ids (items or rows). Vectors are not
stored here: they are derived from the text and cached in the gitignored
index (``shapa.memlog``), so re-embedding never rewrites a tracked file.

The file is the source of truth and is committed whole, so it must never be
committed from two branches. In a linked worktree, :func:`db_path` resolves
to the primary checkout's file: every worktree of a repo shares one
database and one ID counter, and a feature branch never changes its own
checked-out copy. The rollback journal (not WAL) keeps the file complete
between transactions, so ``git add`` never sees half a write.

Standard library only (``sqlite3``).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_FILENAME = "shapa.db"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 10_000
EXPIRY_DAYS = 30
SUMMARY_MAX = 160
#: An M row nobody recalled for this long is dropped by the sweep. R and I
#: rows are never dropped for disuse - only as duplicates or when superseded.
STALE_MEMORY_DAYS = 60
DUP_COSINE = 0.95
CONFLICT_COSINE = 0.80

ITEM_KINDS = ("F", "J", "T")
MRI_KINDS = ("M", "R", "I")
KIND_NAMES = {"F": "feature", "J": "job", "T": "task",
              "M": "memory", "R": "rule", "I": "issue"}
#: A job's parent is a feature; a task's parent is a job. Both may stand alone.
PARENT_KIND = {"J": "F", "T": "J"}
CLOSE_REASONS = ("merge", "commit", "agent", "expired", "parent", "operator")
LINK_RELS = ("about", "supersedes", "conflicts", "related")
ID_RE = re.compile(r"^([FJTMRI])([1-9][0-9]*)$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (kind TEXT PRIMARY KEY, next INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('F','J','T')),
    parent TEXT REFERENCES items(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    verify TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','claimed','closed')),
    closed_reason TEXT CHECK (closed_reason IS NULL OR closed_reason IN
        ('merge','commit','agent','expired','parent','operator')),
    git_ref TEXT,
    alias TEXT,
    claimed_by TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    closed TEXT
);
CREATE INDEX IF NOT EXISTS items_parent ON items(parent);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE TABLE IF NOT EXISTS mri (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('M','R','I')),
    alias TEXT UNIQUE,
    summary TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    session TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mri_hash ON mri(hash);
CREATE TABLE IF NOT EXISTS tags (
    id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (id, tag)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL, dst TEXT NOT NULL,
    rel TEXT NOT NULL CHECK (rel IN ('about','supersedes','conflicts','related')),
    PRIMARY KEY (src, dst, rel)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
"""


class LedgerError(ValueError):
    """A ledger operation the data does not allow (unknown id, wrong kind)."""


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def content_hash(summary: str, body: str) -> str:
    return hashlib.sha1(f"{summary}\n{body}".encode("utf-8")).hexdigest()


def one_line(text: str, limit: int = SUMMARY_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# --- where the file lives -------------------------------------------------------

def _primary_checkout(start: Path) -> tuple[Path, Path] | None:
    """``(worktree_root, primary_root)`` when *start* sits inside a linked git
    worktree, else ``None``. Read from the ``.git`` file and ``commondir``,
    never by shelling out to git (hooks call this on every prompt)."""
    for d in (start, *start.parents):
        dotgit = d / ".git"
        try:
            if dotgit.is_dir():
                return None
            if not dotgit.is_file():
                continue
            text = dotgit.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text.startswith("gitdir:"):
            return None
        gitdir = Path(text.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = (d / gitdir).resolve()
        try:
            common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        except OSError:
            return None  # a submodule, not a linked worktree
        common_dir = Path(common)
        if not common_dir.is_absolute():
            common_dir = (gitdir / common_dir).resolve()
        return d, common_dir.parent
    return None


def db_path(root) -> Path:
    """The database for the wiki at *root*. In a linked worktree, the primary
    checkout's file at the same relative path."""
    root = Path(root).resolve()
    mapped = _primary_checkout(root)
    if mapped is not None:
        worktree, primary = mapped
        try:
            return primary / root.relative_to(worktree) / DB_FILENAME
        except ValueError:
            pass
    return root / DB_FILENAME


def exists(root) -> bool:
    return db_path(root).is_file()


def connect(root, *, create: bool = False) -> sqlite3.Connection | None:
    """Open *root*'s database (``None`` when it has none and *create* is
    false). Foreign keys on, rollback journal, a generous busy timeout so
    hooks from parallel sessions queue instead of failing."""
    path = db_path(root)
    if not path.is_file():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "delete":
        conn.execute("PRAGMA journal_mode = DELETE")
    if create or not _has_schema(conn):
        with write(conn):
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)",
                         (str(SCHEMA_VERSION),))
            for kind in ITEM_KINDS + MRI_KINDS:
                conn.execute("INSERT OR IGNORE INTO counters(kind, next) VALUES (?, 1)", (kind,))
    return conn


def read_rows(root) -> list["Row"]:
    """Every M/R/I row of *root*'s database, opened read-only (never creates
    or migrates the file; ``[]`` when there is none or it is unreadable)."""
    path = db_path(root)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        try:
            if not _has_schema(conn):
                return []
            return rows(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _has_schema(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone() is not None


@contextmanager
def write(conn: sqlite3.Connection):
    """One immediate transaction: the write lock is taken up front, so two
    hooks allocating IDs at once serialize instead of colliding."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _next_id(conn: sqlite3.Connection, kind: str) -> str:
    row = conn.execute("SELECT next FROM counters WHERE kind = ?", (kind,)).fetchone()
    n = int(row[0]) if row else 1
    conn.execute("INSERT OR REPLACE INTO counters(kind, next) VALUES (?, ?)", (kind, n + 1))
    return f"{kind}{n}"


def kind_of(item_id: str) -> str | None:
    m = ID_RE.match(item_id or "")
    return m.group(1) if m else None


# --- the work ledger -------------------------------------------------------------

@dataclass
class Item:
    id: str
    kind: str
    parent: str | None
    title: str
    body: str
    verify: str | None
    status: str
    closed_reason: str | None
    git_ref: str | None
    alias: str | None
    claimed_by: str | None
    created: str
    updated: str
    closed: str | None

    @classmethod
    def from_row(cls, row) -> "Item":
        return cls(**{k: row[k] for k in row.keys()})


def get_item(conn: sqlite3.Connection, item_id: str) -> Item | None:
    row = conn.execute("SELECT * FROM items WHERE id = ? OR alias = ?",
                       (item_id, item_id)).fetchone()
    return Item.from_row(row) if row else None


def _require_item(conn, item_id: str) -> Item:
    item = get_item(conn, item_id)
    if item is None:
        raise LedgerError(f"no ledger item {item_id!r}")
    return item


def add_item(conn: sqlite3.Connection, kind: str, title: str, *, parent: str | None = None,
             body: str = "", verify: str | None = None, alias: str | None = None,
             git_ref: str | None = None, now: datetime | None = None) -> str:
    kind = kind.upper()
    if kind not in ITEM_KINDS:
        raise LedgerError(f"item kind must be one of {ITEM_KINDS}, got {kind!r}")
    title = one_line(title, 200)
    if not title:
        raise LedgerError("title is required")
    with write(conn):
        if parent:
            p = _require_item(conn, parent)
            if PARENT_KIND.get(kind) != p.kind:
                raise LedgerError(f"a {KIND_NAMES[kind]}'s parent must be a "
                                  f"{KIND_NAMES.get(PARENT_KIND.get(kind, ''), 'nothing')}, "
                                  f"not {p.id}")
            if p.status == "closed":
                raise LedgerError(f"{p.id} is closed")
            parent = p.id
        new_id = _next_id(conn, kind)
        ts = now_iso(now)
        conn.execute(
            "INSERT INTO items(id, kind, parent, title, body, verify, alias, git_ref, created, "
            "updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id, kind, parent, title, body, verify, alias, git_ref, ts, ts))
    return new_id


def update_item(conn: sqlite3.Connection, item_id: str, **fields) -> Item:
    allowed = {"title", "body", "verify", "git_ref", "alias"}
    bad = set(fields) - allowed
    if bad:
        raise LedgerError(f"cannot set {sorted(bad)}")
    with write(conn):
        item = _require_item(conn, item_id)
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE items SET {sets}, updated = ? WHERE id = ?",
                         (*fields.values(), now_iso(), item.id))
    return _require_item(conn, item.id)


def claim(conn: sqlite3.Connection, item_id: str, session: str) -> Item:
    with write(conn):
        item = _require_item(conn, item_id)
        if item.status == "closed":
            raise LedgerError(f"{item.id} is closed")
        conn.execute("UPDATE items SET status='claimed', claimed_by=?, updated=? WHERE id=?",
                     (session or "", now_iso(), item.id))
    return _require_item(conn, item.id)


def release(conn: sqlite3.Connection, item_id: str) -> Item:
    with write(conn):
        item = _require_item(conn, item_id)
        if item.status == "claimed":
            conn.execute("UPDATE items SET status='open', claimed_by=NULL, updated=? WHERE id=?",
                         (now_iso(), item.id))
    return _require_item(conn, item.id)


def _descendants(conn, item_id: str) -> list[str]:
    rows = conn.execute(
        "WITH RECURSIVE sub(id) AS (SELECT id FROM items WHERE parent = ? "
        "UNION ALL SELECT i.id FROM items i JOIN sub ON i.parent = sub.id) SELECT id FROM sub",
        (item_id,)).fetchall()
    return [r[0] for r in rows]


def close(conn: sqlite3.Connection, item_id: str, reason: str, *, git_ref: str | None = None,
          now: datetime | None = None) -> list[str]:
    """Close *item_id* and every open descendant (reason ``parent``).
    Returns the ids closed, the item first; an already-closed item closes
    nothing."""
    if reason not in CLOSE_REASONS:
        raise LedgerError(f"reason must be one of {CLOSE_REASONS}")
    ts = now_iso(now)
    with write(conn):
        item = _require_item(conn, item_id)
        if item.status == "closed":
            return []
        conn.execute("UPDATE items SET status='closed', closed_reason=?, closed=?, updated=?, "
                     "git_ref=COALESCE(?, git_ref) WHERE id=?",
                     (reason, ts, ts, git_ref, item.id))
        closed = [item.id]
        for child in _descendants(conn, item.id):
            cur = conn.execute("UPDATE items SET status='closed', closed_reason='parent', "
                               "closed=?, updated=? WHERE id=? AND status != 'closed'",
                               (ts, ts, child))
            if cur.rowcount:
                closed.append(child)
    return closed


def items(conn: sqlite3.Connection, *, status: str | None = None, kind: str | None = None,
          parent: str | None = None, claimed_by: str | None = None) -> list[Item]:
    sql, args = "SELECT * FROM items WHERE 1=1", []
    if status == "live":
        sql += " AND status != 'closed'"
    elif status:
        sql += " AND status = ?"
        args.append(status)
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if parent:
        sql += " AND parent = ?"
        args.append(parent)
    if claimed_by is not None:
        sql += " AND claimed_by = ? AND status = 'claimed'"
        args.append(claimed_by)
    sql += " ORDER BY kind, CAST(SUBSTR(id, 2) AS INTEGER)"
    return [Item.from_row(r) for r in conn.execute(sql, args)]


def tree(conn: sqlite3.Connection, item_id: str) -> list[tuple[int, Item]]:
    """*item_id* and its descendants as ``(depth, item)``, depth-first."""
    root = _require_item(conn, item_id)
    out: list[tuple[int, Item]] = []

    def walk(item: Item, depth: int) -> None:
        out.append((depth, item))
        for child in items(conn, parent=item.id):
            walk(child, depth + 1)

    walk(root, 0)
    return out


def feature_of_branch(branch: str) -> str | None:
    """``F12`` from a branch named ``F12-...`` (or ``f12-...``)."""
    m = re.match(r"^(?:.*/)?[Ff]([1-9][0-9]*)(?:[-_/]|$)", branch or "")
    return f"F{m.group(1)}" if m else None


def job_of_message(message: str) -> str | None:
    """``J7`` from a commit message whose subject starts ``J7:`` or ``J7 ``."""
    m = re.match(r"^\s*[Jj]([1-9][0-9]*)\b[:\s]", message or "")
    return f"J{m.group(1)}" if m else None


# --- memories, rules, issues --------------------------------------------------------

@dataclass
class Row:
    id: str
    kind: str
    alias: str | None
    summary: str
    body: str
    source: str
    session: str
    hash: str
    created: str
    updated: str
    tags: list[str]

    def embed_text(self) -> str:
        return f"{KIND_NAMES[self.kind]} {' '.join(self.tags)}\n{self.summary}\n{self.body}"


def _tags_of(conn, row_id: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT tag FROM tags WHERE id = ? ORDER BY tag", (row_id,))]


def get_row(conn: sqlite3.Connection, row_id: str) -> Row | None:
    r = conn.execute("SELECT * FROM mri WHERE id = ? OR alias = ?", (row_id, row_id)).fetchone()
    if r is None:
        return None
    return Row(**{k: r[k] for k in r.keys()}, tags=_tags_of(conn, r["id"]))


def rows(conn: sqlite3.Connection, kind: str | None = None) -> list[Row]:
    sql = "SELECT * FROM mri" + (" WHERE kind = ?" if kind else "")
    sql += " ORDER BY kind, CAST(SUBSTR(id, 2) AS INTEGER)"
    tag_map: dict[str, list[str]] = {}
    for rid, tag in conn.execute("SELECT id, tag FROM tags ORDER BY tag"):
        tag_map.setdefault(rid, []).append(tag)
    return [Row(**{k: r[k] for k in r.keys()}, tags=tag_map.get(r["id"], []))
            for r in conn.execute(sql, (kind,) if kind else ())]


def _clean_tag(tag: str) -> str:
    return " ".join(str(tag).split())[:80]


def add_row(conn: sqlite3.Connection, kind: str, summary: str, body: str = "", *,
            tags=(), about=(), alias: str | None = None, source: str = "",
            session: str = "", now: datetime | None = None) -> tuple[str, bool]:
    """Write one M/R/I row. Returns ``(id, created)``: identical text already
    stored returns that row's id with ``created`` false (its tags and
    *about* links are still added)."""
    kind = kind.upper()
    if kind not in MRI_KINDS:
        raise LedgerError(f"row kind must be one of {MRI_KINDS}, got {kind!r}")
    summary = one_line(summary)
    if not summary:
        raise LedgerError("summary is required")
    body = str(body or "").strip()
    digest = content_hash(summary, body)
    ts = now_iso(now)
    with write(conn):
        existing = conn.execute("SELECT id FROM mri WHERE hash = ? AND kind = ?",
                                (digest, kind)).fetchone()
        if existing:
            row_id, created = existing[0], False
        else:
            if alias and conn.execute("SELECT 1 FROM mri WHERE alias = ?", (alias,)).fetchone():
                raise LedgerError(f"alias {alias!r} already names a row")
            row_id, created = _next_id(conn, kind), True
            conn.execute("INSERT INTO mri(id, kind, alias, summary, body, source, session, hash, "
                         "created, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (row_id, kind, alias, summary, body, source, session, digest, ts, ts))
        for tag in tags:
            tag = _clean_tag(tag)
            if tag:
                conn.execute("INSERT OR IGNORE INTO tags(id, tag) VALUES (?, ?)", (row_id, tag))
        for target in about:
            if target and target != row_id:
                conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) VALUES (?, ?, 'about')",
                             (row_id, target))
    return row_id, created


def update_row(conn: sqlite3.Connection, row_id: str, *, summary: str | None = None,
               body: str | None = None, alias: str | None = None) -> Row:
    with write(conn):
        row = conn.execute("SELECT * FROM mri WHERE id = ? OR alias = ?",
                           (row_id, row_id)).fetchone()
        if row is None:
            raise LedgerError(f"no row {row_id!r}")
        s = one_line(summary) if summary is not None else row["summary"]
        b = str(body).strip() if body is not None else row["body"]
        conn.execute("UPDATE mri SET summary=?, body=?, hash=?, alias=COALESCE(?, alias), "
                     "updated=? WHERE id=?",
                     (s, b, content_hash(s, b), alias, now_iso(), row["id"]))
    return get_row(conn, row["id"])


def delete(conn: sqlite3.Connection, ids) -> int:
    """Delete rows or items by id, with their tags and links."""
    ids = [i for i in ids if i]
    if not ids:
        return 0
    n = 0
    with write(conn):
        for i in ids:
            n += conn.execute("DELETE FROM mri WHERE id = ?", (i,)).rowcount
            n += conn.execute("DELETE FROM items WHERE id = ?", (i,)).rowcount
            conn.execute("DELETE FROM tags WHERE id = ?", (i,))
            conn.execute("DELETE FROM links WHERE src = ? OR dst = ?", (i, i))
    return n


def tag(conn: sqlite3.Connection, target: str, tags) -> None:
    with write(conn):
        for t in tags:
            t = _clean_tag(t)
            if t:
                conn.execute("INSERT OR IGNORE INTO tags(id, tag) VALUES (?, ?)", (target, t))


def link(conn: sqlite3.Connection, src: str, dst: str, rel: str) -> None:
    if rel not in LINK_RELS:
        raise LedgerError(f"rel must be one of {LINK_RELS}")
    with write(conn):
        conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) VALUES (?, ?, ?)", (src, dst, rel))


def links_of(conn: sqlite3.Connection, node_id: str) -> list[tuple[str, str, str]]:
    return [tuple(r) for r in conn.execute(
        "SELECT src, dst, rel FROM links WHERE src = ? OR dst = ? ORDER BY rel, src, dst",
        (node_id, node_id))]


def related_issues(conn: sqlite3.Connection, item_id: str, *, k: int = 5,
                   vectors: dict[str, list[float]] | None = None,
                   query_vector: list[float] | None = None) -> list[tuple[float, Row]]:
    """The issues most relevant to ledger item *item_id*, best first.

    Score: a direct ``about`` link to the item, its ancestors or descendants
    (3), each tag shared with that subtree (1), plus the cosine between the
    issue and the item's title when *vectors* (row id -> vector) and
    *query_vector* are given."""
    family = conn.execute(
        "WITH RECURSIVE up(id, parent) AS (SELECT id, parent FROM items WHERE id = ? "
        "UNION ALL SELECT i.id, i.parent FROM items i JOIN up ON i.id = up.parent), "
        "down(id) AS (SELECT id FROM items WHERE id = ? "
        "UNION ALL SELECT i.id FROM items i JOIN down ON i.parent = down.id) "
        "SELECT id FROM up UNION SELECT id FROM down", (item_id, item_id)).fetchall()
    fam = [r[0] for r in family] or [item_id]
    marks = ",".join("?" * len(fam))
    scores: dict[str, float] = {}
    for (rid,) in conn.execute(
            f"SELECT DISTINCT l.src FROM links l JOIN mri m ON m.id = l.src "
            f"WHERE m.kind = 'I' AND l.rel = 'about' AND l.dst IN ({marks})", fam):
        scores[rid] = scores.get(rid, 0.0) + 3.0
    for rid, shared in conn.execute(
            f"SELECT t.id, COUNT(*) FROM tags t JOIN mri m ON m.id = t.id WHERE m.kind = 'I' "
            f"AND t.tag IN (SELECT tag FROM tags WHERE id IN ({marks})) GROUP BY t.id", fam):
        scores[rid] = scores.get(rid, 0.0) + float(shared)
    if vectors and query_vector is not None:
        from shapa import embed

        for rid, vec in vectors.items():
            if kind_of(rid) == "I":
                scores[rid] = scores.get(rid, 0.0) + max(0.0, embed.cosine(query_vector, vec))
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
    out = []
    for rid, score in ranked:
        row = get_row(conn, rid)
        if row is not None:
            out.append((score, row))
    return out


# --- the sweep (after every feature) ------------------------------------------------

@dataclass
class SweepReport:
    expired: list[str]
    deleted_items: list[str]
    duplicates: list[tuple[str, str]]  # (deleted, kept)
    superseded: list[str]
    stale_memories: list[str]
    conflicts: list[tuple[str, str]]
    dangling_links: int

    def lines(self) -> list[str]:
        out = []
        if self.expired:
            out.append(f"expired after {EXPIRY_DAYS} days: {', '.join(self.expired)}")
        if self.deleted_items:
            out.append(f"closed items removed: {len(self.deleted_items)}")
        if self.duplicates:
            out.append("duplicates removed: " + ", ".join(f"{a} (kept {b})" for a, b in self.duplicates))
        if self.superseded:
            out.append(f"superseded rows removed: {', '.join(self.superseded)}")
        if self.stale_memories:
            out.append(f"memories unused {STALE_MEMORY_DAYS}+ days removed: "
                       f"{', '.join(self.stale_memories)}")
        if self.conflicts:
            out.append("possible rule conflicts flagged: "
                       + ", ".join(f"{a}/{b}" for a, b in self.conflicts))
        return out


def _cosine(a, b) -> float:
    from shapa import embed

    return embed.cosine(a, b)


def _token_sim(a: str, b: str) -> float:
    ta, tb = set(re.findall(r"[a-z0-9]{3,}", a.lower())), set(re.findall(r"[a-z0-9]{3,}", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def sweep(conn: sqlite3.Connection, *, vectors: dict[str, list[float]] | None = None,
          uses: dict[str, tuple[int, str | None]] | None = None,
          now: datetime | None = None, keep_closed_since: str | None = None) -> SweepReport:
    """Clean the database after a feature merges.

    1. Live items created more than :data:`EXPIRY_DAYS` ago close as
       ``expired``.
    2. Closed items are deleted, except those closed at or after
       *keep_closed_since* (the merge that triggered this sweep keeps its
       rows until the next one).
    3. Rows another row supersedes are deleted.
    4. Duplicates (identical text, or cosine >= :data:`DUP_COSINE` - Jaccard
       >= 0.9 without *vectors*) within one kind: the newer row is deleted,
       its tags and links move to the kept one.
    5. Memories older than :data:`STALE_MEMORY_DAYS` with no recorded use
       (*uses*: id -> (count, last_used)) are deleted.
    6. Rule pairs with cosine in [CONFLICT_COSINE, DUP_COSINE) are linked
       ``conflicts`` for an agent or the operator to judge - never deleted.
    7. Links and tags whose endpoint no longer exists are removed.
    """
    now_dt = now or datetime.now(timezone.utc)
    cutoff = now_iso(now_dt - timedelta(days=EXPIRY_DAYS))
    report = SweepReport([], [], [], [], [], [], 0)

    live = conn.execute("SELECT id FROM items WHERE status != 'closed' AND created < ? "
                        "ORDER BY kind, CAST(SUBSTR(id, 2) AS INTEGER)", (cutoff,)).fetchall()
    for (iid,) in live:
        report.expired.extend(close(conn, iid, "expired", now=now_dt))

    with write(conn):
        sql = "SELECT id FROM items WHERE status = 'closed'"
        args: tuple = ()
        if keep_closed_since:
            sql += " AND (closed IS NULL OR closed < ?)"
            args = (keep_closed_since,)
        report.deleted_items = [r[0] for r in conn.execute(sql, args)]
    delete(conn, report.deleted_items)

    with write(conn):
        report.superseded = [r[0] for r in conn.execute(
            "SELECT DISTINCT l.dst FROM links l JOIN mri m ON m.id = l.dst WHERE l.rel = 'supersedes'")]
    delete(conn, report.superseded)

    all_rows = rows(conn)
    by_kind: dict[str, list[Row]] = {}
    for r in all_rows:
        by_kind.setdefault(r.kind, []).append(r)
    gone: set[str] = set()
    for kind, group in by_kind.items():
        group.sort(key=lambda r: (r.created, int(r.id[1:])))
        for i, older in enumerate(group):
            if older.id in gone:
                continue
            for newer in group[i + 1:]:
                if newer.id in gone:
                    continue
                if older.hash == newer.hash:
                    sim = 1.0
                elif vectors and older.id in vectors and newer.id in vectors:
                    sim = _cosine(vectors[older.id], vectors[newer.id])
                else:
                    sim = 0.0 if vectors else _token_sim(older.summary + " " + older.body,
                                                         newer.summary + " " + newer.body)
                    sim = 1.0 if sim >= 0.9 else 0.0
                if sim >= DUP_COSINE:
                    _merge_into(conn, newer.id, older.id)
                    gone.add(newer.id)
                    report.duplicates.append((newer.id, older.id))
                elif kind == "R" and vectors and sim >= CONFLICT_COSINE:
                    link(conn, older.id, newer.id, "conflicts")
                    report.conflicts.append((older.id, newer.id))
    delete(conn, gone)

    if uses is not None:
        stale_cut = now_dt - timedelta(days=STALE_MEMORY_DAYS)
        for r in by_kind.get("M", []):
            if r.id in gone:
                continue
            created = _parse_iso(r.created)
            count, last = uses.get(r.id, (0, None))
            last_dt = _parse_iso(last)
            if created and created < stale_cut and (last_dt is None or last_dt < stale_cut) and not count:
                report.stale_memories.append(r.id)
        delete(conn, report.stale_memories)

    with write(conn):
        cur = conn.execute(
            "DELETE FROM links WHERE src NOT IN (SELECT id FROM mri UNION SELECT id FROM items) "
            "OR dst NOT IN (SELECT id FROM mri UNION SELECT id FROM items)")
        report.dangling_links = cur.rowcount
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT id FROM mri UNION SELECT id FROM items)")
    return report


def _merge_into(conn, src: str, dst: str) -> None:
    """Move *src*'s tags and links onto *dst* (before *src* is deleted)."""
    with write(conn):
        conn.execute("INSERT OR IGNORE INTO tags(id, tag) SELECT ?, tag FROM tags WHERE id = ?",
                     (dst, src))
        conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) SELECT ?, dst, rel FROM links "
                     "WHERE src = ? AND dst != ?", (dst, src, dst))
        conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) SELECT src, ?, rel FROM links "
                     "WHERE dst = ? AND src != ?", (dst, src, dst))


def conflicts(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [tuple(r) for r in conn.execute(
        "SELECT src, dst FROM links WHERE rel = 'conflicts' ORDER BY src, dst")]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {k: 0 for k in ITEM_KINDS + MRI_KINDS}
    for kind, n in conn.execute("SELECT kind, COUNT(*) FROM items WHERE status != 'closed' "
                                "GROUP BY kind"):
        out[kind] = n
    for kind, n in conn.execute("SELECT kind, COUNT(*) FROM mri GROUP BY kind"):
        out[kind] = n
    return out


def scrub(conn: sqlite3.Connection, terms) -> int:
    """Replace each of *terms* (case-insensitive) in every text column with
    ``[workspace]`` - run before committing a database that will be
    published. Returns the number of rows changed."""
    changed = 0
    terms = [t for t in terms if t]
    if not terms:
        return 0
    pattern = re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.I)
    with write(conn):
        for table, cols in (("mri", ("summary", "body", "source", "alias")),
                            ("items", ("title", "body", "verify", "git_ref", "alias"))):
            for row in conn.execute(f"SELECT id, {', '.join(cols)} FROM {table}").fetchall():
                vals = [row[c] for c in cols]
                new = [pattern.sub("[workspace]", v) if isinstance(v, str) else v for v in vals]
                if new != vals:
                    conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)} "
                                 f"WHERE id = ?", (*new, row["id"]))
                    changed += 1
        for row_id, tag_text in conn.execute("SELECT id, tag FROM tags").fetchall():
            new_tag = pattern.sub("[workspace]", tag_text)
            if new_tag != tag_text:
                conn.execute("DELETE FROM tags WHERE id = ? AND tag = ?", (row_id, tag_text))
                conn.execute("INSERT OR IGNORE INTO tags(id, tag) VALUES (?, ?)", (row_id, new_tag))
                changed += 1
        for row in conn.execute("SELECT id, summary, body FROM mri").fetchall():
            conn.execute("UPDATE mri SET hash = ? WHERE id = ?",
                         (content_hash(row["summary"], row["body"]), row["id"]))
    return changed
