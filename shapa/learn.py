"""``shapa learn`` - a correction is made once, learned once, and serves every repo.

The operator's correction ("no, wrong, not like this") is already an issue row
(:func:`shapa.ledger.record_correction`). This module closes the loop:

1. **Repeat detection** (:func:`on_correction`): a new correction is compared, by
   stemmed-token similarity (a Dice score over an inverted index, the same word rule
   as the FTS index) and, with ``[semantic]``, by vector cosine, with every earlier
   issue and every rule of the repo wiki AND the global wiki. Close to one: it is a
   repeat. It is tagged ``repeat`` and ``repeat-of:REF``, linked ``related`` when the
   target is in the same wiki, the earlier row's count (``repeats:N``, or
   ``violations:N`` on a principle) goes up, and the row's ``session`` column is the
   mark. A repeat of something already learned is a *violation* of that principle.
2. **Generalize without bloat** (:func:`apply`, ``shapa learn apply``): the agent states
   the principle the correction implies. A principle is one rule row (tag
   ``principle``): the statement is its summary, the evidence (source corrections: id,
   wiki key, date, text) its body. A close principle MUST merge into the rule that holds
   it (evidence appended, wording kept as one ``Also:`` line), never a second row. The
   default scope is the GLOBAL wiki: principles apply across repos. ``--scope repo``
   needs ``--why``. ``skill:NAME`` files a new version of a skill; a version derived from
   a correction the hook itself filed activates at once (the operator's word), anything
   else stays pending.
3. **Directed**: every apply records what it serves (the fire of the claimed feature,
   ``--serves F<n>|northstar``, else ``none``, which is counted).
4. **Loop guards**: a correction is learned once; a principle revised three times whose
   violations keep coming is marked ``churn`` (a fourth revision is refused: it needs
   enforcement, not rewording); corrections about the learning system itself are applied
   at most :data:`META_PER_WEEK` a week.

Tags used (all ordinary row tags, so a sync carries them with no Worker change):

issue: ``correction``, ``repeat-checked``, ``repeat``, ``repeat-of:REF``, ``violation``,
``meta-learning``, ``blocked``, ``learned``, ``learned-at:ISO``, ``generalized-into:REF``,
``serves:F3|northstar|none``, ``dismissed``, ``repeats:N``.
principle (rule row): ``principle``, ``violations:N``, ``revisions:N``, ``churn``,
``evidence:N``, ``last-seen:ISO``, ``serves:...``.

CLI::

    shapa learn pending [--json] [--session ID] [--limit N]
    shapa learn apply ISSUE --principle TEXT [--scope global|repo|skill:NAME] [--why TEXT]
                      [--into R<n>|skill:NAME] [--serves F<n>|northstar] [--wiki repo|global] [--json]
    shapa learn dismiss ISSUE [--wiki repo|global]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shapa import config, db, embed, metrics, skills
from shapa.ledger import CommandError, _subcommand_adder, run

LEX_REPEAT, COS_REPEAT = 0.55, 0.85
LEX_MERGE, COS_MERGE = 0.50, 0.80
LEX_TOPIC = 0.34          # same subject, so a flipped polarity is a contradiction
PRINCIPLE_MIN, PRINCIPLE_MAX = 12, 160
EVIDENCE_KEEP = 12
ALSO_KEEP = 4
MAX_SECTION_LINES = 25
NEAREST_SHOWN = 5
REVISION_LIMIT = 3
META_PER_WEEK = 3
PRINCIPLES_SHOWN = 4
DRIFT_DAYS = 14
DRIFT_MIN_JOBS = 4
DRIFT_OFF_SHARE = 0.5
SKILL_SECTION = "## Learned principles"

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
_UNSAFE = re.compile(
    r"(ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier)|disregard|system\s+prompt|"
    r"you\s+are\s+now|new\s+instructions|jailbreak|exfiltrat|https?://|www\.|\bcurl\b|\bwget\b|base64|"
    r"\|\s*(?:sh|bash|zsh)\b|<\s*/?\s*(?:system|shapa|assistant|user|tool)|\bsudo\b|\brm\s+-rf\b)", re.I)


def inert(text: str) -> str:
    """*text* as one line that can open no tag, heading, fence or list in what it is quoted into."""
    from shapa.redact import redact

    t = _ZERO.sub(" ", unicodedata.normalize("NFKC", str(text or "")))
    t = " ".join(redact(t).split()).replace("`", "'").replace("<", "‹").replace(">", "›")
    return re.sub(r"\[[A-Za-z]+:?\d+\]", "(ref)", t)


def clean_principle(text: str, *, limit: int = PRINCIPLE_MAX) -> str:
    """One safe line: no control or direction characters, no markdown structure, no wrapper
    tags, no instruction-injection phrases. A principle ends up in a rule and at session
    start (and an operator-derived skill version goes live at once), so it is checked, not trusted."""
    from shapa.redact import redact

    t = unicodedata.normalize("NFKC", str(text or ""))
    t = _ZERO.sub(" ", t)
    t = " ".join(redact(t).split())
    t = t.replace("`", "'").replace("<", "‹").replace(">", "›").replace("---", "-")
    t = re.sub(r"\[[A-Za-z]+:?\d+\]", "(ref)", t)             # a lesson marker must not be forgeable
    for _ in range(4):                                         # no heading, quote, bullet, list marker or label
        t = re.sub(r"^(?:[#>*\-+•]+|\(ref\)|\d+[.)])\s*", "", t.strip())
        t = _LABEL.sub("", t)
    t = t.strip()
    if len(t) < PRINCIPLE_MIN or not re.search(r"[^\W\d_]{3}", t):
        raise db.LedgerError(f"a principle is a sentence of at least {PRINCIPLE_MIN} characters")
    if len(t) > limit:
        raise db.LedgerError(f"a principle is at most {limit} characters: state the general rule, "
                             "not the occasion")
    if _UNSAFE.search(t):
        raise db.LedgerError("that principle reads as an instruction to the agent, or carries a URL or "
                             "shell fragment: state the rule itself, in the operator's terms")
    return t


# --- the similarity index -----------------------------------------------------------------------

@dataclass
class Entry:
    label: str                 # repo | global
    id: str                    # R5, I12, or the skill name
    kind: str                  # R | I | S
    summary: str
    chunks: list
    tags: set
    created: str = ""

    @property
    def n(self) -> int:
        m = re.search(r"\d+$", self.id)
        return int(m.group()) if m else 0


@dataclass
class Neighbor:
    entry: Entry
    chunk: str
    lex: float
    cos: float | None
    score: float = 0.0

    @property
    def repeat(self) -> bool:
        return self.lex >= LEX_REPEAT or (self.cos is not None and self.cos >= COS_REPEAT)

    @property
    def merge(self) -> bool:
        return self.lex >= LEX_MERGE or (self.cos is not None and self.cos >= COS_MERGE)


def default_embedder():
    return embed.embed_many if embed.available() else None


class Index:
    """Entries' chunks under an inverted token index; vectors are computed lazily, once."""

    def __init__(self, entries: list[Entry], embedder="auto"):
        self.entries = entries
        self.embedder = default_embedder() if embedder == "auto" else embedder
        self.chunks: list[tuple[int, str, frozenset]] = []
        self.post: dict[str, list[int]] = {}
        for ei, e in enumerate(entries):
            for text in e.chunks:
                toks = tokens(text)
                self.post_add(len(self.chunks), toks)
                self.chunks.append((ei, text, toks))
        self._vecs = None

    def post_add(self, ci: int, toks) -> None:
        for t in toks:
            self.post.setdefault(t, []).append(ci)

    def _vectors(self):
        if self._vecs is None:
            self._vecs = self.embedder([c[1] for c in self.chunks]) if self.chunks else []
        return self._vecs

    def _cosines(self, text: str) -> list[float] | None:
        if self.embedder is None or not self.chunks:
            return None
        q = self.embedder([text])[0]
        vecs = self._vectors()
        try:
            import numpy as np

            return [float(x) for x in (np.asarray(vecs, dtype=float) @ np.asarray(q, dtype=float))]
        except ImportError:
            return [float(sum(a * b for a, b in zip(v, q))) for v in vecs]

    def nearest(self, text: str, k: int = NEAREST_SHOWN, *, kinds=None, skip=None) -> list[Neighbor]:
        qt = tokens(text)
        cands = {ci for t in qt for ci in self.post.get(t, ())}
        cos = self._cosines(text)
        if cos is not None:
            cands |= {ci for ci, c in enumerate(cos) if c >= COS_MERGE - 0.1}
        best: dict[int, Neighbor] = {}
        for ci in cands:
            ei, chunk, toks = self.chunks[ci]
            e = self.entries[ei]
            if (kinds and e.kind not in kinds) or (skip and skip(e)):
                continue
            n = Neighbor(e, chunk, dice(qt, toks), cos[ci] if cos is not None else None)
            n.score = max(n.lex, n.cos or 0.0)
            if ei not in best or n.score > best[ei].score:
                best[ei] = n
        return sorted(best.values(), key=lambda n: (-n.score, n.entry.label != "repo", n.entry.id))[:k]


# --- scopes and entries ---------------------------------------------------------------------------

@dataclass
class Scope:
    label: str
    root: Path
    key: str

    def conn(self):
        c = db.connect(self.root)
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
    if ":" in ref and not ref.startswith("skill:"):
        label, rid = ref.split(":", 1)
        return next((s for s in ss if s.label == label), None), rid
    return frm, ref


def _evidence_texts(body: str) -> list[str]:
    out = []
    for line in (body or "").splitlines():
        m = re.match(r"^- [A-Z]\d+ \S+ \d{4}-\d\d-\d\d: (.+)$", line)
        if m:
            out.append(m.group(1))
    return out


def _chunks_of(row: db.Row) -> list[str]:
    if row.kind == "I":
        return [issue_text(row)]
    chunks = [row.summary]
    for line in (row.body or "").splitlines():
        line = line.strip()
        if line in ("Evidence:",) or len(line) < 8:
            continue
        m = re.match(r"^- [A-Z]\d+ \S+ \d{4}-\d\d-\d\d: (.+)$", line)
        chunks.append(m.group(1) if m else re.sub(r"^(?:Also|Why):\s*", "", line))
    return chunks


def statements_of(row: db.Row) -> list[str]:
    """What a rule asserts, to test a new principle against: its summary. Not the corrections quoted as
    evidence (the operator's reactions), and not the ``Also:`` wordings (alternative phrasings of the
    summary, whose looser polarity words would raise false contradictions)."""
    return [row.summary]


def load_entries(ss: list[Scope], *, skills_too: bool = False) -> list[Entry]:
    out = []
    for s in ss:
        conn = s.conn()
        try:
            tags: dict[str, set] = {}
            for rid, t in conn.execute("SELECT id, tag FROM tags"):
                tags.setdefault(rid, set()).add(t)
            for r in db.rows(conn):
                if r.kind in ("I", "R"):
                    out.append(Entry(s.label, r.id, r.kind, r.summary, _chunks_of(r), tags.get(r.id, set()), r.created))
            if skills_too:
                for name, vs in skills._all(conn).items():
                    cur = next((v for v in reversed(vs) if v.status == "active"), vs[-1])
                    lines = [l.strip(" -*") for l in cur.body.splitlines() if len(l.strip()) >= 12
                             and not l.startswith("#")]
                    out.append(Entry(s.label, name, "S", name, lines or [cur.description], set(), cur.created))
        finally:
            conn.close()
    return out


# --- tags ------------------------------------------------------------------------------------------

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


def _bump(conn, rid: str, prefix: str, now: datetime) -> int:
    n = _count(_tags(conn, rid), prefix) + 1
    tg = _tags(conn, rid)
    db.retag(conn, rid, add=[f"{prefix}{n}"], drop=[t for t in tg if t.startswith(prefix)])
    return n


# --- per-device state ------------------------------------------------------------------------------

def _local(root):
    return metrics._events_conn(root)


def served_total(root) -> int:
    try:
        conn = metrics._events_conn(root, create=False)
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
        conn = _local(root)
        if conn is None:
            return
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


def _note_hook_filed(root, rid: str, digest: str, now: datetime | None) -> None:
    try:
        conn = _local(root)
        if conn is not None:
            try:
                conn.execute("INSERT OR REPLACE INTO learn_local(id, hash, ts) VALUES (?,?,?)",
                             (rid, digest, db.now_iso(now)))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def operator_authority(root, row: db.Row) -> bool:
    """True only for an issue this device's UserPromptSubmit hook filed itself, with the text
    it filed: not a synced row, not a hand-written one, not an edited one."""
    if row.source != "correction":
        return False
    try:
        conn = metrics._events_conn(root, create=False)
        if conn is None:
            return False
        try:
            r = conn.execute("SELECT hash FROM learn_local WHERE id = ?", (row.id,)).fetchone()
            return bool(r) and r[0] == row.hash
        finally:
            conn.close()
    except Exception:
        return False


# --- 1. repeat detection ------------------------------------------------------------------------------

def _set_tags(conn, rid: str, mapping: dict, extra=()) -> None:
    """Replace every tag starting with a mapping key by ``key + value``, and add *extra*."""
    tg = _tags(conn, rid)
    drop = [t for t in tg if any(t.startswith(k) for k in mapping)]
    db.retag(conn, rid, add=[f"{k}{v}" for k, v in mapping.items()] + list(extra), drop=drop)


def _violate(target_scope: Scope, pid: str, now: datetime) -> tuple[int, bool]:
    """Count a violation on principle *pid*: ``(violations, churn)``. A principle revised
    :data:`REVISION_LIMIT` times that is violated again is marked ``churn``."""
    conn = target_scope.conn()
    try:
        tg = _tags(conn, pid)
        n = _count(tg, "violations:") + 1
        churn = _count(tg, "revisions:") >= REVISION_LIMIT
        _set_tags(conn, pid, {"violations:": n, "last-seen:": db.now_iso(now)}, extra=["churn"] if churn else [])
        return n, churn
    finally:
        conn.close()


def _target_of(ss: list[Scope], e: Entry):
    """For an earlier row *e* that a new correction repeats: ``(scope, id)`` that takes the
    repeat count, and ``(scope, id)`` of the principle it violates (or None)."""
    scope = next(s for s in ss if s.label == e.label)
    if e.kind == "R":
        return (scope, e.id), ((scope, e.id) if "principle" in e.tags else None)
    gi = tag_of(e.tags, "generalized-into:")
    if gi and "learned" in e.tags and not gi.startswith("skill:"):
        psc, pid = _resolve(ss, scope, gi)
        if psc is not None:
            return (scope, e.id), (psc, pid)
    ro = tag_of(e.tags, "repeat-of:")
    if ro:
        rsc, rid = _resolve(ss, scope, ro)
        if rsc is not None:
            return (rsc, rid), None
    return (scope, e.id), None


def on_correction(root, rid: str, created: bool, *, session: str = "", hook: bool = True,
                  now: datetime | None = None, embedder="auto") -> dict:
    """Run after the hook files issue *rid* in wiki *root*: detect a repeat, tag, link, count, mark."""
    now = now or datetime.now(timezone.utc)
    home_root = Path(root)
    ss = scopes(home_root)
    home = next((s for s in ss if s.root.resolve() == home_root.resolve()), None)
    if home is None:
        home = Scope("global" if metrics.repo_key(home_root) == "global" else "repo", home_root,
                     metrics.repo_key(home_root))
        ss = [home] + [s for s in ss if s.root.resolve() != home_root.resolve()]
    search = [home] + ([s for s in ss if s.label == "global" and s is not home] if home.label == "repo" else [])
    info = {"repeat": False, "of": None, "count": 0, "violation": None, "churn": False, "meta": False}
    conn = home.conn()
    try:
        row = db.get_row(conn, rid)
        if row is None:
            return info
        if hook and created:
            _note_hook_filed(home_root, rid, row.hash, now)
        text = issue_text(row)
        tags = _tags(conn, rid)
        if not created:                                    # the same words again: one row, a higher count
            info.update(repeat=True, of=rid, count=_bump(conn, rid, "repeats:", now))
            gi = tag_of(tags, "generalized-into:")
            if gi and not gi.startswith("skill:"):
                psc, pid = _resolve(ss, home, gi)
                if psc is not None:
                    info["violation"] = gi
                    _, info["churn"] = _violate(psc, pid, now)
            metrics.record(home_root, "repeat", session=session, ref=rid, data={"dedup": 1}, now=now)
            return info
        if "repeat-checked" in tags:
            return info
        add = ["repeat-checked"]
        if is_meta(text):
            add.append("meta-learning")
            info["meta"] = True
        n_self = int(rid[1:])

        def skip(e: Entry) -> bool:
            if e.kind != "I":
                return False
            if e.label == home.label:
                return (e.created, e.n) >= (row.created, n_self)
            return e.created >= row.created

        hit = next((n for n in Index(load_entries(search), embedder).nearest(text, 5, kinds=("I", "R"), skip=skip)
                    if n.repeat), None)
        if hit is not None:
            (tscope, tid), pr = _target_of(ss, hit.entry)
            ref = _ref(home, tscope, tid)
            add += ["repeat", f"repeat-of:{ref}"]
            info.update(repeat=True, of=ref)
            if pr is None or pr != (tscope, tid):
                c2 = tscope.conn()
                try:
                    info["count"] = _bump(c2, tid, "repeats:", now)
                finally:
                    c2.close()
            if pr is not None:
                psc, pid = pr
                info["violation"] = _ref(home, psc, pid)
                add.append("violation")
                info["count"], info["churn"] = _violate(psc, pid, now)
                if info["churn"]:
                    add.append("blocked")
            db.retag(conn, rid, add=add)
            if tscope.label == home.label:
                db.link(conn, rid, tid, "related")
            metrics.record(home_root, "repeat", session=session, ref=rid, now=now)
        else:
            db.retag(conn, rid, add=add)
        metrics.record(home_root, "correction", session=session, ref=rid, now=now)
        return info
    finally:
        conn.close()


# --- pending ------------------------------------------------------------------------------------------

def _unlearned(conn) -> list[db.Row]:
    out = []
    tags: dict[str, set] = {}
    for rid, t in conn.execute("SELECT id, tag FROM tags"):
        tags.setdefault(rid, set()).add(t)
    for r in db.rows(conn, "I"):
        tg = tags.get(r.id, set())
        if "correction" in tg and "learned" not in tg and "dismissed" not in tg:
            r.tags = sorted(tg)
            out.append(r)
    return out


def meta_applied_week(conn, now: datetime) -> int:
    cut = db.now_iso(now - timedelta(days=7))
    n = 0
    for rid, in conn.execute("SELECT id FROM tags WHERE tag = 'meta-learning'"):
        for (t,) in conn.execute("SELECT tag FROM tags WHERE id = ? AND tag LIKE 'learned-at:%'", (rid,)):
            n += t[len("learned-at:"):] >= cut
    return n


def awaiting_count(conn, now: datetime | None = None) -> int:
    """Corrections an agent can still act on: unlearned, not dismissed, not blocked, and not a
    learning-system correction past this week's cap. One query, no similarity."""
    now = now or datetime.now(timezone.utc)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM mri m WHERE m.kind = 'I' "
            "AND EXISTS (SELECT 1 FROM tags t WHERE t.id = m.id AND t.tag = 'correction') "
            "AND NOT EXISTS (SELECT 1 FROM tags t WHERE t.id = m.id AND t.tag IN ('learned','dismissed','blocked'))"
        ).fetchone()[0]
        meta = conn.execute(
            "SELECT COUNT(*) FROM mri m WHERE m.kind = 'I' "
            "AND EXISTS (SELECT 1 FROM tags t WHERE t.id = m.id AND t.tag = 'correction') "
            "AND EXISTS (SELECT 1 FROM tags t WHERE t.id = m.id AND t.tag = 'meta-learning') "
            "AND NOT EXISTS (SELECT 1 FROM tags t WHERE t.id = m.id AND t.tag IN ('learned','dismissed','blocked'))"
        ).fetchone()[0]
        if meta and meta_applied_week(conn, now) >= META_PER_WEEK:
            total -= meta
        return max(0, total)
    except Exception:
        return 0


def _brief(n: Neighbor, home: Scope) -> dict:
    e = n.entry
    kind = {"R": "principle" if "principle" in e.tags else "rule", "I": "issue", "S": "skill"}[e.kind]
    return {"ref": _ref(home, Scope(e.label, Path(), ""), e.id) if e.kind != "S" else f"skill:{e.id}",
            "wiki": e.label, "kind": kind, "score": round(n.score, 3), "lex": round(n.lex, 3),
            "cos": None if n.cos is None else round(n.cos, 3), "merge": n.merge, "repeat": n.repeat,
            "text": n.chunk[:160]}


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
                out.append({"id": rid, "wiki": s.label, "summary": r.summary,
                            "violations": _count(tg, "violations:"), "revisions": _count(tg, "revisions:"),
                            "note": "needs enforcement, not rewording"})
        finally:
            conn.close()
    return out


def pending(cwd=None, *, session: str = "", limit: int = 20, now: datetime | None = None,
            embedder="auto") -> dict:
    now = now or datetime.now(timezone.utc)
    ss = scopes(cwd)
    out_items: list[dict] = []
    total = 0
    mode = "lexical"
    for home in ss:
        search = ss if home.label == "repo" else [home]
        entries = load_entries(search, skills_too=True)
        idx = Index(entries, embedder)
        if idx.embedder is not None:
            mode = "fused"
        conn = home.conn()
        try:
            rows = _unlearned(conn)
            by_id = {r.id: r for r in rows}
            folded: dict[str, list[str]] = {}
            for r in rows:
                ro = tag_of(r.tags, "repeat-of:")
                if ro and ":" not in ro and ro in by_id:
                    folded.setdefault(ro, []).append(r.id)
            fold_ids = {x for v in folded.values() for x in v}
            capped = meta_applied_week(conn, now) >= META_PER_WEEK
            for r in rows:
                if r.id in fold_ids or (session and not (r.session or "").startswith(session[:8])):
                    continue
                total += 1
                text = issue_text(r)
                near = idx.nearest(text, NEAREST_SHOWN, skip=lambda e, rid=r.id, lbl=home.label:
                                   e.label == lbl and e.id == rid)
                ro = tag_of(r.tags, "repeat-of:")
                item = {"id": r.id, "wiki": home.label, "key": home.key, "summary": r.summary, "text": text[:300],
                        "session": r.session, "created": r.created, "repeat": "repeat" in r.tags,
                        "repeat_of": ro, "repeat_count": _count(r.tags, "repeats:"),
                        "violation": "violation" in r.tags, "also": sorted(folded.get(r.id, [])),
                        "meta": "meta-learning" in r.tags, "capped": "meta-learning" in r.tags and capped,
                        "blocked": "blocked" in r.tags,
                        "nearest": [_brief(n, home) for n in near]}
                if item["blocked"]:
                    item["note"] = "needs enforcement, not rewording"
                out_items.append(item)
        finally:
            conn.close()
    out_items.sort(key=lambda i: (i["created"], i["id"]))
    return {"count": total, "pending": out_items[:limit], "churn": churned(ss), "mode": mode}


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


def resolve_serves(conn, explicit: str | None) -> str:
    """The goal a learn serves: *explicit* (``northstar`` or a feature of this wiki), else the fire
    of the claimed feature, else ``none``."""
    from shapa import focus

    if explicit:
        e = explicit.strip()
        if e.lower() == "northstar":
            return "northstar"
        it = db.get_item(conn, e) if re.fullmatch(r"F[1-9]\d*", e) else None
        if it is None or it.kind != "F":
            raise db.LedgerError(f"--serves is northstar or a feature of this repo (F<n>), got {explicit!r}")
        return it.id
    fire_ids = {f.feature for f in focus.load(conn).fires if f and f.feature}
    for item in db.items(conn, status="claimed"):
        cur = item
        while cur is not None and cur.kind != "F":
            cur = db.get_item(conn, cur.parent) if cur.parent else None
        if cur is not None and (cur.id in fire_ids or "fire" in _tags(conn, cur.id)):
            return cur.id
    return "none"


def _skill_section(body: str, line: str) -> str:
    lines = body.rstrip().splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip().lower() == SKILL_SECTION.lower()), None)
    if idx is None:
        return body.rstrip() + f"\n\n{SKILL_SECTION}\n\n- {line}\n"
    end = next((j for j in range(idx + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    if sum(1 for l in lines[idx + 1:end] if l.startswith("- ")) >= MAX_SECTION_LINES:
        raise db.LedgerError(f"the skill's {SKILL_SECTION!r} section is full ({MAX_SECTION_LINES}): "
                             "fold it into the skill's own text first (`shapa skill update`)")
    last = max(j for j in range(idx, end) if lines[j].strip())
    lines.insert(last + 1, f"- {line}")
    return "\n".join(lines) + "\n"


def _apply_skill(ss: list[Scope], name: str, principle: str, prov: str, authority: bool) -> dict:
    holder = None
    for s in ss:
        c = s.conn()
        try:
            if skills.versions(c, name):
                holder = s
                break
        finally:
            c.close()
    if holder is None:
        raise db.LedgerError(f"no skill {name!r} in scope: start it with `shapa skill add`")
    conn = holder.conn()
    try:
        vs = skills.versions(conn, name)
        base = next((v for v in reversed(vs) if v.status == "active"), vs[-1])
        pt = tokens(principle)
        conflict = False
        for l in base.body.splitlines():
            text = re.sub(r"\s*\([^)]*\)\s*$", "", l.strip(" -*"))
            if len(text) < 12:
                continue
            d = dice(pt, tokens(text))
            if d >= LEX_MERGE and not opposite(principle, text):
                return {"action": "duplicate", "target": f"skill:{name}", "wiki": holder.label,
                        "version": base.version, "status": base.status, "conflicts": []}
            if d >= LEX_TOPIC and opposite(principle, text):
                conflict = True
        body = _skill_section(base.body, f"{principle} ({prov})")
        active = authority and not conflict
        v = skills.add_version(conn, name, base.description, body, active=active, source="learn")
        return {"action": "skill-version", "target": f"skill:{name}", "wiki": holder.label,
                "version": v.version, "status": v.status, "conflicts": ["contradicts a line of the skill"] if conflict else []}
    finally:
        conn.close()


def apply(cwd, issue_id: str, principle: str, *, scope: str = "global", why: str = "",
          into: str | None = None, serves: str | None = None, wiki: str | None = None,
          session: str = "", now: datetime | None = None, embedder="auto") -> dict:
    now = now or datetime.now(timezone.utc)
    ss = scopes(cwd)
    if not ss:
        raise CommandError("no wiki with a database in scope")
    cands = [s for s in ss if wiki in (None, s.label)]
    home = None
    row = None
    for s in cands:
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
        row.tags = sorted(tags)
        if "learned" in tags:
            return {"issue": row.id, "wiki": home.label, "status": "already-learned",
                    "target": tag_of(tags, "generalized-into:")}
        if "correction" not in tags:
            raise db.LedgerError(f"{row.id} is not a correction")
        if "meta-learning" in tags and meta_applied_week(hconn, now) >= META_PER_WEEK:
            raise db.LedgerError(f"learning-system corrections are applied at most {META_PER_WEEK} a week: "
                                 f"{row.id} stays recorded and pending until the week turns")
        sc = scope.strip()
        skill_name = None
        if into and into.startswith("skill:"):
            skill_name = into[6:]
        elif sc.startswith("skill:"):
            skill_name = sc[6:]
        elif sc not in ("global", "repo"):
            raise db.LedgerError("--scope is global (the default), repo (with --why), or skill:NAME")
        if skill_name is None and sc == "repo":
            why = " ".join(str(why or "").split())
            if len(why) < 8:
                raise db.LedgerError("--scope repo is the exception: say why this holds only in this repo (--why TEXT)")
        principle = clean_principle(principle)
        why = clean_principle(why, limit=240) if why else ""
        serve = resolve_serves(hconn, serves) if home.label == "repo" else (
            "northstar" if (serves or "").lower() == "northstar" else "none")
        text = inert(issue_text(row))
        date = (row.created or db.now_iso(now))[:10]
        prov = f"{row.id}, {inert(home.key)[:40]}, {date}"
        if skill_name is not None:
            res = _apply_skill(ss, skill_name, principle, prov, operator_authority(home.root, row))
            res["scope"] = f"skill:{skill_name}"
        else:
            target = next((s for s in ss if s.label == sc), None)
            if target is None:
                raise CommandError(f"no {sc} wiki with a database in scope")
            res = _apply_rule(ss, target, home, row, principle, text, why, serve, into, now, embedder)
            res["scope"] = sc
        # the issue is learned, once, with the goal it serves; so are its still-unlearned repeats
        group = [row.id]
        for r in _unlearned(hconn):
            if r.id != row.id and tag_of(r.tags, "repeat-of:") == row.id:
                group.append(r.id)
        tgt = res["target"] if res["target"].startswith("skill:") else _ref(home, next(
            s for s in ss if s.label == res["wiki"]), res["target"])
        stamp = db.now_iso(now)
        for gid in group:
            db.retag(hconn, gid, add=["learned", f"learned-at:{stamp}", f"generalized-into:{tgt}", f"serves:{serve}"])
        if not res["target"].startswith("skill:") and res["wiki"] == home.label:
            for gid in group:
                db.link(hconn, gid, res["target"], "related")
        res.update(issue=row.id, learned=True, serves=serve, also_learned=group[1:], principle=principle)
        metrics.record(home.root, "learn", session=session, ref=row.id, val=0.0 if serve == "none" else 1.0,
                       data={"serves": serve, "target": tgt}, now=now)
        return res
    finally:
        hconn.close()


def _apply_rule(ss, target: Scope, home: Scope, row: db.Row, principle: str, text: str, why: str,
                serve: str, into: str | None, now: datetime, embedder) -> dict:
    """File *principle* in *target*'s wiki: merge into the rule that already holds it (evidence
    appended), else a new principle row; a contradicted rule gets a ``conflicts`` link."""
    conn = target.conn()
    try:
        rules = {r.id: r for r in db.rows(conn, "R")}
        chosen, conflicts = None, []
        pt = tokens(principle)

        def contradicts(rule: db.Row) -> bool:
            return any(dice(pt, tokens(x)) >= LEX_TOPIC and opposite(principle, x) for x in statements_of(rule))

        if into:
            if into not in rules:
                raise db.LedgerError(f"no rule {into!r} in the {target.label} wiki")
            chosen = rules[into]
            if contradicts(chosen):
                raise db.LedgerError(f"the principle contradicts {chosen.id}: drop --into to file it as a new "
                                     f"principle linked `conflicts`, or edit {chosen.id} yourself")
        else:
            entries = [e for e in load_entries([target]) if e.kind == "R"]
            for n in Index(entries, embedder).nearest(principle, 3, kinds=("R",)):
                if not (n.merge or n.lex >= LEX_TOPIC):
                    continue
                if contradicts(rules[n.entry.id]):
                    conflicts.append(n.entry.id)
                elif n.merge:
                    chosen = rules[n.entry.id]
                    break
        if chosen is not None and "churn" in _tags(conn, chosen.id):
            hc = home.conn()
            try:
                db.retag(hc, row.id, add=["blocked"])
            finally:
                hc.close()
            raise db.LedgerError(f"{chosen.id} needs enforcement, not rewording: it was revised {REVISION_LIMIT} "
                                 "times and is still violated. Ask the operator for a hook or a test; "
                                 "no fourth revision is filed")
        stamp = db.now_iso(now)
        wiki_key = inert(home.key).replace(" ", "-")[:40] or "repo"
        key = f"{row.id} {wiki_key} {(row.created or stamp)[:10]}"
        entry_line = f"- {key}: {text[:140]}"
        if chosen is None:
            head = [f"Why: {why}"] if why else []
            rid, created = db.add_row(conn, "R", principle, _join_body(head, [entry_line]),
                                      tags=["principle", f"serves:{serve}", "evidence:1", f"last-seen:{stamp}"],
                                      source="learn", now=now)
            if created:
                for c in conflicts[:3]:
                    db.link(conn, rid, c, "conflicts")
                return {"action": "created", "target": rid, "wiki": target.label, "conflicts": conflicts[:3]}
            chosen = db.get_row(conn, rid)   # the identical principle is already stored
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
        db.update_row(conn, chosen.id, body=_join_body(head, ev))
        outstanding = _count(tg, "violations:") > _count(tg, "revisions:")
        mapping = {"evidence:": max(n_evidence, len(ev)), "last-seen:": stamp}
        if outstanding:
            mapping["revisions:"] = _count(tg, "revisions:") + 1
        _set_tags(conn, chosen.id, mapping, extra=["principle", f"serves:{serve}"])
        return {"action": "merged", "target": chosen.id, "wiki": target.label, "conflicts": [],
                "revision": outstanding}
    finally:
        conn.close()


def principles(cwd=None) -> list[dict]:
    """Every principle in scope with what it records: its source corrections (id, wiki key, date, text),
    why (a repo-scoped one), times violated and revised, last seen, the goals it serves, and, on this
    device, how often session start served it."""
    out = []
    for s in scopes(cwd):
        conn = s.conn()
        try:
            served = {}
            lc = metrics._events_conn(s.root, create=False)
            if lc is not None:
                try:
                    served = {r[0]: (r[1], r[2]) for r in lc.execute("SELECT id, n, last FROM p_served")}
                finally:
                    lc.close()
            for rid, in conn.execute("SELECT id FROM tags WHERE tag = 'principle'").fetchall():
                r = db.get_row(conn, rid)
                if r is None or r.kind != "R":
                    continue
                tg = set(r.tags)
                head, ev = _split_body(r.body)
                out.append({
                    "id": r.id, "wiki": s.label, "summary": r.summary,
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
    for s in scopes(cwd):
        if wiki not in (None, s.label):
            continue
        conn = s.conn()
        try:
            r = db.get_row(conn, issue_id)
            if r is not None and r.kind == "I":
                db.retag(conn, r.id, add=["dismissed"])
                return {"issue": r.id, "wiki": s.label, "status": "dismissed"}
        finally:
            conn.close()
    raise CommandError(f"no issue {issue_id!r} in scope")


# --- 3. session start -----------------------------------------------------------------------------------

def principle_candidates(conn, label: str, root) -> list[dict]:
    """The wiki's live principles (not the churned ones), cheap: tags and summaries only."""
    out, tags = [], {}
    for rid, t in conn.execute("SELECT id, tag FROM tags WHERE id IN (SELECT id FROM tags WHERE tag = 'principle')"):
        tags.setdefault(rid, set()).add(t)
    for rid, tg in tags.items():
        r = conn.execute("SELECT summary FROM mri WHERE id = ? AND kind = 'R'", (rid,)).fetchone()
        if r is None or "churn" in tg:
            continue
        out.append({"id": rid, "wiki": label, "root": str(root), "text": r[0],
                    "violations": _count(tg, "violations:"), "evidence": _count(tg, "evidence:"),
                    "serves": tag_of(tg, "serves:") or "none"})
    return out


def rank_principles(cands: list[dict], fire_text: str, fire_ids: set, k: int = PRINCIPLES_SHOWN) -> list[dict]:
    ft = tokens(fire_text)
    scored = []
    for c in cands:
        pt = tokens(c["text"])
        overlap = len(pt & ft) / max(1, len(pt))
        score = 4 * overlap + min(c["violations"], 3) + 0.3 * min(c["evidence"], 5) \
            + (1.0 if c["serves"] in fire_ids or c["serves"] == "northstar" else 0.0)
        if score > 0:
            scored.append((-score, c["wiki"] != "repo", c["id"], c))
    return [c for *_, c in sorted(scored, key=lambda x: x[:3])[:k]]


def drift(conn, root, now: datetime | None = None, churn: list | None = None) -> list[str]:
    """Why the work may be off its goal (empty when fine): jobs closing outside every fire, a stalled
    fire, a principle that needs enforcement."""
    from shapa import focus

    now = now or datetime.now(timezone.utc)
    out = []
    try:
        f = focus.load(conn)
        if any(x and x.feature for x in f.fires):
            recs = metrics.work_records(conn, root)
            cut = db.now_iso(now - timedelta(days=DRIFT_DAYS))
            closed = [r for r in recs.values() if r["kind"] == "J" and r["closed"] and r["closed"] >= cut]
            off = [r for r in closed if not r["fire"]]
            if len(closed) >= DRIFT_MIN_JOBS and len(off) / len(closed) > DRIFT_OFF_SHARE:
                out.append(f"drift: {len(off)} of {len(closed)} jobs closed in {DRIFT_DAYS} days sat outside every fire")
            stalled = metrics.stalled_fires(conn, recs, now)
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


# --- the CLI ---------------------------------------------------------------------------------------------

def _print_pending(res: dict) -> None:
    if not res["pending"] and not res["churn"]:
        print("no corrections await learning")
        return
    for i in res["pending"]:
        flags = [x for x in ("repeat", "violation", "meta", "capped", "blocked") if i.get(x)]
        also = f" (+{len(i['also'])} repeats: {', '.join(i['also'])})" if i["also"] else ""
        print(f"{i['id']} [{i['wiki']}/{i['key']}] {i['summary']}{also}" + (f"  <{', '.join(flags)}>" if flags else ""))
        for n in i["nearest"][:3]:
            print(f"    near {n['ref']} ({n['kind']}, {n['score']:.2f}{', merge' if n['merge'] else ''}): {n['text'][:90]}")
    for c in res["churn"]:
        print(f"{c['wiki']}/{c['id']} {c['summary']}  <{c['note']}; revised {c['revisions']}x, violated {c['violations']}x>")


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
    ap.add_argument("--into", default=None, help="merge into this rule (R<n>) or skill:NAME")
    ap.add_argument("--serves", default=None, help="F<n> or northstar (default: the claimed fire)")
    ap.add_argument("--wiki", choices=["repo", "global"], default=None, help="the wiki holding the issue")
    ap.add_argument("--session", default="")
    ap.add_argument("--json", action="store_true")
    di = add("dismiss", help="a correction that implies no general principle")
    di.add_argument("issue")
    di.add_argument("--wiki", choices=["repo", "global"], default=None)
    pr = add("principles", help="the principles, with their evidence, violations and serves")
    pr.add_argument("--json", action="store_true")
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
        res = apply(args.cwd, args.issue, args.principle, scope=args.scope, why=args.why, into=args.into,
                    serves=args.serves, wiki=args.wiki, session=args.session)
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
        print(f"{res['issue']} dismissed")
        return 0
    res = pending(args.cwd, session=args.session, limit=max(1, args.limit))
    if args.cmd in (None, "pending") and getattr(args, "json", False):
        print(json.dumps(res, sort_keys=True))
    else:
        _print_pending(res)
    return 0


def main(argv: list[str] | None = None) -> None:
    run(run_learn, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
