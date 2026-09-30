"""The per-root sqlite index - shapa-backend-spec.md §5.

One sqlite file per wiki root (``<root>/.shapa-index.db``), never a hard
dependency: every consumer keeps working off the current full-directory
scan + hand-rolled BM25 (``shapa.bm25``) if the index is missing, stale, or
corrupt - this module only ever makes reads *faster*, never a precondition
for correctness.

- **FTS5 availability probe** is the real ``CREATE VIRTUAL TABLE ... USING
  fts5(...)`` call itself (:func:`fts5_available`), not
  ``PRAGMA compile_options`` - the pragma is unreliable across CPython
  builds; attempting the real table creation and catching the failure is
  the honest probe. When it fails, :func:`search` falls back to the exact
  same ``shapa.bm25`` scoring ``fetch.py`` has always used - one shared
  implementation, so the fallback can never quietly diverge from it.
- **Incremental sync** (:func:`sync`) is keyed on ``(mtime_ns, size)``: a
  file whose signature is unchanged since the last sync is never re-read,
  re-parsed, or rewritten into the index - only added/changed/removed
  files touch the database at all.
- **Usage counters live here**, keyed by ``(root, note id)``
  (:func:`record_use`/:func:`get_use`/:func:`get_all_uses`) - see
  shapa-backend-spec.md §10 decision 7, "reads never write notes": a note's
  own frontmatter changes only when its content does; ``uses``/``last_used``
  move into this index so a read path (``fetch.py``) never dirties a file.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shapa import frontmatter
from shapa.bm25 import bm25_scores, words
from shapa.nodes import extract_links

INDEX_FILENAME = ".shapa-index.db"

_SCHEMA_NOTES = """
CREATE TABLE IF NOT EXISTS notes (
    path TEXT PRIMARY KEY,
    id TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    body TEXT NOT NULL,
    meta_json TEXT NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    last_used TEXT
)
"""
_SCHEMA_ID_INDEX = "CREATE INDEX IF NOT EXISTS notes_id_idx ON notes(id)"
_SCHEMA_FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(id, body)"

_FTS5_AVAILABLE: bool | None = None


def fts5_available() -> bool:
    """Whether this Python's sqlite3 build can create an FTS5 table.

    Cached after the first real attempt (against a throwaway in-memory
    database, so this never touches disk) - the answer is a property of the
    sqlite3 build, not of any particular wiki root. Tests force the
    unavailable path by monkeypatching this function directly, matching
    ``embed.available()``'s pattern.
    """
    global _FTS5_AVAILABLE
    if _FTS5_AVAILABLE is None:
        try:
            probe = sqlite3.connect(":memory:")
            try:
                probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
                _FTS5_AVAILABLE = True
            finally:
                probe.close()
        except sqlite3.OperationalError:
            _FTS5_AVAILABLE = False
    return _FTS5_AVAILABLE


def _has_fts5_table(conn: sqlite3.Connection) -> bool:
    """Whether *this database file* actually has ``notes_fts`` - the source
    of truth for which path :func:`sync`/:func:`search` take, so behavior
    stays self-consistent with what schema creation actually did (rather
    than re-checking the live, cacheable :func:`fts5_available` flag, which
    a test may flip after the db file already exists)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes_fts'"
    ).fetchone()
    return row is not None


def db_path(root) -> Path:
    return Path(root) / INDEX_FILENAME


def open_index(root) -> sqlite3.Connection:
    """Open (creating if needed) the index for *root*. Caller closes it."""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # some filesystems (network mounts, read-only tmpfs) reject WAL
    conn.execute(_SCHEMA_NOTES)
    conn.execute(_SCHEMA_ID_INDEX)
    if fts5_available():
        try:
            conn.execute(_SCHEMA_FTS)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


@dataclass
class SyncResult:
    """Counts from one :func:`sync` pass - mainly for tests to assert that
    an unchanged file is genuinely never touched."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0


def sync(root, conn: sqlite3.Connection | None = None) -> SyncResult:
    """Bring *root*'s index up to date with what is on disk right now.

    A file is re-read and re-written into the index only when its
    ``(mtime_ns, size)`` signature differs from the stored one - the
    incremental-by-mtime fix (shapa-backend-spec.md §5). Cheap and
    idempotent: calling this on an unchanged wiki does one directory scan
    and one small `SELECT`, no parsing, no writes.
    """
    root = Path(root)
    owns_conn = conn is None
    conn = conn or open_index(root)
    result = SyncResult()
    try:
        on_disk: dict[str, tuple[int, int]] = {}
        if root.is_dir():
            for p in sorted(root.rglob("*.md")):
                try:
                    st = p.stat()
                except OSError:
                    continue
                on_disk[str(p)] = (st.st_mtime_ns, st.st_size)

        existing = {
            row["path"]: (row["mtime_ns"], row["size"])
            for row in conn.execute("SELECT path, mtime_ns, size FROM notes")
        }
        has_fts = _has_fts5_table(conn)

        for path_str, sig in on_disk.items():
            if existing.get(path_str) == sig:
                result.unchanged += 1
                continue

            parsed = frontmatter.parse(Path(path_str))
            if parsed.error:
                continue
            node_id = str(parsed.meta.get("id") or Path(path_str).stem)
            meta_json = json.dumps(parsed.meta, default=str)
            is_new = path_str not in existing

            try:
                conn.execute(
                    """
                    INSERT INTO notes(path, id, mtime_ns, size, body, meta_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        id=excluded.id, mtime_ns=excluded.mtime_ns,
                        size=excluded.size, body=excluded.body,
                        meta_json=excluded.meta_json
                    """,
                    (path_str, node_id, sig[0], sig[1], parsed.body, meta_json),
                )
            except sqlite3.IntegrityError:
                continue  # never let one malformed/duplicate note break sync

            if has_fts:
                conn.execute("DELETE FROM notes_fts WHERE id = ?", (node_id,))
                conn.execute(
                    "INSERT INTO notes_fts(id, body) VALUES (?, ?)", (node_id, parsed.body)
                )

            if is_new:
                result.added += 1
            else:
                result.updated += 1

        for gone_path in set(existing) - set(on_disk):
            row = conn.execute("SELECT id FROM notes WHERE path = ?", (gone_path,)).fetchone()
            conn.execute("DELETE FROM notes WHERE path = ?", (gone_path,))
            if row is not None and has_fts:
                conn.execute("DELETE FROM notes_fts WHERE id = ?", (row["id"],))
            result.removed += 1

        conn.commit()
        return result
    finally:
        if owns_conn:
            conn.close()


def _fallback_search(conn: sqlite3.Connection, query: str, k: int | None) -> list[tuple[str, float]]:
    """The FTS5-unavailable path: identical scoring to ``fetch.py``'s
    hand-rolled BM25 (``shapa.bm25``), fed the same features it always used
    - body text plus the id's own words and its wikilink outlinks - so this
    is not merely "close," it is the same function on the same tokens."""
    rows = conn.execute("SELECT id, body FROM notes").fetchall()
    if not rows:
        return []
    docs: dict[str, list[str]] = {}
    for row in rows:
        nid, body = row["id"], row["body"]
        outlinks = extract_links(body)
        id_topic = nid.replace("-", " ") + " " + " ".join(outlinks)
        docs[nid] = words(body + " " + id_topic)
    scores = bm25_scores(query, docs)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:k] if k else ranked


def _fts5_search(conn: sqlite3.Connection, query: str, k: int | None) -> list[tuple[str, float]]:
    toks = words(query)
    if not toks:
        return []
    match = " OR ".join(f'"{t}"' for t in toks)
    limit = k if k else -1
    try:
        rows = conn.execute(
            "SELECT id, bm25(notes_fts) AS rank FROM notes_fts "
            "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    # sqlite's bm25() is lower-is-better; negate so higher-is-better matches
    # every other relevance score in this codebase (fetch.py's cosine/BM25).
    return [(row["id"], -float(row["rank"])) for row in rows]


def search(root, query: str, k: int | None = None, conn: sqlite3.Connection | None = None
           ) -> list[tuple[str, float]]:
    """Rank every indexed note in *root* against *query*, best first.

    Uses the FTS5 table when this database has one, else the identical
    hand-rolled BM25 fallback (:func:`_fallback_search`). Does NOT sync
    first - callers that need the index current (the daemon; anything
    where staleness matters) call :func:`sync` themselves right before.
    """
    root = Path(root)
    owns_conn = conn is None
    conn = conn or open_index(root)
    try:
        if _has_fts5_table(conn):
            return _fts5_search(conn, query, k)
        return _fallback_search(conn, query, k)
    finally:
        if owns_conn:
            conn.close()


def _now_iso(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_use(root, note_id: str, now: datetime | None = None,
                conn: sqlite3.Connection | None = None) -> int:
    """Increment *note_id*'s use counter in *root*'s index and return the
    new count (0 if the note isn't indexed - never blocks, never raises).

    Syncs first so a note captured moments ago and never yet fetched still
    gets its row before the counter bump (best-effort; a note store.py
    cannot find at all is simply not counted, matching the never-blocks
    contract every read-path module in this codebase shares).
    """
    root = Path(root)
    owns_conn = conn is None
    try:
        conn = conn or open_index(root)
    except (OSError, sqlite3.Error):
        return 0
    try:
        try:
            sync(root, conn=conn)
        except sqlite3.Error:
            pass
        cur = conn.execute(
            "UPDATE notes SET uses = uses + 1, last_used = ? WHERE id = ?",
            (_now_iso(now), note_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return 0
        row = conn.execute("SELECT uses FROM notes WHERE id = ?", (note_id,)).fetchone()
        return int(row["uses"]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        if owns_conn:
            conn.close()


def get_use(root, note_id: str, conn: sqlite3.Connection | None = None
            ) -> tuple[int, str | None]:
    """Return ``(uses, last_used)`` for *note_id* in *root*'s index, or
    ``(0, None)`` when it isn't indexed."""
    root = Path(root)
    owns_conn = conn is None
    try:
        conn = conn or open_index(root)
    except (OSError, sqlite3.Error):
        return (0, None)
    try:
        row = conn.execute(
            "SELECT uses, last_used FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        return (int(row["uses"]), row["last_used"]) if row else (0, None)
    finally:
        if owns_conn:
            conn.close()


def get_all_uses(root, conn: sqlite3.Connection | None = None
                  ) -> dict[str, tuple[int, str | None]]:
    """``{note id: (uses, last_used)}`` for every indexed note in *root* -
    one query, for callers (``fetch.py``'s value scoring) that need every
    note's live count rather than one at a time."""
    root = Path(root)
    owns_conn = conn is None
    try:
        conn = conn or open_index(root)
    except (OSError, sqlite3.Error):
        return {}
    try:
        return {
            row["id"]: (int(row["uses"]), row["last_used"])
            for row in conn.execute("SELECT id, uses, last_used FROM notes")
        }
    except sqlite3.Error:
        return {}
    finally:
        if owns_conn:
            conn.close()
