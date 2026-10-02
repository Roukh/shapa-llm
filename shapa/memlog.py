"""v3 memory records (wiki format 3) - the captured-memory store.

**Source of truth: an append-only JSONL log inside the wiki.** Every
captured memory is one line in ``<wiki>/memory/YYYY-MM.jsonl``, committed
with the wiki, so a fresh clone keeps every memory. The wiki's
``.gitattributes`` marks the log ``merge=union``: two branches that each
appended lines merge without a conflict. A line is never edited or
removed - replacing a memory appends a new record whose ``supersedes``
names the old id, and archiving appends an ``{"op": "archive"}`` line.

Record fields (one JSON object per line, this key order)::

    id          "m-" + the first 10 hex chars of ``hash`` (content-derived:
                the same memory captured twice, or on two clones, is one id)
    created     ISO-8601 UTC
    session     the capturing session's id (8 chars), or ""
    repo        the repo the memory is about, or null for a global memory
    scope       "global" | "repo" - matches the wiki the line lives in
    kind        decision | fact | gotcha | outcome | open_question | preference
    summary     one line, <=160 chars - the only text a prompt ever injects
    body        <=600 chars - the full text, served on demand (``shapa get``)
    tags        list of short strings (paths, PR numbers, topics)
    source      where it came from (``capture:report``, ``memri:<row>``, ...)
    supersedes  the id this record replaces, or null
    hash        sha1 of the redacted ``summary + "\\n" + body``

Every field is redacted (:mod:`shapa.redact`) before the hash is computed,
before any vector exists, and before the line is written.

**Derived index: tables in the wiki's gitignored ``.shapa-index.db``.**
:func:`sync` brings them current from the log incrementally - a log that
only grew is read from where the last sync stopped; anything else (a merge
that inserted lines, a removed file) rebuilds the record tables from the
log, which is cheap. The index holds what the log must not: usage counters
(``memory_uses``) and embedding vectors cached by content hash
(``memory_vectors``) - neither is ever rebuilt away. Lexical search is an
FTS5 ``unicode61`` table (hand-rolled BM25 when the sqlite build lacks
FTS5); semantic search is float32 model2vec vectors when the ``[semantic]``
extra is installed. :func:`search_mode` reports which one is live.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: O_APPEND only
    fcntl = None

from shapa import embed, redact, store
from shapa.bm25 import bm25_scores, words

MEMORY_DIRNAME = "memory"
LOG_SUFFIX = ".jsonl"
GITATTRIBUTES_LINE = f"{MEMORY_DIRNAME}/*{LOG_SUFFIX} merge=union"

KINDS = ("decision", "fact", "gotcha", "outcome", "open_question", "preference")
SCOPES = ("global", "repo")
SUMMARY_MAX = 160
BODY_MAX = 600
MAX_TAGS = 8
TAG_MAX = 40
ID_PREFIX = "m-"
FIELDS = ("id", "created", "session", "repo", "scope", "kind", "summary", "body",
          "tags", "source", "supersedes", "hash")

#: Shingle Jaccard at or above this against a live record of the same kind
#: is a duplicate (never written); in [SUPERSEDE, DUP) it is an update and
#: the new record supersedes the old one.
DUP_THRESHOLD = 0.9
SUPERSEDE_THRESHOLD = 0.6
#: Hard cap on records one :func:`append` call writes - a runaway extractor
#: or a pathological transcript can never flood the log.
MAX_APPEND = 24

#: Value scoring inputs for a memory (shapa.score.score_meta) - captured
#: memories rank below curated notes of the same age unless they get used.
KIND_CONSEQUENCE = {"preference": 6, "gotcha": 6, "decision": 5, "open_question": 5,
                    "fact": 4, "outcome": 4}
KIND_LOCUS = {"preference": "output-meta", "gotcha": "output-meta"}

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"(?:#\d+)|(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]*[A-Za-z0-9_])|(?:\b[0-9a-f]{7,40}\b)")
_FTS_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")

_GOTCHA_KW = re.compile(
    r"\b(gotcha|broke|broken|bug|footgun|regression|bypass|incident|mistake|wrong|"
    r"fail(?:s|ed|ure|ing)?|collapse|crash(?:ed|es)?|silently|data[- ]loss|leak(?:s|ed)?)\b", re.I)
_OPEN_KW = re.compile(
    r"\?\s*$|\b(unclear|unknown|tbd|undecided|pending|awaiting|open question|"
    r"needs? (?:a |an )?(?:operator )?decision|not (?:yet )?(?:confirmed|resolved|decided))\b", re.I)
_PREF_KW = re.compile(
    r"\b(never|always|don'?t|do not|must not|must|stop doing|from now on|prefer(?:s|red)?|"
    r"instead of)\b", re.I)
_OUTCOME_KW = re.compile(
    r"\b(shipped|merged|fixed|completed|done|resolved|closed|deployed|verified|built|"
    r"added|removed|migrated|released)\b", re.I)


# --- records -------------------------------------------------------------------

@dataclass
class Record:
    id: str
    created: str
    session: str
    repo: str | None
    scope: str
    kind: str
    summary: str
    body: str
    tags: list[str] = field(default_factory=list)
    source: str = ""
    supersedes: str | None = None
    hash: str = ""

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps({k: data[k] for k in FIELDS}, ensure_ascii=False, separators=(",", ":"))

    def embed_text(self) -> str:
        return f"{self.kind} {' '.join(self.tags)}\n{self.summary}\n{self.body}"


@dataclass
class Op:
    """A non-record log line. Only ``archive`` exists: it retires *target*
    from recall without deleting anything."""

    op: str
    target: str
    created: str
    reason: str = ""

    def to_json(self) -> str:
        return json.dumps({"op": self.op, "target": self.target, "created": self.created,
                           "reason": self.reason}, ensure_ascii=False, separators=(",", ":"))


def now_iso(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def cut(text: str, limit: int) -> str:
    """*text* on one line, cut at a word boundary to at most *limit* chars."""
    text = one_line(text)
    if len(text) <= limit:
        return text
    head = text[:limit]
    space = head.rfind(" ")
    return (head[:space] if space > limit // 2 else head).rstrip(" ,;:-")


def content_hash(summary: str, body: str) -> str:
    return hashlib.sha1(f"{summary}\n{body}".encode("utf-8")).hexdigest()


def classify_kind(text: str, default: str = "fact") -> str:
    """Keyword kind for one atomic piece of text: gotcha beats open question
    beats preference beats outcome; *default* otherwise."""
    if _GOTCHA_KW.search(text):
        return "gotcha"
    if _OPEN_KW.search(text):
        return "open_question"
    if _PREF_KW.search(text):
        return "preference"
    if _OUTCOME_KW.search(text):
        return "outcome"
    return default if default in KINDS else "fact"


def extract_tags(text: str) -> list[str]:
    """Path-, PR- and commit-like tokens in *text* - cheap entities for
    lexical recall."""
    out = []
    for m in _TAG_RE.findall(text or ""):
        tag = m.strip(".,;:()[]`'\"")
        if 2 <= len(tag) <= TAG_MAX and tag not in out:
            out.append(tag)
    return out[:MAX_TAGS]


def _clean_tags(tags) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        if not isinstance(t, str):
            continue
        t = redact.redact(one_line(t))[:TAG_MAX]
        if t and redact.PLACEHOLDER not in t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def make_record(*, kind: str, summary: str, body: str = "", tags=(), source: str = "",
                session: str = "", repo: str | None = None, scope: str = "repo",
                supersedes: str | None = None, created: str | None = None) -> Record | None:
    """Build one normalized, redacted record, or ``None`` when nothing is
    left of it. *summary* falls back to a cut of *body*; *body* falls back
    to *summary*."""
    body_text = redact.redact(one_line(body))
    summary_text = redact.redact(one_line(summary)) or body_text
    summary_text = cut(summary_text, SUMMARY_MAX)
    body_text = cut(body_text or summary_text, BODY_MAX)
    if not summary_text or summary_text == redact.PLACEHOLDER:
        return None
    h = content_hash(summary_text, body_text)
    return Record(
        id=ID_PREFIX + h[:10],
        created=created or now_iso(),
        session=one_line(session)[:8],
        repo=one_line(repo) or None if repo else None,
        scope=scope if scope in SCOPES else "repo",
        kind=kind if kind in KINDS else "fact",
        summary=summary_text,
        body=body_text,
        tags=_clean_tags(tags),
        source=one_line(source)[:120],
        supersedes=supersedes or None,
        hash=h,
    )


def parse_line(line: str) -> Record | Op | None:
    """One log line -> a :class:`Record`, an :class:`Op`, or ``None`` when
    it is blank or malformed (never raises)."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if "op" in obj:
        target = obj.get("target")
        if obj.get("op") == "archive" and isinstance(target, str) and target:
            return Op("archive", target, str(obj.get("created") or ""), str(obj.get("reason") or ""))
        return None
    rid, summary = obj.get("id"), obj.get("summary")
    if not isinstance(rid, str) or not rid or not isinstance(summary, str) or not summary.strip():
        return None
    body = obj.get("body") if isinstance(obj.get("body"), str) else ""
    tags = obj.get("tags") if isinstance(obj.get("tags"), list) else []
    kind = obj.get("kind") if obj.get("kind") in KINDS else "fact"
    scope = obj.get("scope") if obj.get("scope") in SCOPES else "repo"
    repo = obj.get("repo") if isinstance(obj.get("repo"), str) else None
    sup = obj.get("supersedes") if isinstance(obj.get("supersedes"), str) and obj.get("supersedes") else None
    return Record(
        id=rid, created=str(obj.get("created") or ""), session=str(obj.get("session") or ""),
        repo=repo, scope=scope, kind=kind, summary=summary, body=body,
        tags=[t for t in tags if isinstance(t, str)], source=str(obj.get("source") or ""),
        supersedes=sup, hash=str(obj.get("hash") or content_hash(summary, body)),
    )


# --- the log -----------------------------------------------------------------

def log_dir(root) -> Path:
    return Path(root) / MEMORY_DIRNAME


def log_files(root) -> list[Path]:
    d = log_dir(root)
    try:
        return sorted(p for p in d.glob(f"*{LOG_SUFFIX}") if p.is_file())
    except OSError:
        return []


def has_log(root) -> bool:
    return bool(log_files(root))


def current_log(root, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    return log_dir(root) / f"{now.astimezone(timezone.utc):%Y-%m}{LOG_SUFFIX}"


@dataclass
class LogView:
    """Everything the log says, resolved: ``records`` keeps the first line
    seen per id (ids are content-derived, so a repeat is the same memory),
    ``archived`` maps an archived id to its archive op."""

    records: dict[str, Record] = field(default_factory=dict)
    archived: dict[str, Op] = field(default_factory=dict)
    malformed: int = 0
    lines: int = 0

    @property
    def superseded_by(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for r in self.records.values():
            if r.supersedes and r.supersedes in self.records:
                out.setdefault(r.supersedes, r.id)
        return out

    def status(self, rid: str) -> str:
        if rid in self.archived:
            return "archived"
        if rid in self.superseded_by:
            return "superseded"
        return "active" if rid in self.records else "missing"

    def active(self) -> list[Record]:
        dead = set(self.archived) | set(self.superseded_by)
        return [r for r in self.records.values() if r.id not in dead]


def _read_lines(view: LogView, text: str) -> None:
    for line in text.splitlines():
        if not line.strip():
            continue
        view.lines += 1
        item = parse_line(line)
        if item is None:
            view.malformed += 1
        elif isinstance(item, Op):
            view.archived.setdefault(item.target, item)
        else:
            view.records.setdefault(item.id, item)


def read_log(root) -> LogView:
    """Parse every log file under *root* (pure read, no index)."""
    view = LogView()
    for path in log_files(root):
        try:
            _read_lines(view, path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return view


def ensure_gitattributes(root) -> bool:
    """Make sure ``<root>/.gitattributes`` marks the log ``merge=union``.
    Returns whether the file changed."""
    path = Path(root) / ".gitattributes"
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return False
    if any(line.strip() == GITATTRIBUTES_LINE for line in text.splitlines()):
        return False
    sep = "" if not text or text.endswith("\n") else "\n"
    path.write_text(text + sep + "# shapa: memory log lines merge as a union (append-only)\n"
                    + GITATTRIBUTES_LINE + "\n", encoding="utf-8")
    return True


@contextmanager
def _locked_append(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _shingles(text: str) -> set:
    from shapa.maintain import _shingles as sh, _tokens
    return sh(_tokens(text))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class AppendResult:
    written: list[Record] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    superseded: dict[str, str] = field(default_factory=dict)  # new id -> old id
    path: Path | None = None


def append(root, records, *, ops=(), dedup: bool = True, now: datetime | None = None) -> AppendResult:
    """Append *records* (and archive *ops*) to *root*'s current month log,
    under an exclusive lock on that file so concurrent sessions never
    interleave a check with a write.

    With *dedup* (the default), a record is dropped when its id is already
    in the log (an exact duplicate) or when it is a near-duplicate
    (shingle Jaccard >= :data:`DUP_THRESHOLD`) of a live record of the same
    kind; a record in [:data:`SUPERSEDE_THRESHOLD`, DUP) of one live record
    of the same kind and repo is written with ``supersedes`` set to it.
    Never writes more than :data:`MAX_APPEND` records per call."""
    root = Path(root)
    result = AppendResult()
    candidates = [r for r in records if isinstance(r, Record)][:MAX_APPEND]
    ops = list(ops)
    if not candidates and not ops:
        return result
    path = current_log(root, now)
    result.path = path
    with _locked_append(path) as fd:
        view = read_log(root)
        live = view.active() if dedup else []
        by_kind: dict[str, list[tuple[Record, set]]] = {}
        if dedup:
            for r in live:
                by_kind.setdefault(r.kind, []).append((r, _shingles(f"{r.summary} {r.body}")))
        seen: set[str] = set(view.records)
        lines: list[str] = []
        for rec in candidates:
            if rec.id in seen:
                result.duplicates.append(rec.id)
                continue
            if dedup:
                sh = _shingles(f"{rec.summary} {rec.body}")
                best, best_sim = None, 0.0
                for other, osh in by_kind.get(rec.kind, []):
                    sim = _jaccard(sh, osh)
                    if sim > best_sim:
                        best, best_sim = other, sim
                if best is not None and best_sim >= DUP_THRESHOLD:
                    result.duplicates.append(rec.id)
                    continue
                if (best is not None and best_sim >= SUPERSEDE_THRESHOLD and not rec.supersedes
                        and best.repo == rec.repo and best.id not in result.superseded.values()):
                    rec.supersedes = best.id
                    result.superseded[rec.id] = best.id
                by_kind.setdefault(rec.kind, []).append((rec, sh))
            seen.add(rec.id)
            lines.append(rec.to_json())
            result.written.append(rec)
        for op in ops:
            if isinstance(op, Op) and op.target not in view.archived:
                lines.append(op.to_json())
        if lines:
            os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    if result.written or ops:
        try:
            ensure_gitattributes(root)
        except OSError:
            pass
    return result


def archive(root, target: str, reason: str = "", now: datetime | None = None) -> bool:
    """Retire *target* from recall by appending an archive op (nothing is
    deleted). Returns False when *target* is unknown or already archived."""
    view = read_log(root)
    if target not in view.records or target in view.archived:
        return False
    append(root, [], ops=[Op("archive", target, now_iso(now), reason)], now=now)
    return True


# --- the derived index ---------------------------------------------------------

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS memories (
        rowid INTEGER PRIMARY KEY,
        id TEXT UNIQUE NOT NULL,
        created TEXT, session TEXT, repo TEXT, scope TEXT, kind TEXT,
        summary TEXT NOT NULL, body TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
        source TEXT, supersedes TEXT, hash TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1)""",
    "CREATE INDEX IF NOT EXISTS memories_hash_idx ON memories(hash)",
    """CREATE TABLE IF NOT EXISTS memory_ops (
        target TEXT PRIMARY KEY, created TEXT, reason TEXT)""",
    """CREATE TABLE IF NOT EXISTS memory_logs (
        path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
        consumed INTEGER NOT NULL, prefix_sha1 TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_vectors (
        hash TEXT NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL,
        PRIMARY KEY (hash, model))""",
    """CREATE TABLE IF NOT EXISTS memory_uses (
        id TEXT PRIMARY KEY, uses INTEGER NOT NULL DEFAULT 0, last_used TEXT)""",
    """CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT)""",
)
_SCHEMA_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
               "summary, body, tags, content='memories', content_rowid='rowid', "
               "tokenize='unicode61')")


def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in _SCHEMA:
        conn.execute(stmt)
    if store.fts5_available():
        try:
            conn.execute(_SCHEMA_FTS)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def _has_fts(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='memories_fts'").fetchone() is not None
    except sqlite3.Error:
        return False


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


@dataclass
class SyncStats:
    files: int = 0
    appended: int = 0
    rebuilt: bool = False
    embedded: int = 0
    malformed: int = 0


def _complete_prefix(data: bytes) -> int:
    """Bytes up to and including the last newline - a line still being
    written by a concurrent appender is left for the next sync."""
    nl = data.rfind(b"\n")
    return nl + 1 if nl >= 0 else 0


def _insert_items(conn: sqlite3.Connection, items, has_fts: bool) -> tuple[int, int]:
    added = malformed = 0
    for item in items:
        if item is None:
            malformed += 1
        elif isinstance(item, Op):
            conn.execute("INSERT OR IGNORE INTO memory_ops(target, created, reason) VALUES (?,?,?)",
                         (item.target, item.created, item.reason))
        else:
            cur = conn.execute(
                "INSERT OR IGNORE INTO memories(id, created, session, repo, scope, kind, summary, "
                "body, tags, source, supersedes, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.id, item.created, item.session, item.repo, item.scope, item.kind,
                 item.summary, item.body, " ".join(item.tags), item.source, item.supersedes,
                 item.hash))
            if cur.rowcount:
                added += 1
                if has_fts:
                    conn.execute(
                        "INSERT INTO memories_fts(rowid, summary, body, tags) VALUES (?,?,?,?)",
                        (cur.lastrowid, item.summary, item.body, " ".join(item.tags)))
    return added, malformed


def _parse_bytes(data: bytes):
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            yield parse_line(line)


def _refresh_active(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE memories SET active = CASE WHEN id IN (SELECT supersedes FROM memories "
        "WHERE supersedes IS NOT NULL AND supersedes != '') OR id IN (SELECT target FROM "
        "memory_ops) THEN 0 ELSE 1 END")


def _bump_generation(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO memory_meta(key, value) VALUES ('generation', '1') "
                 "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")


def generation(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT value FROM memory_meta WHERE key='generation'").fetchone()
    except sqlite3.Error:
        return "0"
    return str(row[0]) if row else "0"


def sync(root, conn: sqlite3.Connection, *, embed_vectors: bool = False) -> SyncStats:
    """Bring the index tables in *conn* current with *root*'s log.

    A log file whose (mtime, size) is unchanged is skipped without a read.
    One that only grew past the bytes already consumed (the prefix hash
    still matches) has just its new complete lines parsed. Anything else -
    a merge that inserted lines mid-file, a truncation, a removed file -
    rebuilds the record tables from the whole log. Counters and cached
    vectors are never touched by a rebuild. With *embed_vectors*, live
    records that have no vector for the current model get one (batch)."""
    root = Path(root)
    stats = SyncStats()
    has_fts = _has_fts(conn)
    files = log_files(root)
    stats.files = len(files)
    known = {row[0]: row[1:] for row in conn.execute(
        "SELECT path, mtime_ns, size, consumed, prefix_sha1 FROM memory_logs")}
    on_disk = {}
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        on_disk[str(p)] = (st.st_mtime_ns, st.st_size)

    rebuild = bool(set(known) - set(on_disk))
    tails: list[tuple[str, bytes, int]] = []  # (path, new bytes, new consumed)
    updates: dict[str, tuple[int, int, int, str]] = {}
    if not rebuild:
        for path, (mtime, size) in on_disk.items():
            prev = known.get(path)
            if prev is not None and prev[0] == mtime and prev[1] == size:
                continue
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            if prev is not None:
                consumed, sha = prev[2], prev[3]
                if len(data) < consumed or hashlib.sha1(data[:consumed]).hexdigest() != sha:
                    rebuild = True
                    break
            else:
                consumed = 0
            end = _complete_prefix(data)
            tails.append((path, data[consumed:end], end))
            updates[path] = (mtime, size, end, hashlib.sha1(data[:end]).hexdigest())

    if rebuild:
        stats.rebuilt = True
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM memory_ops")
        conn.execute("DELETE FROM memory_logs")
        if has_fts:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('delete-all')")
        for path, (mtime, size) in on_disk.items():
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            end = _complete_prefix(data)
            added, bad = _insert_items(conn, _parse_bytes(data[:end]), has_fts)
            stats.appended += added
            stats.malformed += bad
            conn.execute("INSERT OR REPLACE INTO memory_logs VALUES (?,?,?,?,?)",
                         (path, mtime, size, end, hashlib.sha1(data[:end]).hexdigest()))
    else:
        for path, chunk, _end in tails:
            added, bad = _insert_items(conn, _parse_bytes(chunk), has_fts)
            stats.appended += added
            stats.malformed += bad
        for path, row in updates.items():
            conn.execute("INSERT OR REPLACE INTO memory_logs VALUES (?,?,?,?,?)", (path, *row))

    if rebuild or tails:
        _refresh_active(conn)
        _bump_generation(conn)
    if embed_vectors:
        stats.embedded = _embed_missing(conn)
    conn.commit()
    return stats


def _embed_missing(conn: sqlite3.Connection) -> int:
    if not embed.available():
        return 0
    model = embed.model_name()
    rows = conn.execute(
        "SELECT m.hash, m.kind, m.tags, m.summary, m.body FROM memories m "
        "LEFT JOIN memory_vectors v ON v.hash = m.hash AND v.model = ? "
        "WHERE m.active = 1 AND v.hash IS NULL", (model,)).fetchall()
    if not rows:
        return 0
    seen, texts, hashes = set(), [], []
    for h, kind, tags, summary, body in rows:
        if h in seen:
            continue
        seen.add(h)
        hashes.append(h)
        texts.append(f"{kind} {tags}\n{summary}\n{body}")
    total = 0
    for i in range(0, len(texts), 512):
        mat = embed.embed_many(texts[i:i + 512])
        conn.executemany("INSERT OR REPLACE INTO memory_vectors(hash, model, vec) VALUES (?,?,?)",
                         [(h, model, row.tobytes()) for h, row in zip(hashes[i:i + 512], mat)])
        total += len(mat)
    _bump_generation(conn)
    return total


# --- opening the index for a read ---------------------------------------------

def _fresh(conn: sqlite3.Connection, root: Path) -> bool:
    """Whether *conn*'s memory tables already reflect every log file."""
    if not _has_table(conn, "memory_logs"):
        return False
    known = {row[0]: (row[1], row[2]) for row in conn.execute(
        "SELECT path, mtime_ns, size FROM memory_logs")}
    disk = {}
    for p in log_files(root):
        try:
            st = p.stat()
        except OSError:
            continue
        disk[str(p)] = (st.st_mtime_ns, st.st_size)
    return known == disk


def open_index(root, *, read_only: bool = False, embed_vectors: bool = False
               ) -> sqlite3.Connection | None:
    """A connection whose memory tables are current with *root*'s log, or
    ``None`` when the wiki has no memory log at all (the common case for a
    wiki that has never captured - no file is opened, nothing is created).

    Normal reads open (creating if needed) ``.shapa-index.db`` and
    :func:`sync` it. ``read_only=True`` never writes a byte in the wiki: a
    current index is opened ``mode=ro&immutable=1``; a missing or stale one
    is replaced by an in-memory copy built from the log, reusing whatever
    vectors the on-disk index already caches."""
    root = Path(root)
    if not has_log(root):
        return None
    if not read_only:
        conn = store.open_index(root)
        conn.row_factory = None
        ensure_schema(conn)
        sync(root, conn, embed_vectors=embed_vectors)
        return conn
    disk = store.db_path(root)
    if disk.is_file():
        try:
            ro = sqlite3.connect(f"file:{disk}?mode=ro&immutable=1", uri=True)
            if _fresh(ro, root) and (not embed_vectors or not _missing_vectors(ro)):
                return ro
            ro.close()
        except sqlite3.Error:
            pass
    mem = sqlite3.connect("file::memory:", uri=True)
    ensure_schema(mem)
    if disk.is_file():
        try:
            mem.execute("ATTACH DATABASE ? AS disk", (f"file:{disk}?mode=ro&immutable=1",))
            for table in ("memory_vectors", "memory_uses"):
                if mem.execute("SELECT 1 FROM disk.sqlite_master WHERE name=?", (table,)).fetchone():
                    mem.execute(f"INSERT OR IGNORE INTO main.{table} SELECT * FROM disk.{table}")
            mem.commit()
            mem.execute("DETACH DATABASE disk")
        except sqlite3.Error:
            pass
    sync(root, mem, embed_vectors=embed_vectors)
    return mem


def _missing_vectors(conn: sqlite3.Connection) -> bool:
    if not embed.available():
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM memories m LEFT JOIN memory_vectors v ON v.hash = m.hash AND "
            "v.model = ? WHERE m.active = 1 AND v.hash IS NULL LIMIT 1",
            (embed.model_name(),)).fetchone()
    except sqlite3.Error:
        return True
    return row is not None


# --- search ------------------------------------------------------------------

def search_mode() -> str:
    """``"fused"`` (model2vec vectors + BM25) when the ``[semantic]`` extra
    loads, else ``"bm25"`` - the mode every read path reports."""
    return "fused" if embed.available() else "bm25"


def lexical_backend() -> str:
    return "fts5-unicode61" if store.fts5_available() else "bm25-python"


def fts_query(query: str) -> str | None:
    toks = sorted(set(_FTS_TOKEN_RE.findall(query.lower())))
    return " OR ".join(f'"{t}"' for t in toks) if toks else None


def _row_record(row) -> Record:
    rid, created, session, repo, scope, kind, summary, body, tags, source, sup, h = row
    return Record(id=rid, created=created or "", session=session or "", repo=repo,
                  scope=scope or "repo", kind=kind or "fact", summary=summary, body=body,
                  tags=tags.split() if tags else [], source=source or "", supersedes=sup,
                  hash=h)


_COLS = "id, created, session, repo, scope, kind, summary, body, tags, source, supersedes, hash"


def active_records(conn: sqlite3.Connection, ids=None) -> dict[str, Record]:
    if ids is None:
        rows = conn.execute(f"SELECT {_COLS} FROM memories WHERE active = 1").fetchall()
    else:
        ids = list(ids)
        rows = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows += conn.execute(
                f"SELECT {_COLS} FROM memories WHERE active = 1 AND id IN "
                f"({','.join('?' * len(chunk))})", chunk).fetchall()
    return {r[0]: _row_record(r) for r in rows}


def lexical_scores(conn: sqlite3.Connection, query: str) -> dict[str, float]:
    """Raw BM25 per live record (higher is better): FTS5's ``bm25()`` when
    the index has the FTS table, else the hand-rolled :mod:`shapa.bm25`."""
    if _has_fts(conn):
        expr = fts_query(query)
        if expr is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT m.id, bm25(memories_fts) FROM memories_fts JOIN memories m "
                "ON m.rowid = memories_fts.rowid WHERE memories_fts MATCH ? AND m.active = 1",
                (expr,)).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {rid: -float(score) for rid, score in rows if -float(score) > 0}
    docs = {r.id: words(f"{r.summary} {r.body} {' '.join(r.tags)}")
            for r in active_records(conn).values()}
    return {rid: s for rid, s in bm25_scores(query, docs).items() if s > 0}


_MATRIX_CACHE: dict[tuple, tuple[list[str], object]] = {}


def cache_key(conn: sqlite3.Connection, db_file) -> tuple | None:
    """A key that changes whenever *conn*'s live vectors could have: the
    index file's identity plus its sync generation and row count. ``None``
    for an in-memory index (never cached - its content is per call)."""
    try:
        st = Path(db_file).stat()
        n, top = conn.execute("SELECT count(*), max(rowid) FROM memories").fetchone()
    except (OSError, sqlite3.Error, TypeError):
        return None
    return (str(db_file), st.st_ino, generation(conn), n, top)


def _matrix(conn: sqlite3.Connection, key):
    import numpy as np

    full_key = (key, embed.model_name()) if key is not None else None
    hit = _MATRIX_CACHE.get(full_key) if full_key is not None else None
    if hit is not None:
        return hit
    rows = conn.execute(
        "SELECT m.id, v.vec FROM memories m JOIN memory_vectors v ON v.hash = m.hash "
        "AND v.model = ? WHERE m.active = 1", (embed.model_name(),)).fetchall()
    ids = [r[0] for r in rows]
    mat = (np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
           if rows else np.zeros((0, 0), dtype=np.float32))
    if full_key is not None:
        if len(_MATRIX_CACHE) > 8:
            _MATRIX_CACHE.clear()
        _MATRIX_CACHE[full_key] = (ids, mat)
    return ids, mat


def vector_scores(conn: sqlite3.Connection, query_vec, key=None) -> dict[str, float]:
    """Cosine of *query_vec* (normalized) against every live record's
    vector. *key* (:func:`cache_key`) lets a long-lived process reuse the
    loaded matrix across calls; ``None`` never caches."""
    import numpy as np

    ids, mat = _matrix(conn, key)
    if not ids:
        return {}
    sims = mat @ np.asarray(query_vec, dtype=np.float32)
    return {rid: float(s) for rid, s in zip(ids, sims) if s > 0}


# --- usage counters (index only) ----------------------------------------------

def record_use(root, rid: str, now: datetime | None = None) -> int:
    """Bump *rid*'s use counter in *root*'s index. Never raises."""
    try:
        conn = store.open_index(root)
    except (OSError, sqlite3.Error):
        return 0
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO memory_uses(id, uses, last_used) VALUES (?, 1, ?) ON CONFLICT(id) "
            "DO UPDATE SET uses = uses + 1, last_used = excluded.last_used", (rid, now_iso(now)))
        conn.commit()
        row = conn.execute("SELECT uses FROM memory_uses WHERE id = ?", (rid,)).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def uses(conn: sqlite3.Connection) -> dict[str, tuple[int, str | None]]:
    try:
        return {r[0]: (int(r[1]), r[2]) for r in conn.execute(
            "SELECT id, uses, last_used FROM memory_uses")}
    except sqlite3.Error:
        return {}


def value_meta(rec: Record, use: tuple[int, str | None] = (0, None)) -> dict:
    """A frontmatter-shaped dict :func:`shapa.score.score_meta` can rank."""
    n, last = use
    return {"id": rec.id, "type": "memory", "created": rec.created,
            "consequence": KIND_CONSEQUENCE.get(rec.kind, 4),
            "locus": KIND_LOCUS.get(rec.kind, "output"), "uses": n, "last_used": last}


# --- one record ----------------------------------------------------------------

def get(root, rid: str) -> tuple[Record, str] | None:
    """``(record, status)`` for *rid* straight from the log (the source of
    truth - always current), status being active|superseded|archived."""
    view = read_log(root)
    rec = view.records.get(rid)
    return (rec, view.status(rid)) if rec else None


def stats(root) -> dict:
    """Counts for ``shapa status``: log files/bytes, records by status,
    malformed lines."""
    view = read_log(root)
    sup = view.superseded_by
    files = log_files(root)
    return {
        "log_files": len(files),
        "log_bytes": sum(p.stat().st_size for p in files if p.exists()),
        "records": len(view.records),
        "active": len(view.active()),
        "superseded": len([r for r in view.records if r in sup and r not in view.archived]),
        "archived": len([r for r in view.records if r in view.archived]),
        "malformed": view.malformed,
    }


def hot(root, min_uses: int) -> list[tuple[Record, int, str | None]]:
    """Live records used at least *min_uses* times, most used first -
    ``shapa maintain``'s promotion candidates."""
    conn = open_index(root)
    if conn is None:
        return []
    try:
        counts = uses(conn)
        live = active_records(conn)
    finally:
        conn.close()
    out = [(rec, counts[rid][0], counts[rid][1]) for rid, rec in live.items()
           if rid in counts and counts[rid][0] >= min_uses]
    out.sort(key=lambda t: (-t[1], t[0].id))
    return out
