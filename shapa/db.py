"""The wiki database - format 4's one store for work and memory.

``<wiki>/shapa.db`` holds two ledgers, both tracked in git as the file itself:

- **items** - the work ledger. ``F`` features hold ``J`` jobs, which hold
  ``T`` tasks. A feature is a new capability, a branch and a PR, and closes
  on merge; a job (a fix, docs or release work stand alone) is one commit
  (message starts ``J<n>:``) and closes on that commit; a task
  has no git artifact and closes when an agent says so. IDs are a kind
  letter plus a per-wiki counter, never reused; the hierarchy lives in
  ``parent``.
- **mri** - memories (``M``: something the operator said that matters, or
  a strategy shift), rules (``R``) and issues (``I``: an agent mistake the
  operator corrected). Same ID scheme.
- **docs** (schema 2) - documents (``D``): a titled body kept whole and never
  swept for staleness. ``tombstones`` records every deletion so a hosted
  store can tell "deleted here" from "missing".

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
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shapa.redact import redact

DB_FILENAME = "shapa.db"
SCHEMA_VERSION = 2
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
DOC_KIND = "D"
KIND_NAMES = {"F": "feature", "J": "job", "T": "task",
              "M": "memory", "R": "rule", "I": "issue", "D": "document"}
#: A job's parent is a feature; a task's parent is a job. Both may stand alone.
PARENT_KIND = {"J": "F", "T": "J"}
CLOSE_REASONS = ("merge", "commit", "agent", "expired", "parent", "operator")
LINK_RELS = ("about", "supersedes", "conflicts", "related")
ID_RE = re.compile(r"^([FJTMRID])([1-9][0-9]*)$")
DEFAULT_MIME = "text/markdown"
TITLE_MAX = 200
DOC_BODY_MAX = 1_000_000  # the hosted contract's record limit
#: Documents that are not project documents: the focus record and skill
#: versions. The lean gate (one document per project) never counts them.
FOCUS_TAG = "focus"
SKILL_TAG = "skill"
RESERVED_DOC_TAGS = (FOCUS_TAG, SKILL_TAG)
PROJECT_PREFIX = "project:"
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,79}$")

#: Stages (the central brain contract, section 1): ``stage:N`` (one digit) on a rule, a memory
#: or a skill version. ``stage:0`` is the baseline every session start serves in full; only the
#: local CLI sets it. Anything else that asks for it is stored as ``stage-proposed:0``.
STAGE_PREFIX = "stage:"
PROPOSED_PREFIX = "stage-proposed:"
STAGE_PROPOSED = PROPOSED_PREFIX + "0"
STAGE_MAX = 9
MAX_TAGS = 100  # tags on one record, as the wire format (syncrec) allows
BASELINE_BUDGET = 6000  # characters of the global wiki's stage-0 set; the Worker holds the same constant
_STAGE_RE = re.compile(r"^stage:[0-9]$")
#: Tags that belong to the engine, never shown as topics. One list for every surface.
OWNER_TAGS = {"human_task": "human", "agent_task": "agent"}
IDEA_TAG = "idea"
DECISION_TAG = "decision"
#: Memories carrying one of these are kept by the sweep (they are written to be kept).
KEPT_MEMORY_TAGS = (IDEA_TAG, DECISION_TAG)
#: The learning loop's own tags (``shapa learn`` writes them; see ``learn.py``): the principle marker,
#: its counters, the repeat and learned states. They are system tags like the rest (never a topic,
#: look-alikes refused), and in addition ``check_row_tags`` keeps every agent entry point away from
#: them (``shapa row add|tag``, ``save``, the local MCP). A pulled record may carry them: a principle
#: written on one machine syncs to another.
LEARNING_TAGS = ("principle", "churn", "violation", "repeat", "repeat-checked", "blocked", "learned",
                 "meta-learning", "dismissed", "soft", "fire")
LEARNING_TAG_PREFIXES = ("violations:", "revisions:", "repeats:", "serves:", "repeat-of:", "last-seen:",
                         "evidence:", "learned-at:", "generalized-into:")
SYSTEM_TAGS = (SKILL_TAG, FOCUS_TAG, *OWNER_TAGS, IDEA_TAG, DECISION_TAG, *LEARNING_TAGS)
SYSTEM_TAG_PREFIXES = (STAGE_PREFIX, PROPOSED_PREFIX, "skill:", "v:", "status:", "rank:", PROJECT_PREFIX,
                       *LEARNING_TAG_PREFIXES)
#: Kept under its old names for the callers that import them.
RESERVED_ROW_TAGS = LEARNING_TAGS
RESERVED_ROW_TAG_PREFIXES = LEARNING_TAG_PREFIXES
#: Every system tag and tag family has look-alikes (full-width, zero-width, bidi marks, upper case)
#: that read as the real thing to a person; they are refused rather than stored as another tag.
_SPELLED = (*SYSTEM_TAGS, *SYSTEM_TAG_PREFIXES)
_INVISIBLE = ("Cf", "Cc", "Zl", "Zp")  # zero-width, bidi and other format, control, line separators
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,60}/[a-z0-9][a-z0-9.+-]{0,60}$")

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

#: Schema 2, additive: applied lazily on connect to a schema-1 file. An older
#: shapa never reads these tables, so it keeps working on a schema-2 file.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY,
    alias TEXT UNIQUE,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT 'text/markdown',
    source TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    uid TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS uids (
    id TEXT PRIMARY KEY,
    uid TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS uids_uid ON uids(uid);
CREATE TABLE IF NOT EXISTS metrics_daily (
    day TEXT NOT NULL,
    repo TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (day, repo)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS mri_kind ON mri(kind);
CREATE INDEX IF NOT EXISTS items_kind_status_created ON items(kind, status, created);
CREATE INDEX IF NOT EXISTS items_alias ON items(alias);
CREATE INDEX IF NOT EXISTS items_title ON items(title);
CREATE TABLE IF NOT EXISTS tag_counts (
    tag TEXT NOT NULL, k TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (tag, k)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tag_counts_n ON tag_counts(n);
"""
#: ``metrics_daily`` (``shapa metrics --snapshot``): one small JSON row per day and repo, kept
#: when the raw events behind it are dropped; the global wiki's also holds the ``*`` roll-up.
#: Local to the committed file: the hosted sync never reads or writes it.
#:
#: ``tag_counts`` (additive, kept by the triggers below on every write to ``tags``, so no write
#: path can forget it) is the tag index: tag -> records per kind letter. It makes the topics list
#: of a session start and ``shapa tags`` cost the number of distinct tags, not of records. The
#: other indexes are covering ones for counts and for lookups by alias and title. A schema-1
#: reader ignores all of it; a row-less file stays valid.
_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS tag_counts_ai AFTER INSERT ON tags BEGIN "
    "INSERT INTO tag_counts(tag, k, n) VALUES (NEW.tag, substr(NEW.id, 1, 1), 1) "
    "ON CONFLICT(tag, k) DO UPDATE SET n = n + 1; END",
    "CREATE TRIGGER IF NOT EXISTS tag_counts_ad AFTER DELETE ON tags BEGIN "
    "UPDATE tag_counts SET n = n - 1 WHERE tag = OLD.tag AND k = substr(OLD.id, 1, 1); "
    "DELETE FROM tag_counts WHERE tag = OLD.tag AND k = substr(OLD.id, 1, 1) AND n <= 0; END",
    "CREATE TRIGGER IF NOT EXISTS tag_counts_au AFTER UPDATE ON tags BEGIN "
    "UPDATE tag_counts SET n = n - 1 WHERE tag = OLD.tag AND k = substr(OLD.id, 1, 1); "
    "DELETE FROM tag_counts WHERE tag = OLD.tag AND k = substr(OLD.id, 1, 1) AND n <= 0; "
    "INSERT INTO tag_counts(tag, k, n) VALUES (NEW.tag, substr(NEW.id, 1, 1), 1) "
    "ON CONFLICT(tag, k) DO UPDATE SET n = n + 1; END",
)
#: A record's identity is its uid, kept in the ``uids`` side table (id -> uid)
#: and not as a column of ``items``/``mri``: an older shapa builds its
#: dataclasses from ``SELECT *`` and would fail on an extra column.


class LedgerError(ValueError):
    """A ledger operation the data does not allow (unknown id, wrong kind)."""


class DocExists(LedgerError):
    """The project already has its one document (the lean gate); ``existing``
    is that document's id."""

    def __init__(self, existing: str, title: str, project: str):
        self.existing = existing
        what = f"project {project!r}" if project else "this wiki's project"
        super().__init__(f"{what} already has document {existing} ({title}): edit it "
                         f"(`shapa doc edit {existing}`) or add with --merge to append a section; "
                         "a project keeps one document")


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


def connect(root, *, create: bool = False, busy_ms: int | None = None) -> sqlite3.Connection | None:
    """Open *root*'s database (``None`` when it has none and *create* is
    false). Foreign keys on, rollback journal, a generous busy timeout so
    hooks from parallel sessions queue instead of failing; *busy_ms* shortens
    it for a caller that would rather skip than wait (a session start, the
    prompt hook)."""
    path = db_path(root)
    if not path.is_file():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
    wait = BUSY_TIMEOUT_MS if busy_ms is None else max(0, int(busy_ms))
    conn = sqlite3.connect(str(path), timeout=wait / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {wait}")
    conn.execute("PRAGMA foreign_keys = ON")
    # The file is committed to git whole: zero deleted and overwritten content
    # so a pruned or scrubbed row's old text never lingers in a free page.
    conn.execute("PRAGMA secure_delete = ON")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "delete":
        conn.execute("PRAGMA journal_mode = DELETE")
    if create or not _has_schema(conn):
        with write(conn):
            _apply_schema(conn)
    elif not _is_current(conn):
        try:
            with write(conn):
                _apply_schema(conn)
        except sqlite3.OperationalError:
            pass  # a read-only file stays schema 1; a write there fails where it happens
    return conn


def _is_current(conn: sqlite3.Connection) -> bool:
    return (_has_table(conn, "docs") and _has_table(conn, "tombstones") and _has_table(conn, "uids")
            and _has_table(conn, "metrics_daily")
            and _has_table(conn, "tag_counts")
            and conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN "
                             "('items_title', 'items_kind_status_created', 'tag_counts_n')").fetchone()[0] == 3
            and any(r[1] == "uid" for r in conn.execute("PRAGMA table_info(tombstones)")))


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table (idempotent), heal a schema-2 file written before
    uids existed, and stamp the schema version, inside the caller's write
    transaction."""
    for stmt in (_SCHEMA + _SCHEMA_V2).split(";"):
        if stmt.strip():
            conn.execute(stmt)
    for stmt in _TRIGGERS:
        conn.execute(stmt)
    if (not conn.execute("SELECT 1 FROM tag_counts LIMIT 1").fetchone()
            and conn.execute("SELECT 1 FROM tags LIMIT 1").fetchone()):
        rebuild_tag_counts(conn)  # a file written before the tag index existed
    if not any(r[1] == "uid" for r in conn.execute("PRAGMA table_info(tombstones)")):
        conn.execute("ALTER TABLE tombstones ADD COLUMN uid TEXT NOT NULL DEFAULT ''")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)",
                 (str(SCHEMA_VERSION),))
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema' AND CAST(value AS INTEGER) < ?",
                 (str(SCHEMA_VERSION), SCHEMA_VERSION))
    for kind in ITEM_KINDS + MRI_KINDS + (DOC_KIND,):
        conn.execute("INSERT OR IGNORE INTO counters(kind, next) VALUES (?, 1)", (kind,))
    _backfill_uids(conn)


# --- uids: record identity ------------------------------------------------------------------

def new_uid() -> str:
    return secrets.token_hex(8)


def legacy_uid(record_id: str, created: str) -> str:
    """The uid of a record that predates uids: derived from its label and
    creation time, so every copy of the same database derives the same one."""
    return hashlib.sha256(f"{record_id}\n{created}".encode("utf-8")).hexdigest()[:16]


def uid_of(conn: sqlite3.Connection, record_id: str) -> str | None:
    """The stored uid of *record_id*, ``None`` when it has none yet."""
    if not _has_table(conn, "uids"):
        return None
    r = conn.execute("SELECT uid FROM uids WHERE id = ?", (record_id,)).fetchone()
    return r[0] or None if r else None


def _backfill_uids(conn: sqlite3.Connection) -> int:
    """Give every record without a uid its legacy uid, and every tombstone
    without one the uid its record would have had. Inside a write transaction."""
    n = 0
    for table in ("items", "mri", "docs"):
        if not _has_table(conn, table):
            continue
        for rid, created in conn.execute(
                f"SELECT t.id, t.created FROM {table} t LEFT JOIN uids u ON u.id = t.id "
                "WHERE u.id IS NULL OR u.uid = ''").fetchall():
            conn.execute("INSERT OR REPLACE INTO uids(id, uid) VALUES (?, ?)",
                         (rid, legacy_uid(rid, created)))
            n += 1
    for rid, created in conn.execute("SELECT id, created FROM tombstones WHERE uid = ''").fetchall():
        conn.execute("UPDATE tombstones SET uid = ? WHERE id = ?", (legacy_uid(rid, created), rid))
        n += 1
    return n


def backfill_uids(conn: sqlite3.Connection) -> int:
    """:func:`_backfill_uids` in its own transaction, writing nothing when
    nothing is missing (a rows-only database stays byte-identical)."""
    missing = any(conn.execute(
        f"SELECT 1 FROM {t} x LEFT JOIN uids u ON u.id = x.id WHERE u.id IS NULL OR u.uid = '' LIMIT 1"
    ).fetchone() for t in ("items", "mri", "docs")) or conn.execute(
        "SELECT 1 FROM tombstones WHERE uid = '' LIMIT 1").fetchone()
    if not missing:
        return 0
    with write(conn):
        return _backfill_uids(conn)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def read_rows(root) -> list["Row"]:
    """Every M/R/I row and every document (as a ``D`` row: its title is the
    summary) of *root*'s database, opened read-only (never creates or
    migrates the file; ``[]`` when there is none or it is unreadable)."""
    path = db_path(root)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        try:
            if not _has_schema(conn):
                return []
            return rows(conn) + doc_rows(conn)
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
             git_ref: str | None = None, now: datetime | None = None, tags=()) -> str:
    kind = kind.upper()
    if kind not in ITEM_KINDS:
        raise LedgerError(f"item kind must be one of {ITEM_KINDS}, got {kind!r}")
    # The file is committed whole: no secret may enter it (redact.py).
    title = one_line(redact(title), 200)
    if not title:
        raise LedgerError("title is required")
    body, verify = redact(body or ""), redact(verify) if verify else verify
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
        conn.execute("INSERT OR REPLACE INTO uids(id, uid) VALUES (?, ?)", (new_id, new_uid()))
        if tags:
            insert, drop = _guard_tags(conn, new_id, tags, letter=kind, privileged=False)
            _write_tags(conn, new_id, insert, drop)
            sync_project_link(conn, new_id)
    return new_id


def update_item(conn: sqlite3.Connection, item_id: str, **fields) -> Item:
    allowed = {"title", "body", "verify", "git_ref", "alias"}
    bad = set(fields) - allowed
    if bad:
        raise LedgerError(f"cannot set {sorted(bad)}")
    fields = {k: redact(v) if isinstance(v, str) and k in ("title", "body", "verify") else v
              for k, v in fields.items()}
    with write(conn):
        item = _require_item(conn, item_id)
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE items SET {sets}, updated = ? WHERE id = ?",
                         (*fields.values(), now_iso(), item.id))
    return _require_item(conn, item.id)


def set_parent(conn: sqlite3.Connection, item_id: str, parent: str | None) -> Item:
    """Move an open job or task under another open *parent* (a feature for
    a job, a job for a task), or make it standalone with ``None``. Work
    filed under the wrong feature moves out before that feature's merge
    closes it along with everything still open beneath it."""
    with write(conn):
        item = _require_item(conn, item_id)
        if item.status == "closed":
            raise LedgerError(f"{item.id} is closed")
        if item.kind not in PARENT_KIND:
            raise LedgerError(f"a {KIND_NAMES[item.kind]} has no parent")
        if parent:
            p = _require_item(conn, parent)
            if PARENT_KIND[item.kind] != p.kind:
                raise LedgerError(f"a {KIND_NAMES[item.kind]}'s parent must be a "
                                  f"{KIND_NAMES[PARENT_KIND[item.kind]]}, not {p.id}")
            if p.status == "closed":
                raise LedgerError(f"{p.id} is closed")
            parent = p.id
        conn.execute("UPDATE items SET parent = ?, updated = ? WHERE id = ?",
                     (parent, now_iso(), item.id))
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
            if any(t in OWNER_TAGS for t in _tags_of(conn, child)):
                # A closing parent (merged, expired, closed by hand) never takes the operator's
                # task list with it: the task stays open and stands alone from here on.
                conn.execute("UPDATE items SET parent=NULL, updated=? WHERE id=? AND status != 'closed'",
                             (ts, child))
                continue
            cur = conn.execute("UPDATE items SET status='closed', closed_reason='parent', "
                               "closed=?, updated=? WHERE id=? AND status != 'closed'",
                               (ts, ts, child))
            if cur.rowcount:
                closed.append(child)
    if item.kind == "F":
        _after_feature_closed(conn, item.id)
    return closed


def _after_feature_closed(conn: sqlite3.Connection, feature_id: str) -> None:
    """A closed feature frees its fire slot and the next candidate takes the
    place. Never lets the focus record break a close."""
    try:
        from shapa import focus

        focus.on_feature_closed(conn, feature_id)
    except Exception:
        pass


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


# --- tag rules: the one place every tag write passes ---------------------------------------------

def _plain(tag: str) -> str:
    """*tag* as a person reads it: compatibility-folded, lower case, no invisible characters."""
    folded = unicodedata.normalize("NFKC", str(tag)).casefold()
    return "".join(c for c in folded if unicodedata.category(c) not in _INVISIBLE)


def check_tag_spelling(tags) -> None:
    """Refuse a look-alike of a stage or owner tag (``Stage:0``, full-width ``stage:0``, a
    zero-width space inside ``human_task``): it would read as the real tag and be none."""
    for t in tags:
        p = _plain(t)
        if p != t and any(_imitates(t, p, s) for s in _SPELLED):
            raise LedgerError(f"invalid tag {t!r}: it imitates a system tag; spell it exactly")


def _imitates(tag: str, plain: str, spelled: str) -> bool:
    if not spelled.endswith(":"):
        return plain == spelled
    if spelled in LEARNING_TAG_PREFIXES:
        # the learning loop's families carry mixed-case values (``last-seen:2026-11-13T10:00:00Z``,
        # ``repeat-of:R12``): only the family name is checked
        return plain.startswith(spelled) and not tag.startswith(spelled)
    return plain.startswith(spelled)


def is_global(conn: sqlite3.Connection) -> bool:
    """Is *conn* the global wiki's database? (The baseline, ``stage:0``, belongs to it alone.)"""
    try:
        from shapa import config

        found = conn.execute("PRAGMA database_list").fetchone()
        path = found[2] if found else ""
        return bool(path) and Path(path).resolve() == db_path(config.global_root()).resolve()
    except (OSError, sqlite3.Error):
        return False


def legacy_tags(tags) -> list[str]:
    """*tags* from an older note or log, minus any that now mean something to the engine (a stage,
    a look-alike, a second or malformed project): a migration never fails on, or gains, such a tag.
    One well-formed ``project:KEY`` tag is kept."""
    out = []
    project = ""
    for t in tags:
        t = " ".join(str(t).split())
        if not t or t.startswith((STAGE_PREFIX, PROPOSED_PREFIX)):
            continue
        if t.startswith(PROJECT_PREFIX):
            try:
                check_project_tags([t])
            except LedgerError:
                continue
            if project and project != t:
                continue
            project = t
        try:
            check_tag_spelling([t])
        except LedgerError:
            continue
        if t not in out:
            out.append(t)
    return out


def stage_of(tags) -> int | None:
    """The stage a tag list carries (``stage:N``, canonical spelling only), else ``None``."""
    for t in sorted(tags):
        if _STAGE_RE.fullmatch(t):
            return int(t[len(STAGE_PREFIX):])
    return None


def takes_stage(letter: str, tags=()) -> bool:
    """Stages apply to rules, memories and skill versions."""
    return letter in ("M", "R") or (letter == DOC_KIND and SKILL_TAG in tags)


def check_stage_tags(tags, letter: str) -> None:
    """The stage tags of one record are well formed: at most one ``stage:N`` (N one ASCII digit),
    ``stage-proposed:0`` only as that spelling and never beside a stage, and only on a record that
    takes a stage. Raises :class:`LedgerError`."""
    tags = list(tags)
    staged = [t for t in tags if t.startswith(STAGE_PREFIX)]
    proposed = [t for t in tags if t.startswith(PROPOSED_PREFIX)]
    bad = [t for t in staged if not _STAGE_RE.fullmatch(t)] + [t for t in proposed if t != STAGE_PROPOSED]
    if bad:
        raise LedgerError(f"invalid stage tag {bad[0]!r}: stage:N with N a digit 0-9")
    if len(set(staged)) > 1:
        raise LedgerError("a record has at most one stage (`shapa stage set ID N` changes it)")
    if staged and proposed:
        raise LedgerError("a record cannot carry both a stage and a proposed stage")
    if (staged or proposed) and not takes_stage(letter, tags):
        raise LedgerError("a stage applies to rules, memories and skill versions only")


def _stage_budget_check(conn: sqlite3.Connection, record_id: str | None, size: int | None) -> None:
    from shapa import stages

    stages.check_budget(conn, record_id, size)


def _guard_tags(conn: sqlite3.Connection, target: str | None, new, *, letter: str, privileged: bool,
                size: int | None = None, have=None) -> tuple[list[str], list[str]]:
    """Validate *new* tags for record *target* (``None`` when it is not stored yet) and return
    ``(tags to insert, tags to delete)``. Raises :class:`LedgerError`; call inside a write.

    The stage rules (contract section 1): ``stage:0`` is the operator's, so from a writer that
    is not *privileged* (the local CLI is; an agent tool, a hook, a sync is not) it is stored as
    ``stage-proposed:0`` instead, and no tag changes at all on a record that already holds it."""
    raw = [" ".join(str(x).split()) for x in new if str(x).strip()]
    check_project_tags(raw)                                   # on the tag as sent, before the cut
    check_tag_spelling(raw)
    clean = list(dict.fromkeys(t for t in (_clean_tag(x) for x in raw) if t))
    if len({t for t in clean if t.startswith(PROJECT_PREFIX)}) > 1:
        raise LedgerError("a record belongs to one project")
    have = set(have) if have is not None else (set(_tags_of(conn, target)) if target else set())
    drop = []
    mine = [t for t in clean if t.startswith(PROJECT_PREFIX)]
    if mine:
        drop = [t for t in have if t.startswith(PROJECT_PREFIX) and t != mine[0]]
    if any(t.startswith((STAGE_PREFIX, PROPOSED_PREFIX)) for t in clean):
        check_stage_tags(have | set(clean), letter)
    insert = [t for t in clean if t not in have]
    if len((have - set(drop)) | set(insert)) > MAX_TAGS:
        raise LedgerError(f"a record carries at most {MAX_TAGS} tags")
    if any(t in (f"{STAGE_PREFIX}0", STAGE_PROPOSED) for t in insert) and not is_global(conn):
        raise LedgerError("stage 0 is the baseline of the global wiki: a repo wiki takes stages 1-9 "
                          "(put the record in the global wiki, or give it another stage)")
    if not privileged:
        if f"{STAGE_PREFIX}0" in have and (insert or drop):
            raise LedgerError(f"{target} is baseline (stage 0): its text and tags change only from "
                              "the local CLI")
        if f"{STAGE_PREFIX}0" in insert:
            insert = [STAGE_PROPOSED if t == f"{STAGE_PREFIX}0" else t for t in insert]
            if STAGE_PROPOSED in have:
                insert.remove(STAGE_PROPOSED)
    elif f"{STAGE_PREFIX}0" in insert:
        _stage_budget_check(conn, target, size)
    return insert, drop


def _write_tags(conn: sqlite3.Connection, target: str, insert, drop) -> None:
    for t in drop:
        conn.execute("DELETE FROM tags WHERE id = ? AND tag = ?", (target, t))
    for t in insert:
        conn.execute("INSERT OR IGNORE INTO tags(id, tag) VALUES (?, ?)", (target, t))


def project_doc_id(conn: sqlite3.Connection, key: str) -> str | None:
    """The id of the live project document of *key* (a non-empty key), found by its tag."""
    for (i,) in conn.execute("SELECT id FROM tags WHERE tag = ? AND id >= 'D' AND id < 'E' "
                             "ORDER BY id", (PROJECT_PREFIX + key,)).fetchall():
        if project_of(_tags_of(conn, i)) is not None:
            return i
    return None


def _project_key(tags) -> str:
    return next((t[len(PROJECT_PREFIX):] for t in sorted(tags) if t.startswith(PROJECT_PREFIX)), "")


def sync_project_link(conn: sqlite3.Connection, record_id: str) -> None:
    """Keep the ``about`` link of a record with a ``project:KEY`` tag on that project's document
    (when the wiki has one); a link to the document of a project it left is removed. Tag and link
    are both the relation. Inside the caller's write."""
    if not _is_project_member(conn, record_id):
        return  # the project's own document is the target; a feature or a job never links
    tags = _tags_of(conn, record_id)
    key = _project_key(tags)
    if not key:
        return
    for (dst,) in conn.execute("SELECT dst FROM links WHERE src = ? AND rel = 'about' "
                               "AND substr(dst, 1, 1) = 'D'", (record_id,)).fetchall():
        dtags = _tags_of(conn, dst)
        if project_of(dtags) is not None and _project_key(dtags) not in ("", key):
            conn.execute("DELETE FROM links WHERE src = ? AND dst = ? AND rel = 'about'", (record_id, dst))
    doc = project_doc_id(conn, key)
    if doc and doc != record_id:
        conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) VALUES (?, ?, 'about')", (record_id, doc))


def _unlink_left_projects(conn: sqlite3.Connection, record_id: str, dropped) -> None:
    """A ``project:KEY`` tag left the record: its ``about`` link to that project's document goes."""
    for t in dropped:
        if t.startswith(PROJECT_PREFIX):
            doc = project_doc_id(conn, t[len(PROJECT_PREFIX):])
            if doc:
                conn.execute("DELETE FROM links WHERE src = ? AND dst = ? AND rel = 'about'",
                             (record_id, doc))


#: What an ``about`` link to a project's document is kept for (contract 6d): tasks, memories,
#: rules, issues and skill versions. Never the document itself, a feature or a job.
_PROJECT_MEMBER_KINDS = ("T", "M", "R", "I")


def _is_project_member(conn: sqlite3.Connection, record_id: str) -> bool:
    letter = record_id[:1]
    if letter in _PROJECT_MEMBER_KINDS:
        return True
    return letter == DOC_KIND and SKILL_TAG in _tags_of(conn, record_id)


def _link_project_members(conn: sqlite3.Connection, doc_id: str, key: str) -> None:
    """A project document was just created: every record already tagged ``project:KEY`` gets its
    ``about`` link to it."""
    for (rid,) in conn.execute("SELECT id FROM tags WHERE tag = ?", (PROJECT_PREFIX + key,)).fetchall():
        if rid != doc_id and _is_project_member(conn, rid):
            conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) VALUES (?, ?, 'about')",
                         (rid, doc_id))


def rebuild_tag_counts(conn: sqlite3.Connection) -> None:
    """Recompute ``tag_counts`` from ``tags`` (a file written before the index existed). Inside a write."""
    conn.execute("DELETE FROM tag_counts")
    conn.execute("INSERT INTO tag_counts(tag, k, n) SELECT tag, substr(id, 1, 1), count(*) "
                 "FROM tags GROUP BY tag, substr(id, 1, 1)")


def _identical_text_tags(conn: sqlite3.Connection, record_id: str, tags, privileged: bool) -> list:
    """The tags a write of text the record *record_id* already holds may add (contract 6d): from
    anything but the local CLI it never restages the record or moves it to another project, and
    leaves a baseline record exactly as it is."""
    tags = list(tags)
    if privileged:
        return tags
    have = _tags_of(conn, record_id)
    if f"{STAGE_PREFIX}0" in have:
        return []
    keep_project = any(t.startswith(PROJECT_PREFIX) for t in have)
    out = []
    for t in tags:
        s = " ".join(str(t).split())
        if s.startswith((STAGE_PREFIX, PROPOSED_PREFIX)) or (keep_project and s.startswith(PROJECT_PREFIX)):
            continue
        out.append(t)
    return out


def add_row(conn: sqlite3.Connection, kind: str, summary: str, body: str = "", *,
            tags=(), about=(), alias: str | None = None, source: str = "",
            session: str = "", now: datetime | None = None,
            privileged: bool = False) -> tuple[str, bool]:
    """Write one M/R/I row. Returns ``(id, created)``: identical text already
    stored returns that row's id with ``created`` false (its tags and
    *about* links are still added). *privileged* is the local CLI: only it may
    store ``stage:0`` (anything else gets ``stage-proposed:0``)."""
    kind = kind.upper()
    if kind not in MRI_KINDS:
        raise LedgerError(f"row kind must be one of {MRI_KINDS}, got {kind!r}")
    # The file is committed whole: no secret may enter it, whoever writes the
    # row (capture, correction, save, migration).
    summary = one_line(redact(summary))
    if not summary:
        raise LedgerError("summary is required")
    body = redact(str(body or "").strip())
    digest = content_hash(summary, body)
    ts = now_iso(now)
    with write(conn):
        existing = conn.execute("SELECT id FROM mri WHERE hash = ? AND kind = ?",
                                (digest, kind)).fetchone()
        if existing:
            row_id, created = existing[0], False
            insert, drop = _guard_tags(conn, row_id, _identical_text_tags(conn, row_id, tags, privileged),
                                       letter=kind, privileged=privileged, size=len(summary) + len(body))
        else:
            if alias and conn.execute("SELECT 1 FROM mri WHERE alias = ?", (alias,)).fetchone():
                raise LedgerError(f"alias {alias!r} already names a row")
            insert, drop = _guard_tags(conn, None, tags, letter=kind, privileged=privileged,
                                       size=len(summary) + len(body))
            row_id, created = _next_id(conn, kind), True
            conn.execute("INSERT INTO mri(id, kind, alias, summary, body, source, session, hash, "
                         "created, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (row_id, kind, alias, summary, body, source, session, digest, ts, ts))
            conn.execute("INSERT OR REPLACE INTO uids(id, uid) VALUES (?, ?)", (row_id, new_uid()))
        _write_tags(conn, row_id, insert, drop)
        if insert or drop:
            sync_project_link(conn, row_id)
        frozen = not created and not privileged and f"{STAGE_PREFIX}0" in _tags_of(conn, row_id)
        for target in (() if frozen else about):
            if target and target != row_id:
                conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) VALUES (?, ?, 'about')",
                             (row_id, target))
    _after_row_named_skill(conn, row_id)
    return row_id, created


def _after_row_named_skill(conn: sqlite3.Connection, row_id: str) -> None:
    """A memory, rule or issue that names a skill (a ``skill:NAME`` tag, or an
    ``about`` link to a skill version) files a pending new version of it.
    Never activates, and never lets the skill record break a row write."""
    try:
        from shapa import skills

        skills.lesson_from_row(conn, row_id)
    except Exception:
        pass


def update_row(conn: sqlite3.Connection, row_id: str, *, summary: str | None = None,
               body: str | None = None, alias: str | None = None, privileged: bool = False) -> Row:
    with write(conn):
        row = conn.execute("SELECT * FROM mri WHERE id = ? OR alias = ?",
                           (row_id, row_id)).fetchone()
        if row is None:
            raise LedgerError(f"no row {row_id!r}")
        s = one_line(redact(summary)) if summary is not None else row["summary"]
        b = redact(str(body).strip()) if body is not None else row["body"]
        if f"{STAGE_PREFIX}0" in _tags_of(conn, row["id"]) and (
                s != row["summary"] or b != row["body"] or alias):
            if not privileged:
                raise LedgerError(f"{row['id']} is baseline (stage 0): its text changes only from the "
                                  "local CLI")
            _stage_budget_check(conn, row["id"], len(s) + len(b))
        conn.execute("UPDATE mri SET summary=?, body=?, hash=?, alias=COALESCE(?, alias), "
                     "updated=? WHERE id=?",
                     (s, b, content_hash(s, b), alias, now_iso(), row["id"]))
    return get_row(conn, row["id"])


# --- documents -----------------------------------------------------------------------

@dataclass
class Doc:
    id: str
    alias: str | None
    title: str
    body: str
    mime: str
    source: str
    hash: str
    created: str
    updated: str
    tags: list[str]


def _doc(conn, r) -> Doc:
    return Doc(**{k: r[k] for k in r.keys()}, tags=_tags_of(conn, r["id"]))


def get_doc(conn: sqlite3.Connection, doc_id: str) -> Doc | None:
    if not _has_table(conn, "docs"):
        return None
    r = conn.execute("SELECT * FROM docs WHERE id = ? OR alias = ?", (doc_id, doc_id)).fetchone()
    return _doc(conn, r) if r else None


def documents(conn: sqlite3.Connection) -> list[Doc]:
    if not _has_table(conn, "docs"):
        return []
    tag_map: dict[str, list[str]] = {}
    # only the documents' tags (ids D<n>): an index range scan, not a read of every row's tags
    for rid, tag_text in conn.execute("SELECT id, tag FROM tags WHERE id >= 'D' AND id < 'E' ORDER BY tag"):
        tag_map.setdefault(rid, []).append(tag_text)
    return [Doc(**{k: r[k] for k in r.keys()}, tags=tag_map.get(r["id"], []))
            for r in conn.execute("SELECT * FROM docs ORDER BY CAST(SUBSTR(id, 2) AS INTEGER)")]


def doc_rows(conn: sqlite3.Connection) -> list["Row"]:
    """Documents shaped as :class:`Row` (kind ``D``, the title as the summary)
    for the derived recall index; never part of :func:`rows`, so the sweep's
    duplicate and staleness rules cannot reach them."""
    return [Row(id=d.id, kind=DOC_KIND, alias=d.alias, summary=one_line(d.title), body=d.body,
                source=d.source, session="", hash=d.hash, created=d.created, updated=d.updated,
                tags=d.tags) for d in documents(conn) if project_of(d.tags) is not None]


def project_of(tags) -> str | None:
    """The project key a document belongs to: the ``project:KEY`` tag, ``""``
    (the wiki's own project) without one, ``None`` for a focus record or a
    skill version, which are not project documents and never count."""
    tags = list(tags)
    if any(t in RESERVED_DOC_TAGS for t in tags):
        return None
    for t in sorted(tags):
        if t.startswith(PROJECT_PREFIX):
            return t[len(PROJECT_PREFIX):]
    return ""


def clean_project(project: str | None) -> str:
    """A project (or subproject) key: lowercase ``a-z 0-9 . _ / -``, up to 80."""
    key = (project or "").strip().lower()
    if key and not _PROJECT_RE.match(key):
        raise LedgerError(f"invalid project {project!r}: lowercase letters, digits and . _ / - only")
    return key


def project_doc(conn: sqlite3.Connection, key: str, *, exclude: str = "") -> Doc | None:
    """The live project document of *key* (``""`` = the wiki's own), if any."""
    if key:  # found by its tag: the cost does not grow with the number of documents
        for (i,) in conn.execute("SELECT id FROM tags WHERE tag = ? AND id >= 'D' AND id < 'E' "
                                 "ORDER BY id", (PROJECT_PREFIX + key,)).fetchall():
            if i != exclude and project_of(_tags_of(conn, i)) == key:
                return get_doc(conn, i)
        return None
    for d in documents(conn):
        if d.id != exclude and project_of(d.tags) == key:
            return d
    return None


def project_overages(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Projects that hold more than their one document (stored by an older client, or a
    second device that raced): ``{key: [ids]}``. Such documents are grandfathered, never
    refused; ``shapa doc merge`` folds them."""
    held: dict[str, list[str]] = {}
    for d in documents(conn):
        key = project_of(d.tags)
        if key is not None:
            held.setdefault(key, []).append(d.id)
    return {k: v for k, v in held.items() if len(v) > 1}


def check_project_tags(tags) -> None:
    """Refuse a ``project:`` tag whose key is not exactly what :func:`clean_project`
    makes of it (uppercase, zero-width or full-width look-alikes, empty, too long)."""
    for t in tags:
        if t.startswith(PROJECT_PREFIX):
            key = t[len(PROJECT_PREFIX):]
            if not key or clean_project(key) != key:
                raise LedgerError(f"invalid project tag {t!r}: lowercase letters, digits and . _ / - only")


def _clean_mime(mime: str | None) -> str:
    mime = (mime or DEFAULT_MIME).strip().lower()
    if not _MIME_RE.match(mime):
        raise LedgerError(f"invalid mime type {mime!r}")
    return mime


def add_doc(conn: sqlite3.Connection, title: str, body: str = "", *, tags=(), alias: str | None = None,
            mime: str | None = None, source: str = "", now: datetime | None = None,
            project: str | None = None, merge: bool = False, system: bool = False,
            uid: str | None = None, privileged: bool = False) -> str:
    """Write one document (kind ``D``); returns its id.

    The lean gate: a project (``project`` or a ``project:KEY`` tag; none means
    the wiki's own project) keeps one document. A second one is refused with
    :class:`DocExists` naming the first, or with ``merge`` appended to it as a
    section (its id is returned). Focus records and skill versions are not
    project documents and are never gated."""
    title = one_line(redact(title), TITLE_MAX)
    if not title:
        raise LedgerError("title is required")
    body = redact(str(body or "").strip())
    if len(body.encode("utf-8")) > DOC_BODY_MAX:
        raise LedgerError(f"a document is at most {DOC_BODY_MAX} bytes")
    mime = _clean_mime(mime)
    check_project_tags([" ".join(str(x).split()) for x in tags])  # before any truncation
    tags = [t for t in (_clean_tag(x) for x in tags) if t]
    if not system and any(t in RESERVED_DOC_TAGS for t in tags):
        raise LedgerError(f"the tags {', '.join(RESERVED_DOC_TAGS)} are reserved: "
                          "use `shapa focus` and `shapa skill`")
    key = clean_project(project)
    if key:
        tags = [t for t in tags if not t.startswith(PROJECT_PREFIX)] + [PROJECT_PREFIX + key]
    check_project_tags(tags)
    ts = now_iso(now)
    with write(conn):
        gate = project_of(tags)
        if gate is not None:
            held = project_doc(conn, gate)
            if held is not None:
                if not merge:
                    raise DocExists(held.id, held.title, gate)
                section = f"{held.body}\n\n## {title}\n\n{body}".strip()
                if len(section.encode("utf-8")) > DOC_BODY_MAX:
                    raise LedgerError(f"merging would make {held.id} larger than {DOC_BODY_MAX} bytes")
                conn.execute("UPDATE docs SET body = ?, hash = ?, updated = ? WHERE id = ?",
                             (section, content_hash(held.title, section), ts, held.id))
                return held.id
        if alias and conn.execute("SELECT 1 FROM docs WHERE alias = ? OR id = ?",
                                  (alias, alias)).fetchone():
            raise LedgerError(f"alias {alias!r} already names a document")
        doc_id = _next_id(conn, DOC_KIND)
        conn.execute("INSERT INTO docs(id, alias, title, body, mime, source, hash, created, "
                     "updated) VALUES (?,?,?,?,?,?,?,?,?)",
                     (doc_id, alias or None, title, body, mime, source,
                      content_hash(title, body), ts, ts))
        conn.execute("INSERT OR REPLACE INTO uids(id, uid) VALUES (?, ?)", (doc_id, uid or new_uid()))
        insert, drop = _guard_tags(conn, doc_id, tags, letter=DOC_KIND, privileged=privileged)
        _write_tags(conn, doc_id, insert, drop)
        if gate:
            _link_project_members(conn, doc_id, gate)
        elif project_of(tags) is None:
            sync_project_link(conn, doc_id)  # a skill version tagged project:KEY
    return doc_id


def merge_docs(conn: sqlite3.Connection, src_id: str, dst_id: str) -> str:
    """Append document *src_id* to *dst_id* as a section and remove it (the
    way two project documents that raced from two devices become one).
    Returns the destination id."""
    with write(conn):
        src, dst = get_doc(conn, src_id), get_doc(conn, dst_id)
        if src is None or dst is None or src.id == dst.id:
            raise LedgerError("merge needs two different documents")
        if project_of(src.tags) is None or project_of(dst.tags) is None:
            raise LedgerError("only project documents merge, not a focus record or a skill version")
        section = f"{dst.body}\n\n## {src.title}\n\n{src.body}".strip()
        if len(section.encode("utf-8")) > DOC_BODY_MAX:
            raise LedgerError(f"merging would make {dst.id} larger than {DOC_BODY_MAX} bytes")
        conn.execute("UPDATE docs SET body = ?, hash = ?, updated = ? WHERE id = ?",
                     (section, content_hash(dst.title, section), now_iso(), dst.id))
        remove(conn, [src.id])
    return dst.id


def update_doc(conn: sqlite3.Connection, doc_id: str, *, title: str | None = None,
               body: str | None = None, alias: str | None = None, mime: str | None = None,
               privileged: bool = False) -> Doc:
    with write(conn):
        r = conn.execute("SELECT * FROM docs WHERE id = ? OR alias = ?", (doc_id, doc_id)).fetchone()
        if r is None:
            raise LedgerError(f"no document {doc_id!r}")
        have = _tags_of(conn, r["id"])
        if f"{STAGE_PREFIX}0" in have and not privileged:
            raise LedgerError(f"{r['id']} is baseline (stage 0): its text changes only from the local CLI")
        t = one_line(redact(title), TITLE_MAX) if title is not None else r["title"]
        if not t:
            raise LedgerError("title is required")
        b = redact(str(body).strip()) if body is not None else r["body"]
        m = _clean_mime(mime) if mime is not None else r["mime"]
        if f"{STAGE_PREFIX}0" in have and SKILL_TAG in have and body is not None:
            from shapa import stages

            # an active baseline skill counts its description: an edit may not take it past the cap
            stages.check_budget(conn, r["id"], stages.description_size(b))
        conn.execute("UPDATE docs SET title=?, body=?, mime=?, hash=?, alias=COALESCE(?, alias), "
                     "updated=? WHERE id=?", (t, b, m, content_hash(t, b), alias, now_iso(), r["id"]))
    return get_doc(conn, r["id"])


def tombstones(conn: sqlite3.Connection) -> list[dict]:
    if not _has_table(conn, "tombstones"):
        return []
    cols = "id, kind, created, deleted_at" + (
        ", uid" if any(r[1] == "uid" for r in conn.execute("PRAGMA table_info(tombstones)"))
        else ", '' AS uid")
    return [dict(r) for r in conn.execute(f"SELECT {cols} FROM tombstones ORDER BY deleted_at, id")]


def delete(conn: sqlite3.Connection, ids, *, tombstone: bool = True) -> int:
    """Delete rows, items or documents by id, with their tags and links, and
    leave a tombstone for each (a hosted store learns of the deletion from it).
    ``tombstone=False`` is for a deletion that came *from* the remote."""
    ids = [i for i in ids if i]
    if not ids:
        return 0
    with write(conn):
        return remove(conn, ids, tombstone=tombstone)


def remove(conn: sqlite3.Connection, ids, *, tombstone: bool = True,
           keep_inbound: bool = False) -> int:
    """:func:`delete` inside the caller's open transaction. ``keep_inbound``
    leaves other records' links *to* the removed id alone (sync does: they
    are other records' content)."""
    n, ts = 0, now_iso()
    for i in ids:
        for table in ("mri", "items", "docs"):
            found = conn.execute(f"SELECT created FROM {table} WHERE id = ?", (i,)).fetchone()
            if found is None:
                continue
            if tombstone:
                conn.execute("INSERT OR REPLACE INTO tombstones(id, kind, created, deleted_at, uid) "
                             "VALUES (?,?,?,?,?)",
                             (i, i[:1], found[0], ts, uid_of(conn, i) or legacy_uid(i, found[0])))
            n += conn.execute(f"DELETE FROM {table} WHERE id = ?", (i,)).rowcount
            conn.execute("DELETE FROM uids WHERE id = ?", (i,))
        conn.execute("DELETE FROM tags WHERE id = ?", (i,))
        if keep_inbound:
            conn.execute("DELETE FROM links WHERE src = ?", (i,))
        else:
            conn.execute("DELETE FROM links WHERE src = ? OR dst = ?", (i, i))
    return n


def normalize_label(text) -> str:
    """Case, width and invisible characters folded away, so a lookalike cannot dodge a reserved name."""
    return " ".join(_plain(text or "").split())


def check_row_tags(tags) -> None:
    """Refuse the learning loop's own tags from an entry point an agent reaches: nothing downstream
    trusts them more than the text they sit on."""
    for t in tags or ():
        n = normalize_label(t)
        if n in LEARNING_TAGS or n.startswith(LEARNING_TAG_PREFIXES):
            raise LedgerError(f"the tag {str(t)[:40]!r} is the learning loop's own (`shapa learn` writes it)")


def tag(conn: sqlite3.Connection, target: str, tags, *, privileged: bool = False) -> None:
    if kind_of(target) == DOC_KIND:
        clean = [t for t in (_clean_tag(x) for x in tags) if t]
        if any(t in RESERVED_DOC_TAGS for t in clean):
            raise LedgerError(f"the tags {', '.join(RESERVED_DOC_TAGS)} are reserved: "
                              "use `shapa focus` and `shapa skill`")
        check_project_tags([" ".join(str(x).split()) for x in tags])  # before any truncation
        moves = sorted({t for t in clean if t.startswith(PROJECT_PREFIX)})
        if len(moves) > 1:
            raise LedgerError("a document belongs to one project")
        doc = get_doc(conn, target)
        if doc is not None and any(t in RESERVED_DOC_TAGS for t in doc.tags):
            raise LedgerError(f"{target} is the focus record or a skill version: its tags are the engine's, "
                              "use `shapa focus` and `shapa skill`")
        if doc is not None and moves:
            was, new = project_of(doc.tags), moves[0][len(PROJECT_PREFIX):]
            # the lean gate also covers a retag: no moving into a project another document holds
            if was is not None and new != was:
                held = project_doc(conn, new, exclude=doc.id)
                if held is not None:
                    raise DocExists(held.id, held.title, new)
    with write(conn):
        if not _stored(conn, target):
            raise LedgerError(f"no record {target!r} to tag")
        insert, drop = _guard_tags(conn, target, tags, letter=kind_of(target) or "", privileged=privileged)
        _write_tags(conn, target, insert, drop)
        _unlink_left_projects(conn, target, drop)
        if insert or drop:
            sync_project_link(conn, target)
    if kind_of(target) in MRI_KINDS:
        _after_row_named_skill(conn, target)


def _stored(conn: sqlite3.Connection, record_id: str) -> bool:
    table = {"F": "items", "J": "items", "T": "items", "D": "docs"}.get(record_id[:1], "mri")
    return conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (record_id,)).fetchone() is not None


def retag(conn: sqlite3.Connection, target: str, *, add=(), drop_prefix: str | None = None,
          drop=(), privileged: bool = False) -> None:
    """Replace some of a record's tags (every tag starting with *drop_prefix*,
    and each of *drop*), add *add*, and stamp the record updated so a sync
    sees the change. Used for state kept in tags (a skill's status, a
    feature's rank, a record's stage). Dropping ``stage:0`` takes *privileged*."""
    with write(conn):
        have = set(_tags_of(conn, target))
        gone = {t for t in have if t in drop or (drop_prefix and t.startswith(drop_prefix))}
        if f"{STAGE_PREFIX}0" in gone and not privileged:
            raise LedgerError(f"{target} is baseline (stage 0): it leaves the baseline only from the "
                              "local CLI")
        insert, moved = _guard_tags(conn, target, add, letter=kind_of(target) or "",
                                    privileged=privileged, have=have - gone)
        _write_tags(conn, target, insert, gone | set(moved))
        _unlink_left_projects(conn, target, gone | set(moved))
        if insert or gone or moved:
            sync_project_link(conn, target)
        table = {"F": "items", "J": "items", "T": "items", "D": "docs"}.get(target[:1], "mri")
        conn.execute(f"UPDATE {table} SET updated = ? WHERE id = ?", (now_iso(), target))


def link(conn: sqlite3.Connection, src: str, dst: str, rel: str, *, privileged: bool = False) -> None:
    if rel not in LINK_RELS:
        raise LedgerError(f"rel must be one of {LINK_RELS}")
    with write(conn):
        if (rel == "supersedes" and not privileged
                and f"{STAGE_PREFIX}0" in _tags_of(conn, dst)):
            raise LedgerError(f"{dst} is baseline (stage 0): only the local CLI may supersede it")
        conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) VALUES (?, ?, ?)", (src, dst, rel))
    if rel == "about" and kind_of(src) in MRI_KINDS:
        _after_row_named_skill(conn, src)


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
    learned: list[str] = field(default_factory=list)

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
        if self.learned:
            out.append(f"learned corrections removed (their evidence sits in a principle): {len(self.learned)}")
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
       ``expired`` - except an item claimed and updated within that window,
       and every ancestor of one (work in progress keeps its feature).
    2. Closed items are deleted, except those closed at or after
       *keep_closed_since* (the merge that triggered this sweep keeps its
       rows until the next one; without it, the sweep's own start, so a sweep
       never deletes what it just closed).
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

    keep_closed_since = keep_closed_since or now_iso(now_dt)
    parents = dict(conn.execute("SELECT id, parent FROM items").fetchall())
    active: set[str] = set()
    for (iid,) in conn.execute("SELECT id FROM items WHERE status = 'claimed' AND updated >= ?",
                               (cutoff,)):
        while iid and iid not in active:
            active.add(iid)
            iid = parents.get(iid)
    live = conn.execute("SELECT id FROM items WHERE status != 'closed' AND created < ? "
                        "ORDER BY kind, CAST(SUBSTR(id, 2) AS INTEGER)", (cutoff,)).fetchall()
    # the operator's task list: an open human_task or agent_task never expires, in any wiki
    kept_tasks = {r[0] for r in conn.execute(
        "SELECT t.id FROM tags t JOIN items i ON i.id = t.id WHERE t.tag IN (?, ?) AND i.kind = 'T' "
        "AND i.status != 'closed'", tuple(OWNER_TAGS))}
    for (iid,) in live:
        if iid in active or iid in report.expired or iid in kept_tasks:
            continue
        report.expired.extend(c for c in close(conn, iid, "expired", now=now_dt)
                              if c not in report.expired)

    with write(conn):
        report.deleted_items = [r[0] for r in conn.execute(
            "SELECT id FROM items WHERE status = 'closed' AND (closed IS NULL OR closed < ?)",
            (keep_closed_since,))]
    delete(conn, report.deleted_items)

    with write(conn):
        report.superseded = [r[0] for r in conn.execute(
            "SELECT DISTINCT l.dst FROM links l JOIN mri m ON m.id = l.dst WHERE l.rel = 'supersedes'")]
    delete(conn, report.superseded)

    with write(conn):   # a learned correction is kept as evidence inside its principle: the row itself goes
        report.learned = [r[0] for r in conn.execute(
            "SELECT t.id FROM tags t JOIN mri m ON m.id = t.id WHERE t.tag = 'learned' AND m.kind = 'I'")]
    delete(conn, report.learned)

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
                    # the baseline is never handed to another row's text: of a pair where exactly
                    # one is stage 0, that one stays (its text untouched) and the other goes
                    keep, drop = older, newer
                    if f"{STAGE_PREFIX}0" in newer.tags and f"{STAGE_PREFIX}0" not in older.tags:
                        keep, drop = newer, older
                    elif ("principle" in newer.tags and "principle" not in older.tags
                          and f"{STAGE_PREFIX}0" not in older.tags):
                        keep, drop = newer, older   # the evidence rides on the principle
                    if "principle" in drop.tags and older.hash != newer.hash:
                        continue  # a principle's evidence is never dropped with a near-duplicate
                    _merge_into(conn, drop.id, keep.id)
                    gone.add(drop.id)
                    report.duplicates.append((drop.id, keep.id))
                    if drop is older:
                        break
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
            if any(t in KEPT_MEMORY_TAGS or t.startswith(STAGE_PREFIX) for t in r.tags):
                continue  # ideas and decisions are written to be kept; a staged memory is served
            if created and created < stale_cut and (last_dt is None or last_dt < stale_cut) and not count:
                report.stale_memories.append(r.id)
        delete(conn, report.stale_memories)

    with write(conn):
        cur = conn.execute(
            "DELETE FROM links WHERE src NOT IN (SELECT id FROM mri UNION SELECT id FROM items UNION SELECT id FROM docs) "
            "OR dst NOT IN (SELECT id FROM mri UNION SELECT id FROM items UNION SELECT id FROM docs)")
        report.dangling_links = cur.rowcount
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT id FROM mri UNION SELECT id FROM items UNION SELECT id FROM docs)")
    return report


_SUM_TAGS = ("violations:", "repeats:", "evidence:")
_MAX_TAGS = ("revisions:", "last-seen:")


def _merge_into(conn, src: str, dst: str) -> None:
    """Move *src*'s tags and links onto *dst* (before *src* is deleted). A counter tag exists once on
    the result (counts add, revisions and last-seen keep the larger), and no ``repeat-of`` points at
    the row itself: others that pointed at *src* now point at *dst*."""
    with write(conn):
        if f"{STAGE_PREFIX}0" in _tags_of(conn, dst):
            return  # nothing moves onto the baseline: no tag, no link (a `supersedes` could delete it)
        # A merge never copies a stage marker onto another row (the kept row's own stands), and a
        # record belongs to one project: the kept row's own wins there too.
        keeps_project = any(t.startswith(PROJECT_PREFIX) for t in _tags_of(conn, dst))
        have = {r[0] for r in conn.execute("SELECT tag FROM tags WHERE id = ?", (dst,))}
        other = {r[0] for r in conn.execute("SELECT tag FROM tags WHERE id = ?", (src,))}
        for prefix in _SUM_TAGS + _MAX_TAGS:
            vals = [t[len(prefix):] for t in sorted(have | other) if t.startswith(prefix)]
            if not vals:
                continue
            if prefix in _SUM_TAGS:
                new = str(sum(int(v) for v in vals if v.isdigit()))
            else:
                new = max(vals)
            conn.execute("DELETE FROM tags WHERE id IN (?, ?) AND tag LIKE ?", (dst, src, prefix + "%"))
            conn.execute("INSERT OR IGNORE INTO tags(id, tag) VALUES (?, ?)", (dst, prefix + new))
        conn.execute("INSERT OR IGNORE INTO tags(id, tag) SELECT ?, tag FROM tags WHERE id = ? "
                     "AND tag NOT LIKE 'violations:%' AND tag NOT LIKE 'repeats:%' AND tag NOT LIKE 'evidence:%' "
                     "AND tag NOT LIKE 'revisions:%' AND tag NOT LIKE 'last-seen:%' "
                     "AND substr(tag, 1, 6) != 'stage:' AND substr(tag, 1, 15) != 'stage-proposed:' "
                     "AND (? = 0 OR substr(tag, 1, 8) != 'project:')", (dst, src, int(keeps_project)))
        conn.execute("DELETE FROM tags WHERE id = ? AND tag IN (?, ?)", (dst, f"repeat-of:{dst}", f"repeat-of:{src}"))
        conn.execute("UPDATE OR IGNORE tags SET tag = ? WHERE tag = ?", (f"repeat-of:{dst}", f"repeat-of:{src}"))
        conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) SELECT ?, dst, rel FROM links "
                     "WHERE src = ? AND dst != ?", (dst, src, dst))
        conn.execute("INSERT OR IGNORE INTO links(src, dst, rel) SELECT src, ?, rel FROM links "
                     "WHERE dst = ? AND src != ?", (dst, src, dst))


def conflicts(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [tuple(r) for r in conn.execute(
        "SELECT src, dst FROM links WHERE rel = 'conflicts' ORDER BY src, dst")]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {k: 0 for k in ITEM_KINDS + MRI_KINDS + (DOC_KIND,)}
    for kind, n in conn.execute("SELECT kind, COUNT(*) FROM items WHERE status != 'closed' "
                                "GROUP BY kind"):
        out[kind] = n
    for kind, n in conn.execute("SELECT kind, COUNT(*) FROM mri GROUP BY kind"):
        out[kind] = n
    if _has_table(conn, "docs"):
        out[DOC_KIND] = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
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
        for table, cols in (("mri", ("summary", "body", "source", "alias", "session")),
                            ("items", ("title", "body", "verify", "git_ref", "alias", "claimed_by")),
                            ("docs", ("title", "body", "source", "alias"))):
            if not _has_table(conn, table):
                continue
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
        # Only a hash that changed is rewritten: a scrub that finds nothing must
        # leave the file byte-identical, or `shapa commit` commits it every session.
        for row in conn.execute("SELECT id, summary, body FROM mri").fetchall():
            digest = content_hash(row["summary"], row["body"])
            conn.execute("UPDATE mri SET hash = ? WHERE id = ? AND hash IS NOT ?",
                         (digest, row["id"], digest))
        for row in (conn.execute("SELECT id, title, body FROM docs").fetchall()
                    if _has_table(conn, "docs") else []):
            digest = content_hash(row["title"], row["body"])
            conn.execute("UPDATE docs SET hash = ? WHERE id = ? AND hash IS NOT ?",
                         (digest, row["id"], digest))
    return changed
