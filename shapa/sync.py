"""Two-way sync of one wiki with the hosted store (``.shapa/arch/hosted.md``).

``sync_wiki`` runs the contract's four client steps under a per-wiki file lock:

1. **Snapshot** the wiki: F/J/T items, M/R/I rows, D documents, N note files.
2. **Classify** against the stored base: new, modified (or stale), deleted
   (a local tombstone), missing (gone with no tombstone: a database swapped
   by git heals from the server instead of pushing deletes).
3. **Send** puts (parents first) and deletes in batches, then page through
   what the server has since the cursor.
4. **Apply** in ONE local write transaction: renames first, then each change,
   skipping any record whose local content changed since the snapshot (a write
   that landed during the network call is never clobbered), then counters,
   acknowledged tombstones and the new state.

The server's answers are untrusted: results and changes are validated
(``shapa.syncrec``), note paths re-checked, and an inconsistent response
aborts the sync with the local data untouched.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from shapa import db, dbsync, ledger, syncapply, syncrec, syncstate
from shapa.remotehttp import Client, RemoteError, clean_text

BATCH_OPS = 200
BATCH_BYTES = 4_000_000  # the server refuses a request over 8 MiB
MAX_RECORD_BYTES = syncrec.MAX_RECORD_BYTES
MAX_PAGES = 200
MAX_CHANGES = 20_000
STATUSES = ("accepted", "noop", "rejected", "renamed")


@dataclass
class SyncResult:
    key: str
    status: str = "ok"  # ok | unchanged | busy | skipped | error
    sent_put: int = 0
    sent_del: int = 0
    received: int = 0
    applied: int = 0
    renamed: int = 0
    skipped: int = 0
    invalid: int = 0
    wanted: int = 0
    message: str = ""
    dry_run: bool = False
    oversized: list = field(default_factory=list)  # ids skipped: over the server limit for a record
    notes_skipped: bool = False  # the checkout is off its default branch: no note files synced

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Plan:
    puts: list = field(default_factory=list)   # (record, base_rev)
    dels: list = field(default_factory=list)   # wire del ops
    wants: list = field(default_factory=list)  # ids to re-fetch
    oversized: list = field(default_factory=list)  # ids too big to send
    unchanged: int = 0

    @property
    def pending(self) -> int:
        return len(self.puts) + len(self.dels) + len(self.wants)


def wire_put(rec: dict) -> dict:
    return {k: rec[k] for k in ("id", "uid", "kind", "created", "updated", "rev", "deleted",
                                "device", "data", "tags", "links")}


def too_big(rec: dict) -> bool:
    return len(json.dumps(wire_put(rec), separators=(",", ":"))) > MAX_RECORD_BYTES


# --- step 2: classify -----------------------------------------------------------------------

def classify(local: dict[str, dict], hashes: dict[str, str], base: dict[str, dict],
             tombstones: dict[str, dict], notes: bool = True) -> Plan:
    plan = Plan()

    def put(rec, base_rev):
        if too_big(rec):
            plan.oversized.append(rec["id"])
        else:
            plan.puts.append((rec, base_rev))

    for rid in sorted(local, key=lambda i: (syncrec.KIND_ORDER.index(local[i]["kind"]), len(i), i)):
        rec, ent = local[rid], base.get(rid)
        if ent is None or ent["uid"] != rec["uid"]:
            put(rec, None)
        elif hashes[rid] == ent["hash"]:
            plan.unchanged += 1
        elif rec["updated"] and ent["updated"] and rec["updated"] < ent["updated"]:
            plan.wants.append(rid)  # an older copy of what the server already has
        else:
            put(rec, ent["rev"])
    here = {r["uid"] for r in local.values() if r["uid"]}
    for rid, ent in base.items():
        is_note = not db.ID_RE.match(rid)
        if (is_note and not notes) or (rid in local and local[rid]["uid"] == ent["uid"]):
            continue
        if ent["uid"] and ent["uid"] in here:
            continue  # the record is still here, under another label
        stone = tombstones.get(rid)
        if stone and not is_note and stone["uid"] == ent["uid"]:
            plan.dels.append({"op": "del", "id": rid, "uid": ent["uid"],
                              "deleted_at": stone["deleted_at"], "base_rev": ent["rev"]})
        else:
            plan.wants.append(rid)
    return plan


# --- step 3: send -------------------------------------------------------------------------------

def _batches(plan: Plan):
    """``(ops, want)`` pairs: puts then deletes in order, each request within
    the op and byte limits; wants ride along 200 per request."""
    ops_all = [{"op": "put", "base_rev": b, "record": wire_put(r)} for r, b in plan.puts]
    ops_all += plan.dels
    chunks, cur, size = [], [], 0
    for op in ops_all:
        n = len(json.dumps(op, separators=(",", ":")))
        if cur and (len(cur) >= BATCH_OPS or size + n > BATCH_BYTES):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(op)
        size += n
    if cur:
        chunks.append(cur)
    wants = [plan.wants[i:i + BATCH_OPS] for i in range(0, len(plan.wants), BATCH_OPS)]
    return list(itertools.zip_longest(chunks, wants, fillvalue=[])) or [([], [])]


def _op_id(op: dict) -> str:
    return op["record"]["id"] if op["op"] == "put" else op["id"]


def _check_results(ops: list, results) -> list[dict]:
    if not isinstance(results, list) or len(results) != len(ops):
        raise RemoteError("the server's results do not match the request")
    out = []
    for op, res in zip(ops, results):
        if not (isinstance(res, dict) and res.get("status") in STATUSES):
            raise RemoteError("the server sent a malformed result")
        rev = res.get("rev", 0)
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
            raise RemoteError("the server sent a malformed result")
        new_id = res.get("new_id")
        kind = _op_id(op)[:1]
        if res["status"] == "renamed" and new_id is None:
            raise RemoteError("the server sent an invalid rename")
        if new_id is not None and (op["op"] != "put" or kind == "N"
                                   or not syncrec.id_matches_kind(new_id, kind)):
            raise RemoteError("the server sent an invalid rename")
        out.append({"status": res["status"], "rev": rev, "new_id": new_id})
    return out


def _check_counters(raw) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if k in dbsync.COUNTER_KINDS and isinstance(v, int) and not isinstance(v, bool)
            and 1 <= v <= 10**9}


@dataclass
class Exchange:
    results: list = field(default_factory=list)  # (op, result)
    changes: dict = field(default_factory=dict)  # id -> validated record
    counters: dict = field(default_factory=dict)
    cursor: int = 0
    received: int = 0
    invalid: int = 0


def _ingest(ex: Exchange, resp: dict) -> bool:
    """Fold one response into *ex*; returns its ``more`` flag."""
    changes = resp.get("changes")
    if not isinstance(changes, list):
        raise RemoteError("the server sent a malformed response")
    for raw in changes[:MAX_CHANGES]:
        ex.received += 1
        rec = syncrec.validate_record(raw)
        if rec is None:
            ex.invalid += 1
        else:
            ex.changes[rec["id"]] = rec
    cursor = resp.get("cursor")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        raise RemoteError("the server sent a malformed cursor")
    ex.cursor = cursor
    ex.counters.update(_check_counters(resp.get("counters")))
    return resp.get("more") is True


def exchange(client: Client, key: str, plan: Plan, cursor: int, counters: dict) -> Exchange:
    ex = Exchange(cursor=cursor)
    path = f"/v1/wikis/{key}/sync"
    for ops, want in _batches(plan):
        resp = client.request("POST", path, {"cursor": ex.cursor, "ops": ops, "want": want,
                                             "counters": counters})
        ex.results.extend(zip(ops, _check_results(ops, resp.get("results"))))
        more = _ingest(ex, resp)
    pages = 0
    while more and pages < MAX_PAGES:
        before = ex.cursor
        resp = client.request("POST", path, {"cursor": ex.cursor, "ops": [], "want": [],
                                             "counters": counters})
        more = _ingest(ex, resp) and ex.cursor != before
        pages += 1
    return ex


# --- the whole thing -------------------------------------------------------------------------------

def notes_enabled(wiki_dir: Path) -> bool:
    """Note files sync only while the wiki primary checkout is on its default
    branch; a wiki outside any git checkout always syncs them."""
    checkout = wiki_dir.parent
    if not (checkout / ".git").exists():
        return True
    try:
        top = ledger._git(checkout, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != checkout.resolve():
            return True
        head = ledger._git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
        return head.returncode == 0 and head.stdout.strip() == ledger.default_branch(checkout)
    except (OSError, subprocess.SubprocessError):
        return False


def _snapshot(wiki_dir: Path, conn, notes: bool):
    local = dbsync.snapshot(conn)
    if notes:
        local.update(syncrec.snapshot_notes(wiki_dir))
    return local, syncrec.record_hashes(local)


def _open_readonly(db_file: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def pending_changes(root, *, key: str, url: str, rdir: Path) -> int | None:
    """How many records a sync would send or re-fetch (no network); ``None``
    when the wiki has no database."""
    db_file = db.db_path(root)
    if not db_file.is_file():
        return None
    conn = _open_readonly(db_file)
    try:
        notes = notes_enabled(db_file.parent)
        local, hashes = _snapshot(db_file.parent, conn, notes)
        tombs = {t["id"]: t for t in db.tombstones(conn)}
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    state = syncstate.load_state(syncstate.state_path(rdir, db_file), key, url)
    return classify(local, hashes, state["base"], tombs, notes).pending


def sync_wiki(root, *, client: Client, rdir: Path, key: str, name: str = "", kind: str = "repo",
              origin: str = "", dry_run: bool = False, if_changed: bool = False,
              wait: float = 30.0) -> SyncResult:
    """Sync one wiki; never raises for a server, network or data problem (the
    result carries ``status`` and ``message``)."""
    result = SyncResult(key=key, dry_run=dry_run)
    db_file = db.db_path(root)
    if not db_file.is_file():
        result.status, result.message = "skipped", "no database (run `shapa upgrade`)"
        return result
    wiki_dir = db_file.parent
    path = syncstate.state_path(rdir, db_file)
    try:
        with syncstate.wiki_lock(path, wait) as got:
            if not got:
                result.status, result.message = "busy", "another sync holds this wiki"
                return result
            return _locked_sync(result, root, wiki_dir, db_file, path, client, key, name, kind,
                                origin, dry_run, if_changed)
    except RemoteError as exc:
        result.status, result.message = "error", clean_text(exc)
    except (sqlite3.Error, OSError) as exc:
        result.status, result.message = "error", f"local problem: {clean_text(exc)}"
    return result


def _locked_sync(result, root, wiki_dir, db_file, path, client, key, name, kind, origin,
                 dry_run, if_changed) -> SyncResult:
    state = syncstate.load_state(path, key, client.url)
    conn = _open_readonly(db_file) if dry_run else db.connect(root)
    try:
        if not dry_run:
            db.backfill_uids(conn)  # a record from before uids, or from an older shapa
        notes = notes_enabled(wiki_dir)
        result.notes_skipped = not notes
        local, hashes = _snapshot(wiki_dir, conn, notes)
        tombs = dbsync.local_tombstones(conn)
        counters = dbsync.counters(conn)
        plan = classify(local, hashes, state["base"], tombs, notes)
        plan.wants = sorted(set(plan.wants) | set(state.get("retry", [])))
        result.sent_put, result.sent_del, result.wanted = len(plan.puts), len(plan.dels), len(plan.wants)
        result.oversized = list(plan.oversized)
        if if_changed and not plan.pending:
            result.status = "unchanged"
            return result
        if dry_run:
            try:
                ex = exchange(client, key, Plan(wants=plan.wants), state["cursor"], counters)
            except RemoteError as exc:
                if exc.status != 404:
                    raise
                result.message = "the wiki is not on the server yet"
                return result
            result.received, result.invalid = ex.received, ex.invalid
            return result
        if not state.get("registered"):
            client.request("PUT", f"/v1/wikis/{key}", {"name": name or key, "kind": kind,
                                                       "origin": origin})
            state["registered"] = True
        ex = exchange(client, key, plan, state["cursor"], counters)
        result.received, result.invalid = ex.received, ex.invalid
        left = client.remaining()
        if left is not None and left <= 0:
            raise RemoteError("time budget exhausted before the changes were applied")
        syncapply.apply(conn, wiki_dir, local, hashes, state, ex, result, notes)
        syncstate.save_state(path, state)
        return result
    finally:
        conn.close()

