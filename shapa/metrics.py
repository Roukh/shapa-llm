"""``shapa metrics`` - measure whether the wiki is making the work better.

Raw events (a session start, a prompt, a correction, a repeat, a learn, a skill
use, a feature or job opening or closing, a fetch or bootstrap latency) are
recorded per wiki in the derived, gitignored ``.shapa-index.db``: per device,
like use counts, never synced and never committed. Everything that can be read
from rows (corrections, their repeat and learned tags, principles, skills, the
ledger) is read from the rows, so another device's work counts too.

Bounded by construction (the sweep calls :func:`prune`):

- raw events live :data:`RAW_DAYS` days, at most :data:`MAX_EVENTS` rows; before
  an old day's events are dropped, that day is folded into a daily snapshot;
- a daily snapshot is one small row per day and repo in ``metrics_daily``
  (shapa.db schema 2, additive, never synced); the global wiki's table also
  holds a ``*`` row per day: the roll-up across repos.

Frustration score of one operator message (deterministic, 0 to 10):

======================================  ======
a repeat cue (again, still, already,    +2 each, at most 3 cues
 "I said", "every time", "same mistake")
a profanity word (list below)           +3 each, at most 2
an ALL-CAPS word of 4+ letters          +1 each, at most 3 (acronyms excluded)
``!!`` or ``?!``                        +1
======================================  ======

A correction that is a repeat adds 3 more when the window's index is computed
(``frustration_mean`` = (message scores + 3 per repeat) / messages).

CLI::

    shapa metrics [--since DATE] [--json] [--snapshot] [--all] [--repo KEY] [--history N]
    shapa metrics record skill-use NAME --session ID
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shapa import config, db, store

RAW_DAYS = 60
MAX_EVENTS = 20_000
SNAPSHOT_DAYS = 400          # daily snapshot rows kept per repo
SNAPSHOT_BUDGET_MS = 50      # an automatic snapshot that costs more is skipped, tried next session
INDEX_BUSY_S = 1.0           # the hook path never waits longer than this on the derived index
LATENCY_OPS = ("fetch", "bootstrap")
FIRE_TAG = "fire"
STALL_DAYS = 7
REPEAT_FRUSTRATION = 3.0
ONCE_KINDS = ("job-open", "job-close", "feature-open", "feature-close")
EVENT_KINDS = ("session", "prompt", "correction", "repeat", "learn", "skill-use", "latency") + ONCE_KINDS

_EVENTS_SQL = (
    "CREATE TABLE IF NOT EXISTS m_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
    "kind TEXT NOT NULL, session TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '', "
    "val REAL, data TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS m_events_kind_ts ON m_events(kind, ts)",
    "CREATE UNIQUE INDEX IF NOT EXISTS m_events_once ON m_events(kind, ref) "
    "WHERE kind IN ('job-open','job-close','feature-open','feature-close')",
    # how often session start served each principle on this device (shapa.learn)
    "CREATE TABLE IF NOT EXISTS p_served (id TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0, last TEXT)",
    "CREATE TABLE IF NOT EXISTS m_meta (key TEXT PRIMARY KEY, value TEXT)",
)

# --- frustration ----------------------------------------------------------------------

_REPEAT_CUES = re.compile(
    r"\b(again|still|already|yet again|once more|every (?:single )?time|"
    r"i (?:just )?(?:said|told you|asked)|as i (?:said|told)|like i (?:said|told)|"
    r"how many times|for the (?:\w+ )?time|same (?:mistake|thing|problem|error)|"
    r"keep (?:doing|making|forgetting|getting|ignoring))\b", re.I)
_PROFANITY = frozenset({"fuck", "fucking", "fucked", "shit", "shitty", "bullshit", "damn", "dammit",
                        "goddamn", "wtf", "ffs", "omfg", "crap", "idiot", "stupid", "useless"})
_ACRONYMS = frozenset({"README", "JSON", "HTTP", "HTTPS", "HTML", "CSS", "SQL", "API", "YAML", "TOML",
                       "NULL", "TRUE", "FALSE", "TODO", "NOTE", "URL", "UUID", "UTF", "ASCII", "CORS",
                       "CSRF", "JWT", "SSH", "LLM", "MCP", "CLI", "NPM", "PNPM", "DONE", "WIP", "CI",
                       "OAUTH", "REST", "GRPC", "TCP", "HTTPS", "DNS", "TLS", "SSL", "EOF", "UTC"})
_CAPS = re.compile(r"\b[A-Z]{4,}\b")
_BANG = re.compile(r"!{2,}|\?!|!\?")
_WORD = re.compile(r"[a-z']+")


def frustration(text: str) -> tuple[float, list[str]]:
    """``(score, flags)`` of one operator message; see the module docstring."""
    text = str(text or "")
    flags: list[str] = []
    score = 0.0
    cues = {m.group(0).lower() for m in _REPEAT_CUES.finditer(text)}
    if cues:
        score += 2 * min(len(cues), 3)
        flags.append("repeat-cue")
    swear = [w for w in _WORD.findall(text.lower()) if w in _PROFANITY]
    if swear:
        score += 3 * min(len(swear), 2)
        flags.append("profanity")
    caps = [w for w in _CAPS.findall(text) if w not in _ACRONYMS]
    if caps:
        score += min(len(caps), 3)
        flags.append("caps")
    if _BANG.search(text):
        score += 1
        flags.append("bang")
    return min(score, 10.0), flags


# --- time -----------------------------------------------------------------------------------

def _utc(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value) -> datetime | None:
    return db._parse_iso(value) if value else None


def parse_since(text: str) -> datetime:
    try:
        return datetime.strptime(text.strip()[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise db.LedgerError(f"--since is a date, YYYY-MM-DD (got {text!r})")


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


# --- where events live -----------------------------------------------------------------------

def repo_key(root) -> str:
    """The wiki's repo name (the primary checkout's directory), ``global`` for the global wiki."""
    root = Path(root)
    try:
        if root.resolve() == config.global_root().resolve():
            return "global"
    except OSError:
        pass
    return db.db_path(root).parent.parent.name or "repo"


def home_root(cwd=None) -> Path | None:
    """The wiki a command in *cwd* records its events in: the repo wiki with a
    database, else the global wiki's. ``None`` when neither exists (nothing is created)."""
    try:
        roots = config.wiki_roots(cwd)
    except Exception:
        return None
    for kind in ("repo", "global"):
        for wr in roots:
            if wr.kind == kind and db.exists(wr.path):
                return Path(wr.path)
    return None


def open_local(root, *, create: bool = True, timeout: float = INDEX_BUSY_S) -> sqlite3.Connection | None:
    """The wiki's derived index file with the learning and metrics tables, waiting at most *timeout*
    seconds on a lock (a corrupt file is set aside by ``store.open_index`` and rebuilt once)."""
    root = Path(root)
    if not create and not store.db_path(root).is_file():
        return None
    if not root.is_dir():
        return None
    path = store.db_path(root)
    for attempt in (1, 2):
        try:
            conn = sqlite3.connect(path, timeout=timeout)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
            for stmt in _EVENTS_SQL:
                conn.execute(stmt)
            conn.commit()
            return conn
        except sqlite3.OperationalError:
            raise
        except sqlite3.DatabaseError:
            if attempt == 2:
                raise
            try:
                conn.close()
            except Exception:
                pass
            store.open_index(root).close()   # sets a malformed file aside and rebuilds it


_events_conn = open_local


def record(root, kind: str, *, session: str = "", ref: str = "", val: float | None = None,
           data=None, now: datetime | None = None) -> bool:
    """Append one raw event to *root*'s index. Never raises; creates nothing when
    *root* has no database (a read on a machine with no wiki must leave no trace)."""
    try:
        if root is None or not db.exists(root):
            return False
        conn = _events_conn(root)
        if conn is None:
            return False
        try:
            blob = data if isinstance(data, str) else (json.dumps(data, sort_keys=True) if data else "")
            conn.execute("INSERT OR IGNORE INTO m_events(ts, kind, session, ref, val, data) "
                         "VALUES (?,?,?,?,?,?)",
                         (_iso(_utc(now)), kind, str(session or "")[:8], str(ref or "")[:80], val, blob[:400]))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def events(root, kind: str | None = None, since: str | None = None, until: str | None = None) -> list[dict]:
    conn = _events_conn(root, create=False)
    if conn is None:
        return []
    try:
        sql, args = "SELECT ts, kind, session, ref, val, data FROM m_events WHERE 1=1", []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if since:
            sql += " AND ts >= ?"
            args.append(since)
        if until:
            sql += " AND ts < ?"
            args.append(until)
        return [dict(r) for r in conn.execute(sql + " ORDER BY ts, id", args)]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def record_latency(cwd, op: str, ms: float, *, now: datetime | None = None) -> bool:
    return record(home_root(cwd), "latency", ref=op, val=round(float(ms), 3), now=now)


def record_prompt(root, session: str, text: str, *, now: datetime | None = None) -> bool:
    score, flags = frustration(text)
    return record(root, "prompt", session=session, val=score, data={"flags": flags} if flags else None, now=now)


# --- the ledger as records (items now, plus what the sweep archived) --------------------------

def fire_ids(conn: sqlite3.Connection, heal: bool = False) -> set[str]:
    """Features that are, or ever were, a fire: the ``fire`` tag, plus the focus record's current
    fires (the hosted store sets a fire without the tag). With *heal* the tag is written for those."""
    from shapa import focus

    ids = {r[0] for r in conn.execute("SELECT id FROM tags WHERE tag = ?", (FIRE_TAG,))}
    try:
        now_fires = {f.feature for f in focus.load(conn).fires if f and f.feature}
    except Exception:
        now_fires = set()
    if heal:
        for fid in now_fires - ids:
            if db.get_item(conn, fid) is not None:
                db.retag(conn, fid, add=[FIRE_TAG])
    return ids | now_fires


def harvest(conn: sqlite3.Connection, root) -> int:
    """Archive every feature and job of the ledger as open/close events, once each
    (the sweep deletes closed items, so this runs before it)."""
    fire_tagged = fire_ids(conn, heal=True)
    rows = []
    for it in db.items(conn):
        if it.kind not in ("F", "J"):
            continue
        parent = it.parent or ""
        base = {"parent": parent, "fire": (it.id in fire_tagged) if it.kind == "F" else (parent in fire_tagged)}
        name = "feature" if it.kind == "F" else "job"
        rows.append((it.created, f"{name}-open", it.id, json.dumps(base, sort_keys=True)))
        if it.status == "closed" and it.closed:
            rows.append((it.closed, f"{name}-close", it.id, json.dumps(
                {**base, "created": it.created, "reason": it.closed_reason or ""}, sort_keys=True)))
    if not rows:
        return 0
    try:
        ev = _events_conn(root)
        if ev is None:
            return 0
        try:
            before = ev.total_changes
            ev.executemany("INSERT OR IGNORE INTO m_events(ts, kind, ref, data) VALUES (?,?,?,?)", rows)
            ev.commit()
            return ev.total_changes - before
        finally:
            ev.close()
    except sqlite3.Error:
        return 0


def _all_events(root, ctx: dict) -> tuple[list, list]:
    if "ev" not in ctx:
        conn = open_local(root, create=False)
        rows = []
        if conn is not None:
            try:
                rows = [tuple(r) for r in conn.execute(
                    "SELECT ts, kind, session, ref, val, data FROM m_events ORDER BY ts, id")]
            except sqlite3.Error:
                rows = []
            finally:
                conn.close()
        ctx["ev"], ctx["ev_ts"] = rows, [r[0] for r in rows]
    return ctx["ev"], ctx["ev_ts"]


def _window(root, ctx: dict, s: str, u: str) -> list:
    ev, ts = _all_events(root, ctx)
    return ev[bisect.bisect_left(ts, s):bisect.bisect_left(ts, u)]


def _json(blob) -> dict:
    try:
        return json.loads(blob) if blob else {}
    except ValueError:
        return {}


def work_records(conn: sqlite3.Connection, root, ctx: dict | None = None) -> dict[str, dict]:
    """id -> {kind, parent, created, closed, fire}: archived events overlaid by live items."""
    ctx = {} if ctx is None else ctx
    if "recs" in ctx:
        return ctx["recs"]
    out: dict[str, dict] = {}
    for ts, k, _, ref, _, data in _all_events(root, ctx)[0]:
        if k not in ONCE_KINDS:
            continue
        d = _json(data)
        rec = out.setdefault(ref, {"kind": "F" if k.startswith("feature") else "J",
                                   "parent": d.get("parent") or None, "created": None,
                                   "closed": None, "fire": bool(d.get("fire"))})
        if k.endswith("open"):
            rec["created"] = ts
        else:
            rec["closed"] = ts
            rec["created"] = rec["created"] or d.get("created")
    fire_tagged = fire_ids(conn)
    for it in db.items(conn):
        if it.kind not in ("F", "J"):
            continue
        out[it.id] = {"kind": it.kind, "parent": it.parent, "created": it.created,
                      "closed": it.closed if it.status == "closed" else None,
                      "fire": (it.id in fire_tagged) if it.kind == "F" else bool(it.parent in fire_tagged)}
    ctx["recs"] = out
    return out


def closed_jobs_since(conn: sqlite3.Connection, root, cut: str) -> list[dict]:
    """Jobs closed at or after *cut*: ``{id, parent, closed, fire}``, from two indexed queries (the live
    items and the archived close events), not a scan of the ledger. What session start's drift line reads."""
    fire = fire_ids(conn)
    out: dict[str, dict] = {}
    ev = open_local(root, create=False)
    if ev is not None:
        try:
            for ts, ref, data in ev.execute("SELECT ts, ref, data FROM m_events WHERE kind = 'job-close' "
                                            "AND ts >= ?", (cut,)):
                d = _json(data)
                out[ref] = {"id": ref, "parent": d.get("parent") or None, "closed": ts, "fire": bool(d.get("fire"))}
        except sqlite3.Error:
            pass
        finally:
            ev.close()
    for jid, parent, closed in conn.execute(
            "SELECT id, parent, closed FROM items WHERE kind = 'J' AND status = 'closed' AND closed >= ?", (cut,)):
        out[jid] = {"id": jid, "parent": parent, "closed": closed, "fire": bool(parent in fire)}
    return list(out.values())


# --- compute -----------------------------------------------------------------------------------

def _tagval(tags, prefix: str) -> str | None:
    for t in sorted(tags):
        if t.startswith(prefix):
            return t[len(prefix):]
    return None


def _days(a: str | None, b: str | None) -> float | None:
    da, dbb = _parse(a), _parse(b)
    if da is None or dbb is None:
        return None
    return max(0.0, (dbb - da).total_seconds() / 86400)


def _empty() -> dict:
    return {"sessions": 0, "prompts": 0, "frustration_sum": 0.0,
            "corrections": {"total": 0, "repeats": 0, "violations": 0, "learned": 0, "pending": 0,
                            "dismissed": 0, "meta": 0},
            "learn": {"applied": 0, "serving": 0, "serving_nothing": 0},
            "skills": {"created": 0, "updated": 0, "activated": 0, "uses": {}, "uses_total": 0},
            "features": {"opened": 0, "closed": 0, "lead_days_sum": 0.0, "lead_n": 0, "jobs_sum": 0},
            "jobs": {"opened": 0, "closed": 0, "closed_on_fire": 0, "closed_off_fire": 0},
            "fires": {"closed": 0, "lead_days_sum": 0.0, "lead_n": 0, "stalled": []},
            "principles": {"total": 0, "violations": 0, "served": 0, "churn": 0},
            "latency": {op: {"n": 0, "p50_ms": None, "p95_ms": None} for op in LATENCY_OPS},
            "events": {}, "records": 0}


_FLAGS = ("repeat", "violation", "meta-learning", "learned", "dismissed", "soft")


def _corrections_in(conn, ctx: dict, s: str, u: str) -> list[tuple]:
    """``(id, session, flags)`` of every correction created in ``[s, u)``: one indexed pass over the
    correction tag (kept in *ctx* for the other windows of a report), then the flags of the ones in the window."""
    if "corr" not in ctx:
        ctx["corr"] = conn.execute("SELECT m.id, m.created, m.session FROM tags c JOIN mri m ON m.id = c.id "
                                   "WHERE c.tag = 'correction'").fetchall()
    inside = {r[0]: r[2] for r in ctx["corr"] if s <= r[1] < u}
    flags: dict[str, set] = {}
    ids = list(inside)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        for rid, tag in conn.execute(f"SELECT id, tag FROM tags WHERE id IN ({marks}) AND tag IN "
                                     f"({','.join('?' * len(_FLAGS))})", [*part, *_FLAGS]):
            flags.setdefault(rid, set()).add(tag)
    return [(rid, inside[rid], flags.get(rid, set())) for rid in ids]


def compute(root, since: datetime, until: datetime, ctx: dict | None = None) -> dict:
    """The raw (additive) measures of one wiki over ``[since, until)``. Everything read from the rows goes
    through the tag index and primary keys, so it costs what the window holds, not what the wiki holds.
    A soft candidate is not a correction until it is learned."""
    from shapa import learn, skills

    root = Path(root)
    own = ctx is None
    ctx = {} if ctx is None else ctx
    s, u = _iso(since), _iso(until)
    c = _empty()
    ev = _window(root, ctx, s, u)
    sessions: set[str] = {e[2] for e in ev if e[2] and e[1] != "latency"}
    for e in ev:
        c["events"][e[1]] = c["events"].get(e[1], 0) + 1
    prompts = [e for e in ev if e[1] == "prompt"]
    c["prompts"] = len(prompts)
    c["frustration_sum"] = round(sum(e[4] or 0.0 for e in prompts), 3)
    for e in ev:
        if e[1] == "skill-use" and e[3]:
            c["skills"]["uses"][e[3]] = c["skills"]["uses"].get(e[3], 0) + 1
            c["skills"]["uses_total"] += 1
    for op in LATENCY_OPS:
        vals = [e[4] for e in ev if e[1] == "latency" and e[3] == op and e[4] is not None]
        c["latency"][op] = {"n": len(vals), "p50_ms": percentile(vals, 50), "p95_ms": percentile(vals, 95)}
    dedup = sum(1 for e in ev if e[1] == "repeat" and '"dedup"' in (e[5] or ""))
    c["corrections"]["total"] += dedup
    c["corrections"]["repeats"] += dedup
    conn = ctx.get("conn") or db.connect(root)
    if conn is None:
        c["sessions"] = len(sessions)
        return c
    try:
        for rid, session, fl in _corrections_in(conn, ctx, s, u):
            learned = "learned" in fl
            if "soft" in fl and not learned:
                continue
            if session:
                sessions.add(session)
            cc = c["corrections"]
            cc["total"] += 1
            cc["repeats"] += "repeat" in fl
            cc["violations"] += "violation" in fl
            cc["meta"] += "meta-learning" in fl
            cc["learned" if learned else ("dismissed" if "dismissed" in fl else "pending")] += 1
        applied = conn.execute("SELECT id, tag FROM tags WHERE tag >= ? AND tag < ?",
                               ("learned-at:" + s, "learned-at:" + u)).fetchall()
        for rid, _ in applied:
            c["learn"]["applied"] += 1
            serves = conn.execute("SELECT tag FROM tags WHERE id = ? AND tag LIKE 'serves:%'", (rid,)).fetchone()
            if serves is None or serves[0] == "serves:none":
                c["learn"]["serving_nothing"] += 1
            else:
                c["learn"]["serving"] += 1
        if "skills" not in ctx:
            ctx["skills"] = skills._all(conn)
        for name, versions in ctx["skills"].items():
            for v in versions:
                if s <= v.created < u:
                    c["skills"]["created" if v.version == 1 else "updated"] += 1
                if v.status == "active" and s <= v.updated < u:
                    c["skills"]["activated"] += 1
        for (pid,) in conn.execute("SELECT id FROM tags WHERE tag = 'principle'").fetchall():
            tg = [r[0] for r in conn.execute("SELECT tag FROM tags WHERE id = ?", (pid,))]
            c["principles"]["total"] += 1
            c["principles"]["violations"] += int(_tagval(tg, "violations:") or 0)
            c["principles"]["churn"] += "churn" in tg
        c["principles"]["served"] = learn.served_total(root)
        c["records"] = live_records(conn)
        recs = work_records(conn, root, ctx)
        children: dict[str, int] = {}
        for rid, r in recs.items():
            if r["kind"] == "J" and r["parent"]:
                children[r["parent"]] = children.get(r["parent"], 0) + 1
        for rid, r in recs.items():
            if r["kind"] == "J":
                c["jobs"]["opened"] += bool(r["created"] and s <= r["created"] < u)
                if r["closed"] and s <= r["closed"] < u:
                    c["jobs"]["closed"] += 1
                    c["jobs"]["closed_on_fire" if r["fire"] else "closed_off_fire"] += 1
            else:
                c["features"]["opened"] += bool(r["created"] and s <= r["created"] < u)
                if r["closed"] and s <= r["closed"] < u:
                    c["features"]["closed"] += 1
                    c["features"]["jobs_sum"] += children.get(rid, 0)
                    lead = _days(r["created"], r["closed"])
                    if lead is not None:
                        c["features"]["lead_days_sum"] += lead
                        c["features"]["lead_n"] += 1
                    if r["fire"]:
                        c["fires"]["closed"] += 1
                        if lead is not None:
                            c["fires"]["lead_days_sum"] += lead
                            c["fires"]["lead_n"] += 1
        c["fires"]["stalled"] = stalled_fires(conn, root, until)
    finally:
        if "conn" not in ctx:
            conn.close()
    c["sessions"] = len(sessions)
    c["features"]["lead_days_sum"] = round(c["features"]["lead_days_sum"], 4)
    c["fires"]["lead_days_sum"] = round(c["fires"]["lead_days_sum"], 4)
    return c


def live_records(conn) -> int:
    """The wiki's live record count: rows, documents and open ledger items (three counts, no row read)."""
    n = conn.execute("SELECT COUNT(*) FROM mri").fetchone()[0]
    n += conn.execute("SELECT COUNT(*) FROM items WHERE status != 'closed'").fetchone()[0]
    if db._has_table(conn, "docs"):
        n += conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    return n


def stalled_fires(conn, root, now: datetime) -> list[str]:
    """Fire features still open that have had no job closed in :data:`STALL_DAYS`
    days (a fire younger than that is not stalled)."""
    from shapa import focus

    cut = _iso(now - timedelta(days=STALL_DAYS))
    out = []
    try:
        f = focus.load(conn)
    except Exception:
        return out
    fires = [x.feature for x in f.fires if x and x.feature]
    if not fires:
        return out
    recent = {j["parent"] for j in closed_jobs_since(conn, root, cut)}
    for fid in fires:
        item = db.get_item(conn, fid)
        if item is None or item.status == "closed" or item.created >= cut:
            continue
        if fid not in recent:
            out.append(fid)
    return out


def _avg(num, n):
    return round(num / n, 4) if n else None


def derive(c: dict) -> dict:
    """Ratios from the additive measures."""
    cor, ln, jobs, feat = c["corrections"], c["learn"], c["jobs"], c["features"]
    settled = cor["learned"] + cor["pending"]
    return {
        "corrections_per_session": _avg(cor["total"], c["sessions"]),
        "repeat_rate": _avg(cor["repeats"], cor["total"]),
        "learned_ratio": _avg(cor["learned"], settled),
        "frustration_mean": _avg(c["frustration_sum"] + REPEAT_FRUSTRATION * cor["repeats"], c["prompts"]),
        "fetch_p95_ms": c["latency"]["fetch"]["p95_ms"],
        "bootstrap_p95_ms": c["latency"]["bootstrap"]["p95_ms"],
        "feature_lead_days": _avg(feat["lead_days_sum"], feat["lead_n"]),
        "jobs_per_feature": _avg(feat["jobs_sum"], feat["closed"]),
        "fire_lead_days": _avg(c["fires"]["lead_days_sum"], c["fires"]["lead_n"]),
        "off_fire_share": _avg(jobs["closed_off_fire"], jobs["closed"]),
        "serving_nothing_share": _avg(ln["serving_nothing"], ln["applied"]),
        "skill_uses": c["skills"]["uses_total"],
    }


#: metric -> True when a lower number is better.
DIRECTION = {"corrections_per_session": True, "repeat_rate": True, "learned_ratio": False,
             "frustration_mean": True, "fetch_p95_ms": True, "bootstrap_p95_ms": True,
             "feature_lead_days": True, "off_fire_share": True, "serving_nothing_share": True,
             "skill_uses": False}


def trend(root, now: datetime | None = None, days: int = 7, ctx: dict | None = None) -> dict:
    """The last *days* days against the *days* before them."""
    now = _utc(now)
    ctx = {} if ctx is None else ctx
    cur = derive(compute(root, now - timedelta(days=days), now, ctx))
    prev = derive(compute(root, now - timedelta(days=2 * days), now - timedelta(days=days), ctx))
    out = {}
    for k, lower in DIRECTION.items():
        a, b = cur.get(k), prev.get(k)
        better = None if a is None or b is None or a == b else ((a < b) if lower else (a > b))
        out[k] = {"current": a, "previous": b,
                  "delta": None if a is None or b is None else round(a - b, 4), "better": better}
    return out


# --- merging (the roll-up across repos) ---------------------------------------------------------

def merge(a: dict, b: dict) -> dict:
    """Sum two additive measure sets (latency: n-weighted p50, the larger p95)."""
    out = json.loads(json.dumps(a))
    for k, v in b.items():
        if k == "latency":
            for op, lat in v.items():
                mine = out["latency"].setdefault(op, {"n": 0, "p50_ms": None, "p95_ms": None})
                n = mine["n"] + lat["n"]
                if lat["n"]:
                    p50 = lat["p50_ms"] if not mine["n"] else (mine["p50_ms"] * mine["n"] + lat["p50_ms"] * lat["n"]) / n
                    mine["p50_ms"] = round(p50, 3)
                    mine["p95_ms"] = max(x for x in (mine["p95_ms"], lat["p95_ms"]) if x is not None)
                mine["n"] = n
        elif k == "skills":
            for kk, vv in v.items():
                if kk == "uses":
                    for name, n in vv.items():
                        out["skills"]["uses"][name] = out["skills"]["uses"].get(name, 0) + n
                else:
                    out["skills"][kk] += vv
        elif k == "fires":
            for kk, vv in v.items():
                if kk == "stalled":
                    out["fires"]["stalled"] = sorted(set(out["fires"]["stalled"]) | set(vv))
                else:
                    out["fires"][kk] += vv
        elif isinstance(v, dict):
            for kk, vv in v.items():
                out[k][kk] = out[k].get(kk, 0) + vv
        elif k == "records":   # a gauge: the largest wiki, never a sum over days
            out[k] = max(out.get(k, 0), v)
        else:
            out[k] += v
    return out


#: Latency must have risen at least this much, and at least as fast as the data, to be flagged.
LATENCY_FLAG_GROWTH = 1.5


def latency_scale(root, now: datetime | None = None, days: int = 7) -> dict:
    """Fetch latency against the wiki's record count, from the daily snapshots: the last *days*
    days against the *days* before. Falling or flat latency per record is good; latency that rose
    materially (:data:`LATENCY_FLAG_GROWTH`) at least as fast as the data is flagged."""
    now = _utc(now)
    rows = snapshot_rows(root, repo=repo_key(root))

    def window(a: datetime, b: datetime) -> dict:
        lo, hi = a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")
        sel = [d for day, _, d in rows if lo <= day < hi]
        p95 = [x["latency"]["fetch"]["p95_ms"] for x in sel if x.get("latency", {}).get("fetch", {}).get("n")]
        p50 = [x["latency"]["fetch"]["p50_ms"] for x in sel if x.get("latency", {}).get("fetch", {}).get("n")]
        rec = [x.get("records") for x in sel if x.get("records")]
        mean = lambda v: round(sum(v) / len(v), 3) if v else None
        w = {"fetch_p50_ms": mean(p50), "fetch_p95_ms": mean(p95), "records": mean(rec), "days": len(sel)}
        w["p95_per_1k_records"] = (round(w["fetch_p95_ms"] / w["records"] * 1000, 4)
                                   if w["fetch_p95_ms"] is not None and w["records"] else None)
        return w

    cur = window(now - timedelta(days=days - 1), now + timedelta(days=1))
    prev = window(now - timedelta(days=2 * days - 1), now - timedelta(days=days - 1))
    out = {"current": cur, "previous": prev, "latency_growth": None, "data_growth": None, "flag": False}
    if cur["fetch_p95_ms"] and prev["fetch_p95_ms"] and cur["records"] and prev["records"]:
        out["latency_growth"] = round(cur["fetch_p95_ms"] / prev["fetch_p95_ms"], 3)
        out["data_growth"] = round(cur["records"] / prev["records"], 3)
        out["flag"] = (out["latency_growth"] >= LATENCY_FLAG_GROWTH
                       and out["latency_growth"] >= out["data_growth"])
    return out


# --- snapshots ------------------------------------------------------------------------------------

def _snapshot_conn(root) -> sqlite3.Connection | None:
    return db.connect(root, busy_ms=1000) if db.exists(root) else None


def _write_snapshot(root, day: str, repo: str, data: dict) -> bool:
    conn = _snapshot_conn(root)
    if conn is None:
        return False
    try:
        with db.write(conn):
            conn.execute("INSERT OR REPLACE INTO metrics_daily(day, repo, data) VALUES (?,?,?)",
                         (day, repo, json.dumps(data, sort_keys=True)))
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def snapshot_rows(root, since_day: str | None = None, until_day: str | None = None,
                  repo: str | None = None) -> list[tuple[str, str, dict]]:
    conn = _snapshot_conn(root)
    if conn is None:
        return []
    try:
        sql, args = "SELECT day, repo, data FROM metrics_daily WHERE 1=1", []
        if since_day:
            sql += " AND day >= ?"
            args.append(since_day)
        if until_day:
            sql += " AND day < ?"
            args.append(until_day)
        if repo:
            sql += " AND repo = ?"
            args.append(repo)
        out = []
        for day, rp, data in conn.execute(sql + " ORDER BY day, repo", args):
            try:
                out.append((day, rp, json.loads(data)))
            except ValueError:
                continue
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def snapshot(root, now: datetime | None = None, day: datetime | None = None, data: dict | None = None) -> str:
    """Store today's (or *day*'s) snapshot of *root*, here and in the global wiki
    (with the roll-up row ``*`` there). Returns the day."""
    now = _utc(now)
    start = _utc(day or now).replace(hour=0, minute=0, second=0, microsecond=0)
    end = min(start + timedelta(days=1), now + timedelta(seconds=1)) if day is None else start + timedelta(days=1)
    key = repo_key(root)
    data = data if data is not None else compute(root, start, end)
    d = start.strftime("%Y-%m-%d")
    _write_snapshot(root, d, key, data)
    try:
        groot = config.global_root()
        if db.exists(groot) and Path(groot).resolve() != Path(root).resolve():
            _write_snapshot(groot, d, key, data)
            total = None
            for _, rp, row in snapshot_rows(groot, d, (start + timedelta(days=1)).strftime("%Y-%m-%d")):
                if rp != "*":
                    total = row if total is None else merge(total, row)
            if total is not None:
                _write_snapshot(groot, d, "*", total)
    except OSError:
        pass
    return d


def prune(root, now: datetime | None = None) -> dict:
    """Fold every day whose raw events are about to be dropped into a snapshot, then drop events older
    than :data:`RAW_DAYS` and any beyond :data:`MAX_EVENTS`, and keep at most :data:`SNAPSHOT_DAYS`
    daily snapshots per repo. The sweep calls this. A year costs at most 365 snapshot rows a repo
    (and as many roll-up rows in the global wiki), and a bounded event table."""
    now = _utc(now)
    cut = _iso(now - timedelta(days=RAW_DAYS))
    out = {"snapshots": 0, "dropped": 0, "snapshots_dropped": 0}
    conn = open_local(root, create=False)
    if conn is not None:
        try:
            days = sorted({r[0] for r in conn.execute(
                "SELECT DISTINCT substr(ts, 1, 10) FROM m_events WHERE ts < ?", (cut,))})
            have = {d for d, rp, _ in snapshot_rows(root, repo=repo_key(root))}
        finally:
            conn.close()
        for d in days:
            if d not in have:
                day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                snapshot(root, now, day=day)
                out["snapshots"] += 1
        conn = open_local(root, create=False)
        try:
            cur = conn.execute("DELETE FROM m_events WHERE ts < ?", (cut,))
            out["dropped"] += cur.rowcount
            over = conn.execute("SELECT COUNT(*) FROM m_events").fetchone()[0] - MAX_EVENTS
            if over > 0:
                conn.execute("DELETE FROM m_events WHERE id IN (SELECT id FROM m_events ORDER BY ts, id LIMIT ?)", (over,))
                out["dropped"] += over
            conn.commit()
        finally:
            conn.close()
    old = (now - timedelta(days=SNAPSHOT_DAYS)).strftime("%Y-%m-%d")
    for target in {Path(root), Path(config.global_root())}:
        if db.exists(target):
            c2 = db.connect(target)
            try:
                with db.write(c2):
                    out["snapshots_dropped"] += c2.execute("DELETE FROM metrics_daily WHERE day < ?", (old,)).rowcount
            except sqlite3.Error:
                pass
            finally:
                c2.close()
    return out


SNAP_RETRY_S = 600           # a snapshot that did not happen is tried again after this long


def _meta_set(root, key: str, value: str) -> None:
    conn = open_local(root)
    if conn is not None:
        try:
            conn.execute("INSERT OR REPLACE INTO m_meta(key, value) VALUES (?, ?)", (key, value))
            conn.commit()
        finally:
            conn.close()


def session_start_done(root, session: str, ms: float, now: datetime | None = None) -> bool:
    """What a session start records after its output, on ONE connection to the derived index: its latency
    and the session event. Returns whether today's snapshot is due (none stored today, and none tried in
    the last :data:`SNAP_RETRY_S` seconds). Never raises."""
    try:
        if root is None or not db.exists(root):
            return False
        now = _utc(now)
        ts, day = _iso(now), now.strftime("%Y-%m-%d")
        conn = open_local(root)
        if conn is None:
            return False
        try:
            conn.execute("INSERT OR IGNORE INTO m_events(ts, kind, session, ref, val, data) VALUES (?,?,?,?,?,'')",
                         (ts, "latency", "", "bootstrap", round(float(ms), 3)))
            conn.execute("INSERT OR IGNORE INTO m_events(ts, kind, session, ref, val, data) VALUES (?,?,?,?,NULL,'')",
                         (ts, "session", str(session or "")[:8], ""))
            got = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM m_meta WHERE key IN ('snap_day','snap_try')")}
            due = got.get("snap_day") != day and (got.get("snap_try") or "") < _iso(now - timedelta(seconds=SNAP_RETRY_S))
            if due:
                conn.execute("INSERT OR REPLACE INTO m_meta(key, value) VALUES ('snap_try', ?)", (ts,))
            conn.commit()
        finally:
            conn.close()
        return due
    except Exception:
        return False


def home_of(roots) -> Path | None:
    """:func:`home_root` for wiki roots already resolved (the repo wiki with a database, else the global one)."""
    for kind in ("repo", "global"):
        for wr in roots:
            if wr.kind == kind and db.exists(wr.path):
                return Path(wr.path)
    return None


def snapshot_in_background(root, cwd=None, now: datetime | None = None) -> None:
    """Store today's snapshot without making the session start wait for it: a detached ``shapa metrics
    --auto-snapshot`` (the first session of a day pays one process start, not the snapshot). With
    ``SHAPA_INLINE_SNAPSHOT`` set (the tests) it runs here instead. Never raises."""
    try:
        if os.environ.get("SHAPA_INLINE_SNAPSHOT"):
            auto_snapshot(root, now)
            return
        import subprocess

        subprocess.Popen([sys.executable, "-m", "shapa", "metrics", "--auto-snapshot", "--cwd",
                          str(cwd or Path(root).parent)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except Exception:
        pass


def auto_snapshot(root, now: datetime | None = None) -> bool:
    """Store today's snapshot on the first session of the day, when it costs under
    :data:`SNAPSHOT_BUDGET_MS`; a dearer one is skipped and tried again next session. Never raises."""
    try:
        now = _utc(now)
        day = now.strftime("%Y-%m-%d")
        if any(d == day for d, _, _ in snapshot_rows(root, day, (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                                                     repo=repo_key(root))):
            _meta_set(root, "snap_day", day)
            return False
        t0 = time.perf_counter()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        data = compute(root, start, now + timedelta(seconds=1))
        if (time.perf_counter() - t0) * 1000 > SNAPSHOT_BUDGET_MS:
            return False
        snapshot(root, now, data=data)
        _meta_set(root, "snap_day", day)
        return True
    except Exception:
        return False


# --- the report ------------------------------------------------------------------------------------

def report(root, since: datetime, until: datetime) -> dict:
    conn = db.connect(root)
    ctx = {"conn": conn} if conn is not None else {}
    try:
        c = compute(root, since, until, ctx)
        return {"repo": repo_key(root), "since": _iso(since), "until": _iso(until),
                "measures": c, "derived": derive(c), "trend": trend(root, until, ctx=ctx),
                "scale": latency_scale(root, until)}
    finally:
        if conn is not None:
            conn.close()


def rollup(groot, since: datetime, until: datetime, repo: str | None = None) -> dict:
    """The measures of every repo (or one) from the global wiki's daily snapshots."""
    total, repos = None, set()
    for day, rp, data in snapshot_rows(groot, since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d"),
                                       repo=repo):
        if repo is None and rp == "*":
            continue
        repos.add(rp)
        total = data if total is None else merge(total, data)
    total = total if total is not None else _empty()
    return {"repo": repo or "*", "repos": sorted(repos), "since": _iso(since), "until": _iso(until),
            "measures": total, "derived": derive(total)}


def _fmt(v) -> str:
    return "-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def render(rep: dict) -> str:
    d, m = rep["derived"], rep["measures"]
    lines = [f"shapa metrics [{rep['repo']}] {rep['since'][:10]} .. {rep['until'][:10]}"
             + (f" (repos: {', '.join(rep['repos'])})" if rep.get("repos") else "")]
    cor = m["corrections"]
    lines += [
        f"  sessions {m['sessions']}, corrections {cor['total']} ({_fmt(d['corrections_per_session'])}/session), "
        f"repeats {cor['repeats']} (rate {_fmt(d['repeat_rate'])}), violations {cor['violations']}",
        f"  learning: learned {cor['learned']}, pending {cor['pending']}, dismissed {cor['dismissed']}; "
        f"applied {m['learn']['applied']} (serving nothing {m['learn']['serving_nothing']})",
        f"  frustration {_fmt(d['frustration_mean'])} over {m['prompts']} messages",
        f"  latency p50/p95 ms: fetch {_fmt(m['latency']['fetch']['p50_ms'])}/{_fmt(d['fetch_p95_ms'])}, "
        f"bootstrap {_fmt(m['latency']['bootstrap']['p50_ms'])}/{_fmt(d['bootstrap_p95_ms'])}",
        f"  skills: created {m['skills']['created']}, updated {m['skills']['updated']}, "
        f"activated {m['skills']['activated']}, uses {m['skills']['uses_total']}",
        f"  work: features closed {m['features']['closed']} (lead {_fmt(d['feature_lead_days'])} d, "
        f"{_fmt(d['jobs_per_feature'])} jobs each); jobs closed {m['jobs']['closed']}, "
        f"off-fire share {_fmt(d['off_fire_share'])}",
        f"  fires: closed {m['fires']['closed']} (lead {_fmt(d['fire_lead_days'])} d), "
        f"stalled {', '.join(m['fires']['stalled']) or 'none'}",
        f"  principles: {m['principles']['total']}, violations {m['principles']['violations']}, "
        f"served {m['principles']['served']}, churn {m['principles']['churn']}",
    ]
    sc = rep.get("scale")
    if sc and sc["current"]["records"]:
        cu, pv = sc["current"], sc["previous"]
        lines.append(f"  scale: {_fmt(cu['records'])} records, fetch p95 {_fmt(cu['fetch_p95_ms'])} ms "
                     f"({_fmt(cu['p95_per_1k_records'])} ms per 1k records; before: {_fmt(pv['records'])} records, "
                     f"{_fmt(pv['p95_per_1k_records'])})" + ("  FLAG: latency rising with the data" if sc["flag"] else ""))
    for k, t in (rep.get("trend") or {}).items():
        if t["current"] is not None or t["previous"] is not None:
            mark = {True: "better", False: "worse", None: "flat"}[t["better"]]
            lines.append(f"  trend {k}: {_fmt(t['previous'])} -> {_fmt(t['current'])} ({mark})")
    return "\n".join(lines)


def run_metrics(argv: list[str]) -> int:
    from shapa.ledger import CommandError, scoped_root

    if argv and argv[0] == "record":
        p = argparse.ArgumentParser(prog="shapa metrics record")
        p.add_argument("event", choices=["skill-use"])
        p.add_argument("name")
        p.add_argument("--session", default="")
        p.add_argument("--cwd", default=None)
        a = p.parse_args(argv[1:])
        from shapa.skills import NAME_RE

        if not NAME_RE.match(a.name):
            raise CommandError(f"a skill name is a slug (got {a.name!r})")
        record(home_root(a.cwd), "skill-use", session=a.session, ref=a.name)
        return 0
    p = argparse.ArgumentParser(prog="shapa metrics", description="Measure the wiki's effect over time.")
    p.add_argument("--cwd", default=None)
    p.add_argument("--since", default=None, help="window start, YYYY-MM-DD (default: 7 days ago)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--snapshot", action="store_true", help="store today's daily snapshot")
    p.add_argument("--auto-snapshot", action="store_true",
                   help="what a session start launches: today's snapshot if none and it is cheap (silent)")
    p.add_argument("--all", action="store_true", help="roll up every repo from the global wiki's snapshots")
    p.add_argument("--repo", default=None, help="one repo's snapshots from the global wiki")
    p.add_argument("--history", type=int, default=None, metavar="DAYS", help="list the stored daily snapshots")
    a = p.parse_args(argv)
    now = _utc()
    since = parse_since(a.since) if a.since else now - timedelta(days=7)
    root = home_root(a.cwd)
    if a.auto_snapshot:
        auto_snapshot(root) if root is not None else None
        return 0
    if a.all or a.repo:
        groot = config.global_root()
        if not db.exists(groot):
            raise CommandError("no global wiki database: the roll-up lives there")
        rep = rollup(groot, since, now + timedelta(days=1), repo=a.repo)
    else:
        if root is None:
            rep = {"repo": "-", "since": _iso(since), "until": _iso(now), "measures": _empty(),
                   "derived": derive(_empty()), "trend": {}}
        else:
            if a.snapshot:
                conn = db.connect(root)
                try:
                    harvest(conn, root)
                finally:
                    conn.close()
                snapshot(root, now)
                prune(root, now)
            rep = report(root, since, now)
    if a.history is not None:
        groot = root if root is not None else config.global_root()
        rows = snapshot_rows(groot, (now - timedelta(days=a.history)).strftime("%Y-%m-%d"))
        if a.json:
            print(json.dumps([{"day": d, "repo": r, "records": x.get("records"),
                               "fetch_p50_ms": x["latency"]["fetch"]["p50_ms"],
                               "fetch_p95_ms": x["latency"]["fetch"]["p95_ms"], "derived": derive(x)}
                              for d, r, x in rows]))
        else:
            for d, r, x in rows:
                print(f"{d} {r} records={x.get('records')} fetch p50/p95={x['latency']['fetch']['p50_ms']}/"
                      f"{x['latency']['fetch']['p95_ms']} {json.dumps(derive(x), sort_keys=True)}")
        return 0
    print(json.dumps(rep, sort_keys=True) if a.json else render(rep))
    return 0


def main(argv: list[str] | None = None) -> None:
    from shapa.ledger import run

    run(run_metrics, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
