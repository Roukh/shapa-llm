"""``shapa learn`` - a correction is made once, learned once, and serves every repo.

The operator's correction ("no, wrong, not like this", or, while work is claimed, a short remark such as
"move this header up") is an issue row (:func:`shapa.ledger.record_correction`). This module closes the loop:

1. **Repeat detection** (:func:`on_correction`): a new correction is compared with every earlier issue and
   rule of the repo wiki, with every rule of the global wiki, and with the unlearned corrections of the
   OTHER repos (a derived mirror in the global wiki's index), by stemmed-token Dice over a derived inverted
   index (top candidates only) and, with ``[semantic]``, by cosine over stored vectors. Close to one: it is a
   repeat (tags ``repeat`` and ``repeat-of:REF``, a ``related`` link in one wiki, a count on the earlier
   row); a repeat of a learned principle is a *violation* of it.
2. **Generalize without bloat** (:func:`apply`): the agent states the principle the correction implies and
   CHOOSES where it goes: ``--into R<n>`` (merge into that rule), ``--into skill:NAME`` or ``--new``. Lexical
   matching cannot decide meaning, so a call that chooses nothing is refused with the candidates (nearest
   rules and skills, every churned principle, principles serving the same fire). A principle is one rule row
   tagged ``principle``: the statement is its summary, the evidence (id, wiki key, date, text) its body.
   Global by default; ``--scope repo`` needs ``--why``. A principle for a skill is a rule carrying
   ``skill:NAME``; the existing lesson mechanism files a PENDING version of the skill from it, and only
   ``shapa skill activate`` makes it live. At most :data:`MAX_PRINCIPLES` live principles per wiki
   (:func:`merge` folds two).
3. **Directed**: every apply records what it serves (``northstar``, ``<wiki key>/F<n>``, or ``none``).
4. **Loop guards**: a correction is learned once; a principle revised :data:`REVISION_LIMIT` times and
   violated again is ``churn`` (no more revisions, and no ``--new`` beside it: it needs enforcement);
   corrections about the learning system itself are applied at most :data:`META_PER_WEEK` a week.

Scale: every similarity query goes through the derived index (``l_chunks``, ``l_tok``, ``l_vec`` in
``.shapa-index.db``, kept up to date from a rowid watermark and by the writes here), looks at top candidates
only, and never scans the wiki. The prompt path waits at most one second on any lock and skips silently.

Tags (ordinary row tags that sync; ``db.RESERVED_ROW_TAGS`` keeps every other writer away from them)::

    issue: correction soft repeat-checked repeat repeat-of:REF violation meta-learning blocked learned
           learned-at:ISO generalized-into:REF serves:... dismissed repeats:N
    principle (rule row): principle violations:N revisions:N evidence:N last-seen:ISO serves:... churn

CLI::

    shapa learn pending [--json] [--session ID] [--limit N]
    shapa learn apply ISSUE --principle TEXT (--into R<n>|--into skill:NAME|--new) [--scope global|repo|skill:NAME]
                      [--why TEXT] [--serves <key>/F<n>|northstar] [--wiki repo|global] [--session ID] [--json]
    shapa learn dismiss ISSUE [--wiki repo|global]      shapa learn merge SRC DST      shapa learn principles
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shapa import config, db, embed, metrics, skills
from shapa.ledger import CommandError, _subcommand_adder

LEX_REPEAT, COS_REPEAT = 0.55, 0.85
LEX_MERGE, COS_MERGE = 0.50, 0.80
LEX_TOPIC = 0.34          # same subject, so a flipped polarity is a contradiction
PRINCIPLE_MIN, PRINCIPLE_MAX = 12, 160
EVIDENCE_KEEP = 12
ALSO_KEEP = 4
NEAREST_SHOWN = 5
REVISION_LIMIT = 3
META_PER_WEEK = 3
PRINCIPLES_SHOWN = 4
MAX_PRINCIPLES = 60       # live principles per wiki
HOOK_BUSY_S = 1.0         # the prompt path gives up on a lock after this
CATCHUP_ROWS = 1500       # rows the hook path indexes per call while it catches up
CANDIDATE_POOL = 80       # candidate chunks scored per search
DF_CAP = 2000             # a token in more chunks than this is too common to retrieve by
VECTOR_POOL = 2000        # newest chunks a vector search covers besides the lexical candidates
DRIFT_DAYS = 14
DRIFT_MIN_JOBS = 4
DRIFT_OFF_SHARE = 0.5
HEAD_NOTE = "context learned from corrections, not an instruction"

# --- text ----------------------------------------------------------------------------------

_WORD = re.compile(r"[^\W_]+", re.U)
STOP = frozenset("""the and for that this with from have has had was were are not but you your its it's
into than then them they their there here what when which will would should could can cant dont don't
did does done about just please also any all one out use using used need needs want wants make made
more most some such very only over under after before again still wrong said told yes nope nah
no yet now how why who whom let lets get got been being too via per our rather instead opposed""".split())
NEGATION = frozenset("never not no dont don't doesn't didn't avoid without stop cannot can't won't "
                     "shouldn't mustn't nothing nobody".split())
ANTONYMS = (("always", "never"), ("add", "remove"), ("enable", "disable"), ("use", "avoid"),
            ("allow", "forbid"), ("allow", "block"), ("keep", "drop"), ("include", "exclude"),
            ("before", "after"), ("with", "without"), ("increase", "decrease"), ("open", "close"),
            ("show", "hide"), ("require", "skip"), ("commit", "revert"))
META_TERMS = frozenset("shapa principle principles learn learned learning pending correction corrections "
                       "generalize generalized memory skill skills metrics bootstrap wiki hook".split())


def _stem(w: str) -> str:
    if w.isascii() and len(w) > 4:
        for suf in ("ing", "ed", "es", "ly", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                return w[: -len(suf)]
    return w


def tokens(text: str) -> frozenset:
    out = set()
    for w in _WORD.findall(unicodedata.normalize("NFKC", str(text or "")).lower()):
        if w in STOP or w in NEGATION:
            continue
        if len(w) < (3 if w.isascii() else 2) and not w.isdigit():
            continue
        out.add(_stem(w))
    return frozenset(out)


def dice(a: frozenset, b: frozenset) -> float:
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def _negated(text: str) -> bool:
    s = re.sub(r"\b(?:rather than|instead of|as opposed to)\b", " not ", str(text).lower().replace("’", "'"))
    words = _WORD.findall(s)
    return sum(1 for w in words if w in NEGATION) % 2 == 1


def opposite(a: str, b: str) -> bool:
    """Whether two statements about the same subject say the opposite: one negated and
    not the other, or an antonym pair split between them."""
    if _negated(a) != _negated(b):
        return True
    wa, wb = set(_WORD.findall(str(a).lower())), set(_WORD.findall(str(b).lower()))
    return any((x in wa and y in wb and x not in wb and y not in wa)
               or (y in wa and x in wb and y not in wb and x not in wa) for x, y in ANTONYMS)


def issue_text(row: db.Row) -> str:
    m = re.search(r"^Operator:\s*(.*?)(?:\nAgent before:|\Z)", row.body or "", re.S | re.M)
    return " ".join(((m.group(1) if m else "") or row.summary or "").split())


META_STEMS = frozenset(_stem(w) for w in META_TERMS)


def is_meta(text: str) -> bool:
    """The correction's subject is the learning system itself: its content words are mostly the
    system's own vocabulary."""
    toks = tokens(text)
    meta = {t for t in toks if t in META_STEMS}
    return bool(meta) and len(toks - meta) <= 2 * len(meta) + 1


_ZERO = re.compile("[​-‏‪-‮⁠-⁤﻿\x00-\x1f\x7f-\x9f]")
_LABEL = re.compile(r"^(?:evidence|why|also)\s*:\s*", re.I)
#: A hint only: it adds a warning to an apply's result. Nothing depends on it (a lookalike letter defeats
#: any list); what keeps a line harmless is that it is flattened, masked, capped and printed as recalled
#: context, and that a skill version it files is only ever pending.
_HINT = re.compile(
    r"(ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier)|disregard|system\s+prompt|"
    r"you\s+are\s+now|new\s+instructions|jailbreak|exfiltrat|https?://|www\.|\bcurl\b|\bwget\b|base64|"
    r"\|\s*(?:sh|bash|zsh)\b|<\s*/?\s*(?:system|shapa|assistant|user|tool)|\bsudo\b|\brm\s+-rf\b)", re.I)


def inert(text: str) -> str:
    """*text* as one masked line that can open no tag, heading, fence or list in what it is quoted into."""
    from shapa.redact import redact

    t = _ZERO.sub(" ", unicodedata.normalize("NFKC", str(text or "")))
    t = " ".join(redact(t).split()).replace("`", "'").replace("<", "‹").replace(">", "›")
    return re.sub(r"\[[A-Za-z]+:?\d+\]", "(ref)", t)


def flatten(text: str, limit: int = PRINCIPLE_MAX) -> str:
    """:func:`inert`, markdown markers and labels stripped, cut to *limit*: what is printed, never raises."""
    t = inert(text).replace("---", "-")
    for _ in range(4):
        t = re.sub(r"^(?:[#>*\-+•]+|\(ref\)|\d+[.)])\s*", "", t.strip())
        t = _LABEL.sub("", t)
    t = t.strip()
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def clean_principle(text: str, *, limit: int = PRINCIPLE_MAX) -> str:
    t = flatten(text, limit=10 ** 6)
    if len(t) < PRINCIPLE_MIN or not re.search(r"[^\W\d_]{3}", t):
        raise db.LedgerError(f"a principle is a sentence of at least {PRINCIPLE_MIN} characters")
    if len(t) > limit:
        raise db.LedgerError(f"a principle is at most {limit} characters: state the general rule, "
                             "not the occasion")
    return t


class ChooseError(db.LedgerError):
    """``apply`` was given neither ``--into`` nor ``--new`` while candidates exist."""

    def __init__(self, candidates: list[dict], message: str = ""):
        candidates = [{k: c[k] for k in ("id", "wiki", "summary", "churn")} for c in candidates]
        self.candidates = candidates
        super().__init__(message or "choose where this principle goes: `--into R<n>` (or `skill:NAME`) to merge it, "
                         "or `--new` if none of these covers it:\n" + "\n".join(
                             f"  {c['wiki']}/{c['id']}{' [CHURN: needs enforcement]' if c['churn'] else ''} {c['summary']}"
                             for c in candidates))


# --- scopes ---------------------------------------------------------------------------------------

@dataclass
class Scope:
    label: str
    root: Path
    key: str

    def conn(self, busy_ms: int | None = None):
        c = db.connect(self.root, busy_ms=busy_ms)
        if c is None:
            raise CommandError(f"{self.root} has no database")
        return c


def scopes(cwd=None) -> list[Scope]:
    out, seen = [], set()
    try:
        roots = config.wiki_roots(cwd)
    except Exception:
        roots = []
    for kind in ("repo", "global"):
        for wr in roots:
            if wr.kind == kind and db.exists(wr.path):
                p = Path(wr.path).resolve()
                if p not in seen:
                    seen.add(p)
                    out.append(Scope(kind, Path(wr.path), metrics.repo_key(wr.path)))
    return out


def _ref(frm: Scope, target: Scope, rid: str) -> str:
    return rid if frm.label == target.label else f"{target.label}:{rid}"


def _resolve(ss: list[Scope], frm: Scope, ref: str) -> tuple[Scope | None, str]:
    if ref.startswith(("skill:", "x:")):
        return None, ref
    if ":" in ref:
        label, rid = ref.split(":", 1)
        return next((s for s in ss if s.label == label), None), rid
    return frm, ref


# --- tags -------------------------------------------------------------------------------------------

def tag_of(tags, prefix: str) -> str | None:
    for t in sorted(tags):
        if t.startswith(prefix):
            return t[len(prefix):]
    return None


def _tags(conn, rid: str) -> set:
    return {r[0] for r in conn.execute("SELECT tag FROM tags WHERE id = ?", (rid,))}


def _count(tags, prefix: str) -> int:
    v = tag_of(tags, prefix)
    return int(v) if v and v.isdigit() else 0


def _set_tags(conn, rid: str, mapping: dict, extra=()) -> None:
    """Replace every tag starting with a mapping key by ``key + value``, and add *extra*."""
    tg = _tags(conn, rid)
    drop = [t for t in tg if any(t.startswith(k) for k in mapping)]
    try:
        db.retag(conn, rid, add=[f"{k}{v}" for k, v in mapping.items()] + list(extra), drop=drop)
    except db.LedgerError:
        if f"{db.STAGE_PREFIX}0" in tg:
            return  # the operator's baseline: its tags change only from the local CLI, counters included
        raise


def _bump(conn, rid: str, prefix: str) -> int:
    n = _count(_tags(conn, rid), prefix) + 1
    _set_tags(conn, rid, {prefix: n})
    return n


# --- the derived similarity index ----------------------------------------------------------------------

_INDEX_SQL = (
    "CREATE TABLE IF NOT EXISTS l_chunks (cid INTEGER PRIMARY KEY AUTOINCREMENT, rid TEXT NOT NULL, "
    "kind TEXT NOT NULL, created TEXT NOT NULL DEFAULT '', src TEXT NOT NULL DEFAULT '', text TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS l_chunks_rid ON l_chunks(src, rid)",
    "CREATE TABLE IF NOT EXISTS l_tok (tok TEXT NOT NULL, cid INTEGER NOT NULL, PRIMARY KEY (tok, cid)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS l_vec (cid INTEGER PRIMARY KEY, vec BLOB NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p_local (id TEXT PRIMARY KEY, ts TEXT NOT NULL)",
)
CHUNK_CHARS = 400


def _chunks_of(row: db.Row) -> list[str]:
    if row.kind == "I":
        return [issue_text(row)[:CHUNK_CHARS]]
    chunks = [row.summary]
    for line in (row.body or "").splitlines():
        line = line.strip()
        if line == "Evidence:" or len(line) < 8:
            continue
        m = re.match(r"^- [A-Z]\d+ \S+ \d{4}-\d\d-\d\d: (.+)$", line)
        chunks.append((m.group(1) if m else re.sub(r"^(?:Also|Why):\s*", "", line))[:CHUNK_CHARS])
    return chunks


@dataclass
class Hit:
    rid: str
    kind: str                  # I | R | S
    src: str                   # '' for the index's own wiki, else the repo key of a mirrored issue
    wiki: str                  # repo | global | x (another repo's unlearned correction)
    chunk: str
    lex: float
    cos: float | None
    created: str = ""

    @property
    def score(self) -> float:
        return max(self.lex, self.cos or 0.0)

    @property
    def repeat(self) -> bool:
        return self.lex >= LEX_REPEAT or (self.cos is not None and self.cos >= COS_REPEAT)

    @property
    def merge(self) -> bool:
        return self.lex >= LEX_MERGE or (self.cos is not None and self.cos >= COS_MERGE)

    @property
    def n(self) -> int:
        m = re.search(r"\d+$", self.rid)
        return int(m.group()) if m else 0


class LearnIndex:
    """One wiki's similarity index inside its derived ``.shapa-index.db``. Rebuilt from the rows when
    missing; the rows stay the truth."""

    def __init__(self, root, timeout: float = HOOK_BUSY_S):
        self.root = Path(root)
        self.conn = metrics.open_local(root, timeout=timeout)
        for stmt in _INDEX_SQL:
            self.conn.execute(stmt)
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- writing
    def _put(self, rid: str, kind: str, created: str, src: str, chunks: list[str], fresh: bool = False) -> None:
        cur = self.conn.cursor()
        if not fresh:
            self._drop(rid, src)
        for text in chunks:
            toks = tokens(text)
            if not toks:
                continue
            cur.execute("INSERT INTO l_chunks(rid, kind, created, src, text) VALUES (?,?,?,?,?)",
                        (rid, kind, created, src, text))
            cid = cur.lastrowid
            cur.executemany("INSERT OR IGNORE INTO l_tok(tok, cid) VALUES (?, ?)", [(t, cid) for t in toks])

    def _drop(self, rid: str, src: str) -> None:
        cids = [r[0] for r in self.conn.execute("SELECT cid FROM l_chunks WHERE src = ? AND rid = ?", (src, rid))]
        if cids:
            marks = ",".join("?" * len(cids))
            self.conn.execute(f"DELETE FROM l_tok WHERE cid IN ({marks})", cids)
            self.conn.execute(f"DELETE FROM l_vec WHERE cid IN ({marks})", cids)
            self.conn.execute(f"DELETE FROM l_chunks WHERE cid IN ({marks})", cids)

    def upsert_row(self, row: db.Row) -> None:
        if row.kind in ("I", "R"):
            self._put(row.id, row.kind, row.created, "", _chunks_of(row))
            self.conn.commit()

    def drop(self, rid: str, src: str = "") -> None:
        self._drop(rid, src)
        self.conn.commit()

    def put_mirror(self, key: str, rid: str, created: str, text: str) -> None:
        self._put(rid, "I", created, key, [text[:CHUNK_CHARS]])
        self.conn.commit()

    def _meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM m_meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def _set_meta(self, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO m_meta(key, value) VALUES (?, ?)", (key, str(value)))

    def sync(self, dbconn, max_rows: int | None = None) -> bool:
        """Index the rows written since the watermark (a rowid: new rows always have a higher one).
        *max_rows* bounds a catch-up on the prompt path; ``True`` when caught up."""
        top = dbconn.execute("SELECT COALESCE(MAX(rowid), 0) FROM mri").fetchone()[0]
        wm = int(self._meta("l_wm") or 0)
        if wm > top:   # the file was swapped or renumbered: start over
            self.conn.execute("DELETE FROM l_chunks WHERE src = ''")
            self.conn.execute("DELETE FROM l_tok WHERE cid NOT IN (SELECT cid FROM l_chunks)")
            self.conn.execute("DELETE FROM l_vec WHERE cid NOT IN (SELECT cid FROM l_chunks)")
            wm = 0
        fresh = wm == 0 and self.conn.execute("SELECT 1 FROM l_chunks WHERE src = '' LIMIT 1").fetchone() is None
        sql = ("SELECT rowid, id, kind, created, summary, body FROM mri WHERE rowid > ? AND kind IN ('I','R') "
               "ORDER BY rowid" + (" LIMIT ?" if max_rows else ""))
        last = wm
        n = 0
        for rowid, rid, kind, created, summary, body in dbconn.execute(sql, (wm, max_rows) if max_rows else (wm,)):
            row = db.Row(rid, kind, None, summary, body, "", "", "", created, created, [])
            self._put(rid, kind, created, "", _chunks_of(row), fresh=fresh)
            last = rowid
            n += 1
        if not max_rows or n < max_rows:
            last = max(last, top)
        self._set_meta("l_wm", last)
        self.conn.commit()
        return last >= top

    # -- reading
    def _rare_tokens(self, qtoks, limit: int = 8) -> list[str]:
        scored = []
        for t in qtoks:
            df = self.conn.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM l_tok WHERE tok = ? LIMIT {DF_CAP})", (t,)).fetchone()[0]
            if df:
                scored.append((df, t))
        scored.sort()
        rare = [t for df, t in scored if df < DF_CAP][:limit]
        return rare or [t for _, t in scored[:2]]

    def search(self, text: str, k: int = NEAREST_SHOWN, *, kinds=None, skip=None, exclude_src=(),
               embedder=None, wiki: str = "repo") -> list[Hit]:
        """The *k* chunks nearest *text* among a handful of lexical candidates (rare tokens, scored by
        overlap), plus, with *embedder*, the vector neighbours among the newest chunks. *skip(hit)* drops
        one (the row being filed); each entry counts once, by its best chunk."""
        qt = tokens(text)
        cands: dict[int, tuple] = {}
        rare = self._rare_tokens(qt)
        if rare:
            marks = ",".join("?" * len(rare))
            cids = [r[0] for r in self.conn.execute(
                f"SELECT cid, COUNT(*) n FROM l_tok WHERE tok IN ({marks}) GROUP BY cid "
                f"ORDER BY n DESC, cid DESC LIMIT {CANDIDATE_POOL}", rare)]
            if cids:
                m2 = ",".join("?" * len(cids))
                for cid, rid, kind, created, src, chunk in self.conn.execute(
                        f"SELECT cid, rid, kind, created, src, text FROM l_chunks WHERE cid IN ({m2})", cids):
                    cands[cid] = (rid, kind, created, src, chunk)
        cos: dict[int, float] = {}
        if embedder is not None:
            cos = self._cosines(text, embedder, cands)
            missing = [c for c in cos if c not in cands]
            if missing:
                m3 = ",".join("?" * len(missing))
                for cid, rid, kind, created, src, chunk in self.conn.execute(
                        f"SELECT cid, rid, kind, created, src, text FROM l_chunks WHERE cid IN ({m3})", missing):
                    cands[cid] = (rid, kind, created, src, chunk)
        best: dict[tuple, Hit] = {}
        for cid, (rid, kind, created, src, chunk) in cands.items():
            if (kinds and kind not in kinds) or src in exclude_src:
                continue
            h = Hit(rid, kind, src, "x" if src else wiki, chunk, dice(qt, tokens(chunk)), cos.get(cid), created)
            if skip is not None and skip(h):
                continue
            key = (src, rid)
            if key not in best or h.score > best[key].score:
                best[key] = h
        return sorted(best.values(), key=lambda h: (-h.score, h.rid))[:k]

    def _cosines(self, text: str, embedder, cands: dict) -> dict[int, float]:
        pool = set(cands) | {r[0] for r in self.conn.execute(
            f"SELECT cid FROM l_chunks ORDER BY cid DESC LIMIT {VECTOR_POOL}")}
        if not pool:
            return {}
        have = {r[0]: r[1] for r in self.conn.execute(
            f"SELECT cid, vec FROM l_vec WHERE cid IN ({','.join('?' * len(pool))})", list(pool))}
        todo = [c for c in pool if c not in have]
        if todo:
            texts = {r[0]: r[1] for r in self.conn.execute(
                f"SELECT cid, text FROM l_chunks WHERE cid IN ({','.join('?' * len(todo))})", todo)}
            order = list(texts)
            mat = embedder([texts[c] for c in order])
            from array import array
            for cid, vec in zip(order, mat):
                blob = array("f", [float(x) for x in vec]).tobytes()
                self.conn.execute("INSERT OR REPLACE INTO l_vec(cid, vec) VALUES (?, ?)", (cid, blob))
                have[cid] = blob
            self.conn.commit()
        from array import array
        q = [float(x) for x in embedder([text])[0]]
        out = {}
        for cid, blob in have.items():
            v = array("f")
            v.frombytes(blob)
            out[cid] = sum(a * b for a, b in zip(v, q))
        return out


def default_embedder():
    return embed.embed_many if embed.available() else None


def _embedder(arg):
    return default_embedder() if arg == "auto" else arg


# --- per-device state -------------------------------------------------------------------------------------

def served_total(root) -> int:
    try:
        conn = metrics.open_local(root, create=False)
        if conn is None:
            return 0
        try:
            return int(conn.execute("SELECT COALESCE(SUM(n), 0) FROM p_served").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return 0


def mark_served(root, ids, now: datetime | None = None) -> None:
    try:
        conn = metrics.open_local(root)
        try:
            ts = db.now_iso(now)
            for rid in ids:
                conn.execute("INSERT INTO p_served(id, n, last) VALUES (?,1,?) ON CONFLICT(id) DO UPDATE "
                             "SET n = n + 1, last = excluded.last", (rid, ts))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _mark_local(root, rid: str, now: datetime | None = None) -> None:
    """This device wrote (or merged into) principle *rid* itself: only these earn a rank boost from their tags."""
    try:
        with LearnIndex(root) as ix:
            ix.conn.execute("INSERT OR REPLACE INTO p_local(id, ts) VALUES (?, ?)", (rid, db.now_iso(now)))
            ix.conn.commit()
    except Exception:
        pass


def local_principles(root) -> set[str]:
    try:
        conn = metrics.open_local(root, create=False)
        if conn is None:
            return set()
        try:
            return {r[0] for r in conn.execute("SELECT id FROM p_local")}
        finally:
            conn.close()
    except Exception:
        return set()


# --- 1. repeat detection ------------------------------------------------------------------------------

def _violate(target_scope: Scope, pid: str, now: datetime, busy_ms: int | None = None) -> tuple[int, bool]:
    """Count a violation on principle *pid*: ``(violations, churn)``. A principle revised
    :data:`REVISION_LIMIT` times that is violated again is marked ``churn``."""
    conn = target_scope.conn(busy_ms)
    try:
        tg = _tags(conn, pid)
        n = _count(tg, "violations:") + 1
        churn = _count(tg, "revisions:") >= REVISION_LIMIT
        _set_tags(conn, pid, {"violations:": n, "last-seen:": db.now_iso(now)}, extra=["churn"] if churn else [])
        return n, churn
    finally:
        conn.close()


def _target_of(ss: list[Scope], scope: Scope, rid: str, kind: str, tg: set):
    """For an earlier row a new correction repeats: ``((scope, id) taking the count, (scope, id) of the
    principle it violates or None)``."""
    if kind == "R":
        return (scope, rid), ((scope, rid) if "principle" in tg else None)
    gi = tag_of(tg, "generalized-into:")
    if gi and "learned" in tg and not gi.startswith("skill:"):
        psc, pid = _resolve(ss, scope, gi)
        if psc is not None:
            return (scope, rid), (psc, pid)
    ro = tag_of(tg, "repeat-of:")
    if ro:
        rsc, root_id = _resolve(ss, scope, ro)
        if rsc is not None:
            return (rsc, root_id), None
    return (scope, rid), None


def on_correction(root, rid: str, created: bool, *, session: str = "", hook: bool = True,
                  now: datetime | None = None, embedder="auto") -> dict:
    """Run after the hook files issue *rid* in wiki *root*: detect a repeat, tag, link, count, mark. Never
    waits more than a second on a lock: on a lock it returns what it has and the row stays unchecked."""
    now = now or datetime.now(timezone.utc)
    home_root = Path(root)
    ss = scopes(home_root)
    home = next((s for s in ss if s.root.resolve() == home_root.resolve()), None)
    if home is None:
        home = Scope("global" if metrics.repo_key(home_root) == "global" else "repo", home_root,
                     metrics.repo_key(home_root))
        ss = [home] + [s for s in ss if s.root.resolve() != home_root.resolve()]
    gscope = next((s for s in ss if s.label == "global" and s is not home), None) if home.label == "repo" else None
    info = {"repeat": False, "of": None, "count": 0, "violation": None, "churn": False, "meta": False}
    emb = _embedder(embedder)
    busy = int(HOOK_BUSY_S * 1000)
    conn = home.conn(busy)
    idx = gidx = None
    try:
        row = db.get_row(conn, rid)
        if row is None:
            return info
        text = issue_text(row)
        tags = _tags(conn, rid)
        if not created:                                    # the same words again: one row, a higher count
            info.update(repeat=True, of=rid, count=_bump(conn, rid, "repeats:"))
            gi = tag_of(tags, "generalized-into:")
            if gi and not gi.startswith("skill:"):
                psc, pid = _resolve(ss, home, gi)
                if psc is not None:
                    info["violation"] = gi
                    _, info["churn"] = _violate(psc, pid, now, busy)
            metrics.record(home_root, "repeat", session=session, ref=rid, data={"dedup": 1}, now=now)
            return info
        if "repeat-checked" in tags:
            return info
        add = ["repeat-checked"]
        if is_meta(text):
            add.append("meta-learning")
            info["meta"] = True
        idx = LearnIndex(home_root)
        idx.sync(conn, max_rows=CATCHUP_ROWS)
        idx.upsert_row(row)
        n_self = int(rid[1:])

        def skip_own(h: Hit) -> bool:
            return h.rid == rid or (h.kind == "I" and (h.created, h.n) >= (row.created, n_self))

        hits = idx.search(text, 6, kinds=("I", "R"), skip=skip_own, embedder=emb, wiki=home.label)
        if gscope is not None:
            try:
                gidx = LearnIndex(gscope.root)
                # the mirror holds only corrections already filed, whatever clock stamped them: no
                # timestamp test for it, so two repos filing in the same second still see each other
                hits += gidx.search(text, 6, kinds=("I", "R"), exclude_src={home.key}, embedder=emb, wiki="global",
                                    skip=lambda h: h.kind == "I" and not h.src and h.created >= row.created)
            except sqlite3.Error:
                gidx = None
        hit = next((h for h in sorted(hits, key=lambda h: (-h.score, h.rid)) if h.repeat), None)
        if hit is not None:
            if hit.src:                                     # another repo's unlearned correction
                ref = f"x:{hit.src}/{hit.rid}"
                add += ["repeat", f"repeat-of:{ref}"]
                info.update(repeat=True, of=ref, count=1)
                db.retag(conn, rid, add=add)
            else:
                tscope = home if hit.wiki == home.label else next(s for s in ss if s.label == hit.wiki)
                tconn = tscope.conn(busy) if tscope is not home else conn
                try:
                    (cs, cid), pr = _target_of(ss, tscope, hit.rid, hit.kind, _tags(tconn, hit.rid))
                finally:
                    if tconn is not conn:
                        tconn.close()
                ref = _ref(home, cs, cid)
                add += ["repeat", f"repeat-of:{ref}"]
                info.update(repeat=True, of=ref)
                if pr is None or pr != (cs, cid):
                    c2 = cs.conn(busy)
                    try:
                        info["count"] = _bump(c2, cid, "repeats:")
                    finally:
                        c2.close()
                if pr is not None:
                    psc, pid = pr
                    info["violation"] = _ref(home, psc, pid)
                    add.append("violation")
                    info["count"], info["churn"] = _violate(psc, pid, now, busy)
                    if info["churn"]:
                        add.append("blocked")
                db.retag(conn, rid, add=add)
                if cs.label == home.label:
                    db.link(conn, rid, cid, "related")
            metrics.record(home_root, "repeat", session=session, ref=rid, now=now)
        else:
            db.retag(conn, rid, add=add)
        if gidx is not None:
            gidx.put_mirror(home.key, rid, row.created, text)    # visible to the other repos' sessions at once
        metrics.record(home_root, "correction", session=session, ref=rid, now=now)
        return info
    except sqlite3.OperationalError:
        return info
    finally:
        for x in (idx, gidx):
            if x is not None:
                x.close()
        conn.close()


# --- pending ------------------------------------------------------------------------------------------

#: A correction an agent can still act on: not learned, dismissed or blocked, and not folded under an
#: earlier still-pending correction of the same wiki (a repeat is listed once, with its root).
_FOLDED = ("NOT EXISTS (SELECT 1 FROM tags ro JOIN tags rc ON rc.id = substr(ro.tag, 11) AND rc.tag = 'correction' "
           "WHERE ro.id = c.id AND ro.tag LIKE 'repeat-of:I%' AND instr(substr(ro.tag, 11), ':') = 0 "
           "AND NOT EXISTS (SELECT 1 FROM tags y WHERE y.id = rc.id AND y.tag IN ('learned','dismissed')))")
#: A correction listed by ``learn pending``: not learned or dismissed, and not folded under an earlier
#: still-pending correction of the same wiki (a repeat is listed once, with its root).
_LISTED = ("NOT EXISTS (SELECT 1 FROM tags x WHERE x.id = c.id AND x.tag IN ('learned','dismissed')) AND " + _FOLDED)
#: One an agent can still act on: listed, and not blocked (a churned principle needs the operator).
_OPEN = ("NOT EXISTS (SELECT 1 FROM tags x WHERE x.id = c.id AND x.tag IN ('learned','dismissed','blocked')) AND " + _FOLDED)


def _folded_open(conn) -> int:
    """Pending corrections folded under an earlier pending one of the same wiki (a repeat is listed once)."""
    return conn.execute(
        "SELECT COUNT(DISTINCT ro.id) FROM tags ro "
        "JOIN tags cc ON cc.id = ro.id AND cc.tag = 'correction' "
        "JOIN tags rc ON rc.id = substr(ro.tag, 11) AND rc.tag = 'correction' "
        "WHERE ro.tag >= 'repeat-of:I' AND ro.tag < 'repeat-of:J' AND instr(substr(ro.tag, 11), ':') = 0 "
        "AND NOT EXISTS (SELECT 1 FROM tags x WHERE x.id = ro.id AND x.tag IN ('learned','dismissed','blocked')) "
        "AND NOT EXISTS (SELECT 1 FROM tags y WHERE y.id = rc.id AND y.tag IN ('learned','dismissed'))").fetchone()[0]


def awaiting_count(conn, now: datetime | None = None) -> int:
    """Corrections an agent can still act on: unlearned, not dismissed, not blocked, not folded under an
    earlier pending one, and not a learning-system correction past this week's cap. Indexed counts only
    (no similarity pass, no row read); the same number ``learn pending`` reports."""
    now = now or datetime.now(timezone.utc)
    try:
        total = conn.execute("SELECT COUNT(*) FROM tags WHERE tag = 'correction'").fetchone()[0]
        if not total:
            return 0
        total -= conn.execute(
            "SELECT COUNT(DISTINCT a.id) FROM tags a JOIN tags b ON b.id = a.id AND "
            "b.tag IN ('learned','dismissed','blocked') WHERE a.tag = 'correction'").fetchone()[0]
        total -= _folded_open(conn)
        if total and meta_applied_week(conn, now) >= META_PER_WEEK:
            meta = [r[0] for r in conn.execute("SELECT id FROM tags WHERE tag = 'meta-learning'")]
            total -= sum(1 for rid in meta if conn.execute(
                f"SELECT 1 FROM tags c WHERE c.id = ? AND c.tag = 'correction' AND {_OPEN}", (rid,)).fetchone())
        return max(0, total)
    except Exception:
        return 0


def meta_applied_week(conn, now: datetime) -> int:
    cut = "learned-at:" + db.now_iso(now - timedelta(days=7))
    return conn.execute(
        "SELECT COUNT(*) FROM tags l WHERE l.tag >= ? AND l.tag < 'learned-at:~' AND EXISTS "
        "(SELECT 1 FROM tags m WHERE m.id = l.id AND m.tag = 'meta-learning')", (cut,)).fetchone()[0]


def awaiting_activation(ss: list[Scope] | None = None, conn=None) -> list[dict]:
    """Pending skill versions that a learned principle filed: ``[{skill, version, lesson}]``. Only
    ``shapa skill activate`` makes one live."""
    out = []
    conns = [(None, conn)] if conn is not None else [(s, s.conn()) for s in (ss or [])]
    try:
        for _, c in conns:
            for doc_id, source in c.execute("SELECT id, source FROM docs WHERE source LIKE 'lesson:R%'").fetchall():
                d = db.get_doc(c, doc_id)
                v = skills._from_doc(d) if d else None
                if v is None or v.status != "pending":
                    continue
                row = db.get_row(c, source[len("lesson:"):])
                if row is None or "principle" not in _tags(c, row.id):
                    continue
                out.append({"skill": v.name, "version": v.version, "lesson": flatten(row.summary)})
    finally:
        if conn is None:
            for _, c in conns:
                c.close()
    return sorted(out, key=lambda x: (x["skill"], x["version"]))


def _unlearned_ids(conn, offset: int, n: int) -> list[str]:
    return [r[0] for r in conn.execute(
        f"SELECT c.id FROM tags c JOIN mri m ON m.id = c.id WHERE c.tag = 'correction' AND {_LISTED} "
        "ORDER BY m.created, m.rowid LIMIT ? OFFSET ?", (n, offset))]


def _skill_lines(ss: list[Scope]) -> list[tuple[str, str, str]]:
    out = []
    for s in ss:
        c = s.conn()
        try:
            for name, vs in skills._all(c).items():
                cur = next((v for v in reversed(vs) if v.status == "active"), vs[-1])
                for l in cur.body.splitlines():
                    l = l.strip(" -*")
                    if len(l) >= 12 and not l.startswith("#"):
                        out.append((name, s.label, l))
        finally:
            c.close()
    return out


def _brief_hit(h: Hit, conn_of) -> dict:
    kind = {"I": "issue", "R": "rule"}[h.kind]
    if h.kind == "R" and "principle" in _tags(conn_of(h.wiki), h.rid):
        kind = "principle"
    ref = h.rid if h.wiki in ("repo",) else (f"x:{h.src}/{h.rid}" if h.src else f"{h.wiki}:{h.rid}")
    return {"ref": ref, "wiki": h.wiki, "kind": kind, "score": round(h.score, 3), "lex": round(h.lex, 3),
            "cos": None if h.cos is None else round(h.cos, 3), "merge": h.merge, "repeat": h.repeat,
            "text": h.chunk[:160]}


def churned(ss: list[Scope]) -> list[dict]:
    out = []
    for s in ss:
        conn = s.conn()
        try:
            for (rid,) in conn.execute("SELECT id FROM tags WHERE tag = 'churn'").fetchall():
                r = db.get_row(conn, rid)
                if r is None or r.kind != "R":
                    continue
                tg = _tags(conn, rid)
                out.append({"id": rid, "wiki": s.label, "summary": flatten(r.summary),
                            "violations": _count(tg, "violations:"), "revisions": _count(tg, "revisions:"),
                            "note": "needs enforcement, not rewording"})
        finally:
            conn.close()
    return out


def pending(cwd=None, *, session: str = "", limit: int = 20, now: datetime | None = None,
            embedder="auto") -> dict:
    """The corrections not yet generalized, at most *limit* of them (the limit applies before any similarity
    search), each with its nearest rules, skills and issues. ``count`` is :func:`awaiting_count` summed over
    the wikis in scope: the number session start prints."""
    now = now or datetime.now(timezone.utc)
    emb = _embedder(embedder)
    ss = scopes(cwd)
    items: list[dict] = []
    total = 0
    skill_lines = None
    for home in ss:
        conn = home.conn()
        try:
            total += awaiting_count(conn, now)
            if len(items) >= limit:
                continue
            capped = meta_applied_week(conn, now) >= META_PER_WEEK
            with LearnIndex(home.root, timeout=5.0) as idx:
                idx.sync(conn)
                gidx = None
                if home.label == "repo":
                    g = next((s for s in ss if s.label == "global"), None)
                    if g is not None:
                        gidx = LearnIndex(g.root, timeout=5.0)
                conns = {"repo": conn}
                gconn = None

                def conn_of(label):
                    nonlocal gconn
                    if label == home.label:
                        return conn
                    if gconn is None:
                        gconn = next(s for s in ss if s.label == label).conn()
                    return gconn

                offset = 0
                try:
                    while len(items) < limit:
                        page = _unlearned_ids(conn, offset, 100)
                        if not page:
                            break
                        offset += len(page)
                        for rid in page:
                            if len(items) >= limit:
                                break
                            r = db.get_row(conn, rid)
                            tg = _tags(conn, rid)
                            r.tags = sorted(tg)
                            ro = tag_of(tg, "repeat-of:")
                            if ro and ":" not in ro:   # folded under its (still pending) root
                                rt = _tags(conn, ro)
                                if rt and "correction" in rt and not rt & {"learned", "dismissed"}:
                                    continue
                            if session and not (r.session or "").startswith(session[:8]):
                                continue
                            text = issue_text(r)
                            hits = idx.search(text, NEAREST_SHOWN, skip=lambda h, i=rid: h.rid == i and not h.src,
                                              embedder=emb, wiki=home.label)
                            if gidx is not None:
                                hits += gidx.search(text, 3, exclude_src={home.key}, embedder=emb, wiki="global")
                            hits = sorted(hits, key=lambda h: -h.score)[:NEAREST_SHOWN]
                            near = [_brief_hit(h, conn_of) for h in hits]
                            if skill_lines is None:
                                skill_lines = _skill_lines(ss)
                            qt = tokens(text)
                            best = {}
                            for name, label, line in skill_lines:
                                sc = dice(qt, tokens(line))
                                if sc >= 0.2 and sc > best.get(name, (0,))[0]:
                                    best[name] = (sc, label, line)
                            for name, (sc, label, line) in sorted(best.items(), key=lambda kv: -kv[1][0])[:2]:
                                near.append({"ref": f"skill:{name}", "wiki": label, "kind": "skill",
                                             "score": round(sc, 3), "lex": round(sc, 3), "cos": None,
                                             "merge": sc >= LEX_MERGE, "repeat": False, "text": line[:160]})
                            also = [x[0] for x in conn.execute(
                                "SELECT t.id FROM tags t WHERE t.tag = ? AND NOT EXISTS (SELECT 1 FROM tags l WHERE "
                                "l.id = t.id AND l.tag IN ('learned','dismissed'))", (f"repeat-of:{rid}",))]
                            item = {"id": rid, "wiki": home.label, "key": home.key, "summary": flatten(r.summary),
                                    "text": flatten(text, 300), "session": r.session, "created": r.created,
                                    "repeat": "repeat" in tg, "repeat_of": ro, "repeat_count": _count(tg, "repeats:"),
                                    "violation": "violation" in tg, "also": sorted(also),
                                    "meta": "meta-learning" in tg, "capped": "meta-learning" in tg and capped,
                                    "blocked": "blocked" in tg, "soft": "soft" in tg, "nearest": near}
                            if item["blocked"]:
                                item["note"] = "needs enforcement, not rewording"
                            items.append(item)
                finally:
                    if gidx is not None:
                        gidx.close()
                    if gconn is not None:
                        gconn.close()
        finally:
            conn.close()
    items.sort(key=lambda i: (i["created"], i["id"]))
    return {"count": total, "pending": items[:limit], "churn": churned(ss),
            "awaiting_activation": awaiting_activation(ss), "mode": "fused" if emb is not None else "lexical"}


# --- 2. apply -------------------------------------------------------------------------------------------

def _split_body(body: str) -> tuple[list[str], list[str]]:
    head, ev, in_ev = [], [], False
    for line in (body or "").splitlines():
        if line.strip() == "Evidence:":
            in_ev = True
        elif in_ev:
            if line.strip():
                ev.append(line.rstrip())
        else:
            head.append(line.rstrip())
    while head and not head[-1].strip():
        head.pop()
    return head, ev


def _join_body(head: list[str], ev: list[str]) -> str:
    return "\n".join([*head, *(["Evidence:"] if ev else []), *ev]).strip()


def statements_of(row: db.Row) -> list[str]:
    """What a rule asserts, to test a new principle against: its summary. Not the corrections quoted as
    evidence (the operator's reactions), and not the ``Also:`` wordings (alternative phrasings of the
    summary, whose looser polarity words would raise false contradictions)."""
    return [row.summary]


def resolve_serves(conn, explicit: str | None, key: str, label: str) -> str:
    """The goal a learn serves: ``northstar``, ``<wiki key>/F<n>`` (an OPEN feature of this repo wiki,
    never a bare id: a global principle outlives the repo it was learned in), or ``none``."""
    from shapa import focus

    def feature(fid: str) -> str:
        it = db.get_item(conn, fid) if re.fullmatch(r"F[1-9]\d*", fid) else None
        if it is None or it.kind != "F":
            raise db.LedgerError(f"--serves names a feature of this repo wiki (F<n>), got {fid!r}")
        if it.status == "closed":
            raise db.LedgerError(f"{it.id} is closed: a closed feature is served by nothing")
        return f"{key}/{it.id}"

    if explicit:
        e = explicit.strip()
        if e.lower() == "northstar":
            return "northstar"
        if label != "repo":
            raise db.LedgerError("only a repo wiki's feature can be served: use --serves northstar or none")
        return feature(e.split("/", 1)[1] if "/" in e and e.split("/", 1)[0] == key else e)
    if label != "repo":
        return "none"
    fire_ids = {f.feature for f in focus.load(conn).fires if f and f.feature}
    for item in db.items(conn, status="claimed"):
        cur = item
        while cur is not None and cur.kind != "F":
            cur = db.get_item(conn, cur.parent) if cur.parent else None
        if cur is not None and cur.status != "closed" and (cur.id in fire_ids or "fire" in _tags(conn, cur.id)):
            return f"{key}/{cur.id}"
    return "none"


def live_principles(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT t.id FROM tags t JOIN mri m ON m.id = t.id "
                                       "WHERE t.tag = 'principle' AND m.kind = 'R'")]


#: How near a stated principle must be to a churned one for the churn to bind it: a real share of its
#: words (Dice), or a close vector. The listed candidates are the nearest five whatever their score; only
#: a near churned one blocks `--new`, so an unrelated correction is never held up by someone else's churn.
CHURN_NEAR = 0.2


def _candidates(ss: list[Scope], target: Scope, principle: str, serve: str, embedder, skill_name=None) -> list[dict]:
    """What a principle might already be: the nearest rules of the target wiki, the nearest skills, and the
    principles that serve the same goal. A churned one among the nearest is marked ``near`` (internal)."""
    out: dict[str, dict] = {}
    conn = target.conn()
    try:
        with LearnIndex(target.root, timeout=5.0) as idx:
            idx.sync(conn)
            for h in idx.search(principle, 5, kinds=("R",), embedder=embedder, wiki=target.label):
                r = db.get_row(conn, h.rid)
                if r is not None:
                    out[h.rid] = {"id": h.rid, "wiki": target.label, "summary": flatten(r.summary),
                                  "churn": "churn" in _tags(conn, h.rid),
                                  "near": h.lex >= CHURN_NEAR or (h.cos or 0.0) >= COS_MERGE}
        if serve != "none":
            for (rid,) in conn.execute("SELECT id FROM tags WHERE tag = ?", (f"serves:{serve}",)).fetchall():
                tg = _tags(conn, rid)
                r = db.get_row(conn, rid) if "principle" in tg else None
                if r is not None:
                    out.setdefault(rid, {"id": rid, "wiki": target.label, "summary": flatten(r.summary),
                                         "churn": "churn" in tg, "near": False})
    finally:
        conn.close()
    qt = tokens(principle)
    seen = set()
    for name, label, line in _skill_lines(ss):
        if name not in seen and dice(qt, tokens(line)) >= 0.25:
            seen.add(name)
            out[f"skill:{name}"] = {"id": f"skill:{name}", "wiki": label, "summary": flatten(line),
                                    "churn": False, "near": False}
    return sorted(out.values(), key=lambda c: (not c["churn"], c["id"]))


def _find_skill(ss: list[Scope], name: str) -> Scope:
    for s in ss:
        c = s.conn()
        try:
            if skills.versions(c, name):
                return s
        finally:
            c.close()
    raise db.LedgerError(f"no skill {name!r} in scope: start it with `shapa skill add`")


def _parse_into(into: str | None, ss: list[Scope]) -> tuple[str | None, str | None, str | None]:
    """``(rule id, wiki label, skill name)`` from ``R2``, ``global/R2``, ``repo/R2`` or ``skill:NAME``."""
    if not into:
        return None, None, None
    into = into.strip()
    if into.startswith("skill:"):
        return None, None, into[6:]
    label = None
    if "/" in into:
        label, into = into.split("/", 1)
        if label not in ("global", "repo"):
            raise db.LedgerError("--into is R<n>, global/R<n>, repo/R<n> or skill:NAME")
    if not re.fullmatch(r"R[1-9]\d*", into):
        raise db.LedgerError("--into is R<n>, global/R<n>, repo/R<n> or skill:NAME")
    return into, label, None


def apply(cwd, issue_id: str, principle: str, *, scope: str = "global", into: str | None = None,
          new: bool = False, why: str = "", serves: str | None = None, wiki: str | None = None,
          distinct_from=(), session: str = "", now: datetime | None = None, embedder="auto") -> dict:
    now = now or datetime.now(timezone.utc)
    emb = _embedder(embedder)
    ss = scopes(cwd)
    if not ss:
        raise CommandError("no wiki with a database in scope")
    if into and new:
        raise db.LedgerError("--into and --new are two answers to one question: give one")
    home, row = None, None
    for s in (x for x in ss if wiki in (None, x.label)):
        c = s.conn()
        try:
            r = db.get_row(c, issue_id)
            if r is not None and r.kind == "I":
                home, row = s, r
                break
        finally:
            c.close()
    if home is None:
        raise CommandError(f"no issue {issue_id!r} in scope" + (f" ({wiki} wiki)" if wiki else ""))
    hconn = home.conn()
    try:
        row = db.get_row(hconn, row.id)
        tags = _tags(hconn, row.id)
        if "learned" in tags:
            return {"issue": row.id, "wiki": home.label, "status": "already-learned",
                    "target": tag_of(tags, "generalized-into:")}
        if "correction" not in tags:
            raise db.LedgerError(f"{row.id} is not a correction")
        if "meta-learning" in tags and meta_applied_week(hconn, now) >= META_PER_WEEK:
            raise db.LedgerError(f"learning-system corrections are applied at most {META_PER_WEEK} a week: "
                                 f"{row.id} stays recorded and pending until the week turns")
        rule_id, rule_label, skill_name = _parse_into(into, ss)
        sc = scope.strip()
        if skill_name is None and sc.startswith("skill:"):
            skill_name = sc[6:]
        elif skill_name is None and sc not in ("global", "repo"):
            raise db.LedgerError("--scope is global (the default), repo (with --why), or skill:NAME")
        if skill_name is not None:
            target = _find_skill(ss, skill_name)
        else:
            want = rule_label or sc
            target = next((s for s in ss if s.label == want), None)
            if target is None:
                raise CommandError(f"no {want} wiki with a database in scope")
        why = " ".join(str(why or "").split())
        if skill_name is None and target.label == "repo" and len(why) < 8:
            raise db.LedgerError("--scope repo is the exception: say why this holds only in this repo (--why TEXT)")
        warnings = ["the principle reads like an instruction or carries a URL or shell fragment: it is stored "
                    "flattened, and printed only as recalled context"] if _HINT.search(str(principle)) else []
        principle = clean_principle(principle)
        why = flatten(why, 240) if why else ""
        serve = resolve_serves(hconn, serves, home.key, home.label)
        text = inert(issue_text(row))
        res = _apply_rule(ss, target, home, row, principle, text, why, serve, rule_id, new, skill_name, now, emb,
                         list(distinct_from or ()))
        res["warnings"] = warnings
        res["scope"] = f"skill:{skill_name}" if skill_name else target.label
        group = [row.id]
        for (gid,) in hconn.execute("SELECT t.id FROM tags t WHERE t.tag = ?", (f"repeat-of:{row.id}",)).fetchall():
            if not _tags(hconn, gid) & {"learned", "dismissed"}:
                group.append(gid)
        tgt = _ref(home, target, res["target"])
        stamp = db.now_iso(now)
        for gid in group:
            db.retag(hconn, gid, add=["learned", f"learned-at:{stamp}", f"generalized-into:{tgt}", f"serves:{serve}"])
            if target.label == home.label:
                db.link(hconn, gid, res["target"], "related")
        _forget_mirrors(ss, home, group)
        res.update(issue=row.id, learned=True, serves=serve, also_learned=group[1:], principle=principle)
        metrics.record(home.root, "learn", session=session, ref=row.id, val=0.0 if serve == "none" else 1.0,
                       data={"serves": serve, "target": tgt}, now=now)
        return res
    finally:
        hconn.close()


def _forget_mirrors(ss: list[Scope], home: Scope, ids: list[str]) -> None:
    g = next((s for s in ss if s.label == "global"), None)
    if home.label != "repo" or g is None:
        return
    try:
        with LearnIndex(g.root) as ix:
            for i in ids:
                ix._drop(i, home.key)
            ix.conn.commit()
    except Exception:
        pass


def _apply_rule(ss, target: Scope, home: Scope, row: db.Row, principle: str, text: str, why: str, serve: str,
                rule_id: str | None, new: bool, skill_name: str | None, now: datetime, emb,
                distinct_from: list[str] | None = None) -> dict:
    """File *principle* in *target*'s wiki: merge into the rule the caller named, or add a new principle
    when the caller says so. Refuses to guess."""
    conn = target.conn()
    try:
        rules = {r.id: r for r in db.rows(conn, "R")} if rule_id is None and not new else {}
        pt = tokens(principle)
        cands = _candidates(ss, target, principle, serve, emb, skill_name)
        chosen = None
        if rule_id:
            chosen = db.get_row(conn, rule_id)
            if chosen is None or chosen.kind != "R":
                raise db.LedgerError(f"no rule {rule_id!r} in the {target.label} wiki")
            if "churn" in _tags(conn, chosen.id):
                raise db.LedgerError(f"{chosen.id} needs enforcement, not rewording: it was revised {REVISION_LIMIT} "
                                     "times and is still violated. Ask the operator for a hook or a test; "
                                     "no fourth revision is filed")
            if any(dice(pt, tokens(x)) >= LEX_TOPIC and opposite(principle, x) for x in statements_of(chosen)):
                raise db.LedgerError(f"the principle contradicts {chosen.id}: file it with --new (it is linked "
                                     f"`conflicts`), or edit {chosen.id} yourself")
        elif skill_name is not None and not new:
            # `--into skill:NAME`: the principle belongs to that skill; fold it into the skill's close rule
            for c in cands:
                if c["id"].startswith("skill:") or c["churn"]:
                    continue
                r = db.get_row(conn, c["id"])
                if r is not None and f"skill:{skill_name}" in _tags(conn, r.id) and \
                        dice(pt, tokens(r.summary)) >= LEX_MERGE and not any(
                            opposite(principle, x) for x in statements_of(r)):
                    chosen = r
                    break
        elif not new:
            if cands:
                raise ChooseError(cands)
        if chosen is None and (new or skill_name is not None or not cands):
            claimed = {x.strip() for x in (distinct_from or ())}
            churn = [c for c in cands if c["churn"] and c.get("near")
                     and c["id"] not in claimed and f"{c['wiki']}/{c['id']}" not in claimed]
            if churn:
                c0 = churn[0]
                raise ChooseError(cands, f"{c0['wiki']}/{c0['id']} ({c0['summary'][:60]!r}) needs enforcement, not "
                                         "rewording, and this principle is near it: no new principle is filed "
                                         "beside it. If this is a different subject, say so: `--new "
                                         f"--distinct-from {c0['id']}`; otherwise ask the operator for a hook or "
                                         "a test")
            if len(live_principles(conn)) >= MAX_PRINCIPLES:
                raise db.LedgerError(f"the {target.label} wiki holds {MAX_PRINCIPLES} principles, the most it keeps: "
                                     "merge two first (`shapa learn merge SRC DST`) or file this one with --into")
        stamp = db.now_iso(now)
        wiki_key = inert(home.key).replace(" ", "-")[:40] or "repo"
        key = f"{row.id} {wiki_key} {(row.created or stamp)[:10]}"
        claim = "".join(f"[distinct from {x.strip()[:20]}] " for x in (distinct_from or ())[:3] if x.strip())
        entry_line = f"- {key}: {claim}{text[:140]}"
        extra = [f"skill:{skill_name}"] if skill_name else []
        if chosen is None:
            conflicts = []
            for r in (rules or {r.id: r for r in db.rows(conn, "R")}).values():
                if dice(pt, tokens(r.summary)) >= LEX_TOPIC and opposite(principle, r.summary):
                    conflicts.append(r.id)
            head = [f"Why: {why}"] if why else []
            rid, created = db.add_row(conn, "R", principle, _join_body(head, [entry_line]),
                                      tags=["principle", f"serves:{serve}", "evidence:1", f"last-seen:{stamp}"] + extra,
                                      source="learn", now=now)
            if created:
                for c in conflicts[:3]:
                    db.link(conn, rid, c, "conflicts")
                _post_write(target, rid, now)
                return {"action": "created", "target": rid, "wiki": target.label, "conflicts": conflicts[:3]}
            chosen = db.get_row(conn, rid)          # the identical principle is already stored
        if f"{db.STAGE_PREFIX}0" in _tags(conn, chosen.id):
            raise db.LedgerError(f"{chosen.id} is baseline (stage 0): its text and tags change only from the local "
                                 "CLI. File the principle with --new, or --into another rule")
        head, ev = _split_body(chosen.body)
        statements = [chosen.summary] + [re.sub(r"^Also:\s*", "", l) for l in head if l.startswith("Also:")]
        if not any(dice(pt, tokens(x)) >= 0.85 for x in statements):
            head.append(f"Also: {principle}")
            also = [i for i, l in enumerate(head) if l.startswith("Also:")]
            for i in reversed(also[:-ALSO_KEEP]):
                head.pop(i)
        if why and not any(l.startswith("Why:") for l in head):
            head.insert(0, f"Why: {why}")
        seen = any(l.startswith(f"- {key}:") for l in ev)
        if not seen:
            ev.append(entry_line)
        tg = _tags(conn, chosen.id)
        n_evidence = _count(tg, "evidence:") + (0 if seen else 1)
        ev = ev[-EVIDENCE_KEEP:]
        outstanding = _count(tg, "violations:") > _count(tg, "revisions:")
        mapping = {"evidence:": max(n_evidence, len(ev)), "last-seen:": stamp}
        if outstanding:
            mapping["revisions:"] = _count(tg, "revisions:") + 1
        # the 100-tag cap of a record is checked before anything is written: a refused merge changes nothing
        after = {t for t in tg if not any(t.startswith(k) for k in mapping)} | {"principle", f"serves:{serve}"}
        after |= {f"{k}{v}" for k, v in mapping.items()} | ({f"skill:{skill_name}"} if skill_name else set())
        if len(after) > db.MAX_TAGS:
            raise db.LedgerError(f"{chosen.id} carries {len(tg)} tags and a record holds at most {db.MAX_TAGS}: "
                                 "file the principle with --new")
        db.update_row(conn, chosen.id, body=_join_body(head, ev))
        _set_tags(conn, chosen.id, mapping, extra=["principle", f"serves:{serve}"])
        if skill_name and f"skill:{skill_name}" not in tg:
            db.tag(conn, chosen.id, [f"skill:{skill_name}"])   # files the pending version, once
        _post_write(target, chosen.id, now)
        return {"action": "merged", "target": chosen.id, "wiki": target.label, "conflicts": [],
                "revision": outstanding}
    finally:
        conn.close()


def _post_write(target: Scope, rid: str, now: datetime) -> None:
    """Keep the derived index current with a rule just written, and remember this device wrote it."""
    try:
        c = target.conn()
        try:
            row = db.get_row(c, rid)
            with LearnIndex(target.root, timeout=5.0) as ix:
                if row is not None:
                    ix.upsert_row(row)
        finally:
            c.close()
    except Exception:
        pass
    _mark_local(target.root, rid, now)


def merge(cwd, src: str, dst: str, wiki: str | None = None) -> dict:
    """Fold principle *src* into *dst* (same wiki): evidence and counters combined, *src* removed. A
    churned principle is not merged away (it would shed the mark)."""
    ss = scopes(cwd)
    label = wiki
    if "/" in src:
        label, src = src.split("/", 1)
    if "/" in dst:
        l2, dst = dst.split("/", 1)
        label = label or l2
    target = next((s for s in ss if s.label == (label or "global")), None)
    if target is None:
        raise CommandError("no such wiki in scope")
    conn = target.conn()
    try:
        a, b = db.get_row(conn, src), db.get_row(conn, dst)
        if a is None or b is None or a.kind != "R" or b.kind != "R" or src == dst:
            raise db.LedgerError("merge takes two different rules of one wiki")
        for r in (a, b):
            tg = _tags(conn, r.id)
            if "principle" not in tg:
                raise db.LedgerError(f"{r.id} is not a principle")
            if "churn" in tg:
                raise db.LedgerError(f"{r.id} needs enforcement, not rewording: it cannot be merged to shed that")
            if f"{db.STAGE_PREFIX}0" in tg:
                raise db.LedgerError(f"{r.id} is baseline (stage 0): it changes only from the local CLI "
                                     "(`shapa stage clear` first)")
        bh, bev = _split_body(b.body)
        ah, aev = _split_body(a.body)
        if dice(tokens(a.summary), tokens(b.summary)) < 0.85:
            bh.append(f"Also: {a.summary}")
        bh += [l for l in ah if l.startswith(("Also:", "Why:")) and l not in bh]
        also = [i for i, l in enumerate(bh) if l.startswith("Also:")]
        for i in reversed(also[:-ALSO_KEEP]):
            bh.pop(i)
        ev = list(dict.fromkeys(bev + aev))[-EVIDENCE_KEEP:]
        db._merge_into(conn, a.id, b.id)
        db.update_row(conn, b.id, body=_join_body(bh, ev))
        _set_tags(conn, b.id, {"evidence:": max(_count(_tags(conn, b.id), "evidence:"), len(ev))})
        conn.execute("UPDATE OR IGNORE tags SET tag = ? WHERE tag = ?", (f"generalized-into:{b.id}", f"generalized-into:{a.id}"))
        for s in ss:
            if s is not target:
                c2 = s.conn()
                try:
                    c2.execute("UPDATE OR IGNORE tags SET tag = ? WHERE tag = ?",
                               (f"generalized-into:{target.label}:{b.id}", f"generalized-into:{target.label}:{a.id}"))
                finally:
                    c2.close()
        db.delete(conn, [a.id])
    finally:
        conn.close()
    try:
        with LearnIndex(target.root, timeout=5.0) as ix:
            ix.drop(src)
    except Exception:
        pass
    _post_write(target, dst, datetime.now(timezone.utc))
    return {"merged": src, "into": dst, "wiki": target.label}


def unchurn(cwd, ref: str, wiki: str | None = None) -> dict:
    """The operator's word that enforcement now exists for a churned principle: clears ``churn`` and the
    revision count, and lifts ``blocked`` from the corrections that were repeats of it."""
    ss = scopes(cwd)
    label = wiki
    if "/" in ref:
        label, ref = ref.split("/", 1)
    target = next((s for s in ss if s.label == (label or "global")), None)
    if target is None:
        raise CommandError("no such wiki in scope")
    conn = target.conn()
    try:
        tg = _tags(conn, ref)
        if "principle" not in tg or "churn" not in tg:
            raise db.LedgerError(f"{ref} is not a churned principle")
        db.retag(conn, ref, drop=[t for t in tg if t == "churn" or t.startswith("revisions:")])
    finally:
        conn.close()
    lifted = 0
    for s2 in ss:
        c2 = s2.conn()
        try:
            names = {f"repeat-of:{target.label}:{ref}"}
            if s2 is target:
                names.add(f"repeat-of:{ref}")
            marks = ",".join("?" * len(names))
            for (cid,) in c2.execute(f"SELECT id FROM tags WHERE tag IN ({marks})", sorted(names)).fetchall():
                if "blocked" in _tags(c2, cid):
                    db.retag(c2, cid, drop=["blocked"])
                    lifted += 1
        finally:
            c2.close()
    return {"unchurned": ref, "wiki": target.label, "unblocked": lifted}


def principles(cwd=None) -> list[dict]:
    """Every principle in scope with what it records: its source corrections (id, wiki key, date, text),
    why (a repo-scoped one), times violated and revised, last seen, the goals it serves, and, on this
    device, how often session start served it."""
    out = []
    for s in scopes(cwd):
        conn = s.conn()
        try:
            served = {}
            lc = metrics.open_local(s.root, create=False)
            if lc is not None:
                try:
                    served = {r[0]: (r[1], r[2]) for r in lc.execute("SELECT id, n, last FROM p_served")}
                finally:
                    lc.close()
            for rid in live_principles(conn):
                r = db.get_row(conn, rid)
                if r is None:
                    continue
                tg = set(r.tags)
                head, ev = _split_body(r.body)
                out.append({
                    "id": r.id, "wiki": s.label, "summary": flatten(r.summary),
                    "why": next((l[5:].strip() for l in head if l.startswith("Why:")), ""),
                    "evidence": _count(tg, "evidence:"), "sources": [e.lstrip("- ") for e in ev],
                    "violations": _count(tg, "violations:"), "revisions": _count(tg, "revisions:"),
                    "last_seen": tag_of(tg, "last-seen:"), "churn": "churn" in tg,
                    "serves": sorted(t[7:] for t in tg if t.startswith("serves:")),
                    "served": served.get(r.id, (0, None))[0], "last_served": served.get(r.id, (0, None))[1]})
        finally:
            conn.close()
    return out


def dismiss(cwd, issue_id: str, wiki: str | None = None) -> dict:
    """A correction that implies no principle. A soft candidate is removed outright (it teaches nothing
    and is never measured); a strong one is kept, tagged ``dismissed``."""
    ss = scopes(cwd)
    for s in ss:
        if wiki not in (None, s.label):
            continue
        conn = s.conn()
        try:
            r = db.get_row(conn, issue_id)
            if r is None or r.kind != "I":
                continue
            soft = "soft" in _tags(conn, r.id)
            if soft:
                db.delete(conn, [r.id])
            else:
                db.retag(conn, r.id, add=["dismissed"])
        finally:
            conn.close()
        _forget_mirrors(ss, s, [issue_id])
        try:
            with LearnIndex(s.root, timeout=5.0) as ix:
                if soft:
                    ix.drop(issue_id)
        except Exception:
            pass
        return {"issue": issue_id, "wiki": s.label, "status": "removed" if soft else "dismissed"}
    raise CommandError(f"no issue {issue_id!r} in scope")


# --- 3. session start -----------------------------------------------------------------------------------

def principle_candidates(conn, label: str, root) -> list[dict]:
    """The wiki's live principles (not the churned ones): tags and summaries only. The counters in their
    tags count for ranking only on a principle this device wrote itself."""
    mine = local_principles(root)
    out = []
    for rid in live_principles(conn):
        tg = _tags(conn, rid)
        if "churn" in tg:
            continue
        r = conn.execute("SELECT summary FROM mri WHERE id = ?", (rid,)).fetchone()
        local = rid in mine
        out.append({"id": rid, "wiki": label, "root": str(root), "text": r[0], "local": local,
                    "violations": _count(tg, "violations:") if local else 0,
                    "evidence": _count(tg, "evidence:") if local else 0,
                    "serves": (tag_of(tg, "serves:") or "none") if local else "none"})
    return out


def rank_principles(cands: list[dict], fire_text: str, fire_keys: set, k: int = PRINCIPLES_SHOWN) -> list[dict]:
    ft = tokens(fire_text)
    scored = []
    for c in cands:
        pt = tokens(c["text"])
        overlap = len(pt & ft) / max(1, len(pt))
        score = 4 * overlap + min(c["violations"], 3) + 0.3 * min(c["evidence"], 5) \
            + (1.0 if c["serves"] in fire_keys or c["serves"] == "northstar" else 0.0)
        if score > 0:
            scored.append((-score, c["wiki"] != "repo", c["id"], c))
    return [c for *_, c in sorted(scored, key=lambda x: x[:3])[:k]]


def principle_line(c: dict) -> str:
    """One principle as session start prints it: masked, capped, labelled as recalled context."""
    return (f"[{c['wiki']} {c['id']}] {flatten(c['text'], 160)}"
            + (f" (violated {c['violations']}x)" if c["violations"] else ""))


def drift(conn, root, now: datetime | None = None, churn: list | None = None) -> list[str]:
    """Why the work may be off its goal (empty when fine): jobs closing outside every fire, a stalled
    fire, a principle that needs enforcement, latency rising with the data."""
    from shapa import focus

    now = now or datetime.now(timezone.utc)
    out = []
    try:
        f = focus.load(conn)
        if any(x and x.feature for x in f.fires):
            cut = db.now_iso(now - timedelta(days=DRIFT_DAYS))
            closed = metrics.closed_jobs_since(conn, root, cut)
            off = [r for r in closed if not r["fire"]]
            if len(closed) >= DRIFT_MIN_JOBS and len(off) / len(closed) > DRIFT_OFF_SHARE:
                out.append(f"drift: {len(off)} of {len(closed)} jobs closed in {DRIFT_DAYS} days sat outside every fire")
            stalled = metrics.stalled_fires(conn, root, now)
            if stalled:
                out.append(f"drift: fire {', '.join(stalled)} has had no job closed in {metrics.STALL_DAYS} days")
        sc = metrics.latency_scale(root, now)
        if sc["flag"]:
            out.append(f"drift: fetch latency grew {sc['latency_growth']}x while the data grew {sc['data_growth']}x")
    except Exception:
        pass
    if churn:
        out.append(f"drift: principle {churn[0]['id']} needs enforcement, not rewording (shapa learn pending)")
    return out


def reindex(cwd=None) -> int:
    """Catch the derived index up with every wiki in scope (the sweep calls this)."""
    n = 0
    for s in scopes(cwd):
        conn = s.conn()
        try:
            with LearnIndex(s.root, timeout=5.0) as ix:
                ix.sync(conn)
                n += 1
        finally:
            conn.close()
    return n


# --- the CLI ---------------------------------------------------------------------------------------------

def _print_pending(res: dict) -> None:
    if not res["pending"] and not res["churn"] and not res["awaiting_activation"]:
        print("no corrections await learning")
        return
    print(f"{res['count']} correction{'' if res['count'] == 1 else 's'} await learning")
    for i in res["pending"]:
        flags = [x for x in ("soft", "repeat", "violation", "meta", "capped", "blocked") if i.get(x)]
        also = f" (+{len(i['also'])} repeats: {', '.join(i['also'])})" if i["also"] else ""
        print(f"{i['id']} [{i['wiki']}/{i['key']}] {i['summary']}{also}" + (f"  <{', '.join(flags)}>" if flags else ""))
        for n in i["nearest"][:3]:
            print(f"    near {n['ref']} ({n['kind']}, {n['score']:.2f}{', merge' if n['merge'] else ''}): {n['text'][:90]}")
    for c in res["churn"]:
        print(f"{c['wiki']}/{c['id']} {c['summary']}  <{c['note']}; revised {c['revisions']}x, violated {c['violations']}x>")
    for a in res["awaiting_activation"]:
        print(f"skill {a['skill']} v{a['version']} awaits activation (`shapa skill activate`): {a['lesson']}")


def run_learn(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa learn", description="Generalize corrections into principles.")
    p.add_argument("--cwd", default=None)
    sub = p.add_subparsers(dest="cmd")
    add = _subcommand_adder(sub)
    pe = add("pending", help="corrections not yet generalized, with their nearest rules, skills and issues")
    pe.add_argument("--json", action="store_true")
    pe.add_argument("--session", default="")
    pe.add_argument("--limit", type=int, default=20)
    ap = add("apply", help="record the principle a correction implies")
    ap.add_argument("issue")
    ap.add_argument("--principle", required=True)
    ap.add_argument("--scope", default="global", help="global (default), repo (needs --why) or skill:NAME")
    ap.add_argument("--why", default="")
    ap.add_argument("--into", default=None, help="merge into this rule (R<n>, global/R<n>, repo/R<n>) or skill:NAME")
    ap.add_argument("--new", action="store_true", help="no listed candidate covers it")
    ap.add_argument("--distinct-from", action="append", default=[], metavar="R<n>",
                    help="with --new: a churned principle near this one is about a different subject (recorded)")
    ap.add_argument("--serves", default=None, help="<wiki key>/F<n> or northstar (default: the claimed fire)")
    ap.add_argument("--wiki", choices=["repo", "global"], default=None, help="the wiki holding the issue")
    ap.add_argument("--session", default="")
    ap.add_argument("--json", action="store_true")
    di = add("dismiss", help="a correction that implies no general principle")
    di.add_argument("issue")
    di.add_argument("--wiki", choices=["repo", "global"], default=None)
    mg = add("merge", help="fold principle SRC into DST (evidence combined)")
    mg.add_argument("src")
    mg.add_argument("dst")
    mg.add_argument("--wiki", choices=["repo", "global"], default=None)
    uc = add("unchurn", help="enforcement now exists for a churned principle: clear churn and its revisions")
    uc.add_argument("principle")
    uc.add_argument("--wiki", choices=["repo", "global"], default=None)
    pr = add("principles", help="the principles, with their evidence, violations and serves")
    pr.add_argument("--json", action="store_true")
    add("reindex", help="catch the derived similarity index up")
    args = p.parse_args(argv)
    if args.cmd == "principles":
        res = principles(args.cwd)
        if args.json:
            print(json.dumps(res, sort_keys=True))
        for x in ([] if args.json else res):
            print(f"{x['wiki']}/{x['id']} {x['summary']}  <evidence {x['evidence']}, violated {x['violations']}x, "
                  f"revised {x['revisions']}x, served {x['served']}x here, serves {', '.join(x['serves'])}"
                  f"{', CHURN' if x['churn'] else ''}>")
        return 0
    if args.cmd == "apply":
        try:
            res = apply(args.cwd, args.issue, args.principle, scope=args.scope, why=args.why, into=args.into,
                        new=args.new, serves=args.serves, wiki=args.wiki, session=args.session,
                        distinct_from=args.distinct_from)
        except ChooseError as exc:
            if args.json:
                print(json.dumps({"error": "choose", "candidates": exc.candidates, "message": str(exc)}, sort_keys=True))
                return 2
            raise
        if args.json:
            print(json.dumps(res, sort_keys=True))
        elif res.get("status") == "already-learned":
            print(f"{res['issue']} is already learned (into {res.get('target')})")
        else:
            print(f"{res['issue']} learned: {res['action']} {res['target']} [{res['wiki']}], serves {res['serves']}"
                  + (f"; conflicts {', '.join(res['conflicts'])}" if res.get("conflicts") else ""))
        return 0
    if args.cmd == "dismiss":
        res = dismiss(args.cwd, args.issue, args.wiki)
        print(f"{res['issue']} {res['status']}")
        return 0
    if args.cmd == "unchurn":
        res = unchurn(args.cwd, args.principle, args.wiki)
        print(f"{res['unchurned']} [{res['wiki']}] no longer churned; {res['unblocked']} correction(s) unblocked")
        return 0
    if args.cmd == "merge":
        res = merge(args.cwd, args.src, args.dst, args.wiki)
        print(f"{res['merged']} merged into {res['into']} [{res['wiki']}]")
        return 0
    if args.cmd == "reindex":
        print(f"indexed {reindex(args.cwd)} wiki(s)")
        return 0
    res = pending(args.cwd, session=args.session, limit=max(1, args.limit))
    if getattr(args, "json", False):
        print(json.dumps(res, sort_keys=True))
    else:
        _print_pending(res)
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        code = run_learn(argv)
    except (CommandError, db.LedgerError) as exc:
        print(f"shapa: {exc}", file=sys.stderr)
        code = 2
    except sqlite3.Error as exc:
        print(f"shapa: the database is locked or unreadable ({type(exc).__name__}); try again", file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
