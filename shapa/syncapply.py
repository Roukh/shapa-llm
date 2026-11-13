"""Step 4 of the hosted sync: apply the server's answers to the wiki.

Everything database-side happens in ONE write transaction (relabels first, then
acknowledgements, then each change, then counters). Records are matched by uid,
never by label: an incoming record whose uid is known here under another id
relabels the local record to the incoming id, and a different local record that
holds the incoming id moves to a fresh label first, so a change never
overwrites another record. A change is applied only when the local record is
unchanged since the snapshot the plan was built from, so a write that landed
during the network call is never clobbered; skipped ids are asked for again on
the next sync (``state["retry"]``). Note files are written atomically and only
through ``shapa.syncrec``'s path checks, and only while notes are enabled.
"""

from __future__ import annotations

from pathlib import Path

from shapa import db, dbsync, syncrec, syncstate


def key_of(rec: dict) -> str:
    """A record's identity key: its uid, or its path for a note."""
    return rec["uid"] or rec["id"]


class _Apply:
    """The state one apply step threads through its helpers."""

    def __init__(self, conn, wiki_dir, local, hashes, state, ex, result, notes):
        self.conn, self.wiki_dir, self.state, self.ex, self.result = conn, wiki_dir, state, ex, result
        self.notes = notes
        self.base = state["base"]
        self.snap = {key_of(local[i]): h for i, h in hashes.items()}
        self.refresh()
        self.written: list[dict] = []
        self.retry: set[str] = set()  # skipped incoming records: asked for again next sync
        self.stone_uids: set[str] = set()

    def refresh(self) -> None:
        """Re-read the live database: what "changed since the snapshot" is judged against."""
        self.cur = {key_of(r): r for r in dbsync.snapshot(self.conn).values()}
        self.cur_hash = syncrec.record_hashes(self.cur)
        self.by_id = {r["id"]: k for k, r in self.cur.items()}

    def skip(self, rid: str) -> None:
        self.result.skipped += 1
        self.retry.add(rid)

    def unchanged_locally(self, key: str) -> bool:
        return key in self.snap and self.cur_hash.get(key) == self.snap[key]

    def forget(self, uid: str) -> None:
        for rid in [i for i, ent in self.base.items() if ent["uid"] == uid]:
            del self.base[rid]

    # -- labels ------------------------------------------------------------------------------------
    def relabel(self, key: str, new: str) -> None:
        """Give the local record *key* the label *new*; whatever else holds *new* moves first."""
        old = self.cur[key]["id"]
        if old == new:
            return
        if self.by_id.get(new, key) != key:
            self.relabel(self.by_id[new], dbsync.free_id(self.conn, new[0], avoid=self.base))
        dbsync.relabel(self.conn, old, new)
        ent = self.base.get(old)
        if ent and ent["uid"] == self.cur[key]["uid"]:
            if new in self.base:
                self.retry.add(new)  # that label was another record on the server: fetch it again
            self.base[new] = self.base.pop(old)
        self.refresh()

    def place(self, op, res) -> None:
        """The server says this put's record now lives at ``new_id``."""
        rec, new = op["record"], res["new_id"]
        key = rec["uid"]
        mine = self.cur.get(key)
        if mine is None or mine["kind"] != new[0]:
            self.skip(rec["id"])
            return
        self.relabel(key, new)
        if res["status"] == "renamed":
            self.result.renamed += 1

    def ack(self, op, res) -> None:
        if op["op"] == "put":
            rec = op["record"]
            final = res["new_id"] if res["new_id"] and key_of(rec) in self.cur else rec["id"]
            if res["status"] in ("accepted", "noop", "renamed") and key_of(rec) in self.snap:
                self.base[final] = {"rev": res["rev"], "uid": rec["uid"], "updated": rec["updated"],
                                    "hash": self.snap[key_of(rec)]}
        else:
            dbsync.drop_tombstones(self.conn, [op["id"]])
            if res["status"] in ("accepted", "noop"):
                self.forget(op["uid"])

    # -- changes -----------------------------------------------------------------------------------
    def change(self, rec: dict) -> None:
        if rec["kind"] == "N":
            return self.note_change(rec)
        key, rid = rec["uid"], rec["id"]
        mine = self.cur.get(key)
        if rec["deleted"]:
            if mine is None:
                self.forget(key)
            elif self.unchanged_locally(key):
                db.remove(self.conn, [mine["id"]], tombstone=False, keep_inbound=True)
                self.forget(key)
                self.refresh()
            else:
                self.skip(rid)
            return
        if key in self.stone_uids or (mine is not None and mine["kind"] != rec["kind"]):
            self.skip(rid)  # deleted here and not sent yet, or a uid changing kind
            return
        if mine is not None and mine["id"] != rid:
            self.relabel(key, rid)  # the server says this record lives at rid
            mine = self.cur[key]
        elif mine is None and rid in self.by_id:
            self.relabel(self.by_id[rid], dbsync.free_id(self.conn, rid[0], avoid=self.base))
        if mine is not None and not self.unchanged_locally(key):
            self.skip(rid)  # edited since the snapshot: pushed next time
            return
        if mine is None or syncrec.record_hash(rec) != self.cur_hash[key]:
            dbsync.upsert(self.conn, rec)
            self.result.applied += 1
            self.cur[key], self.cur_hash[key] = rec, syncrec.record_hash(rec)
            self.by_id[rid] = key
        self.written.append(rec)

    def note_change(self, rec: dict) -> None:
        rid = rec["id"]
        if not self.notes:
            self.retry.add(rid)  # fetched again once the checkout is back on its default branch
            return
        text = syncrec.note_text(self.wiki_dir, rid)
        was = self.snap.get(rid)
        now = syncrec.record_hash({"data": {"body": text}, "tags": [], "links": []}) \
            if text is not None else None
        if was != now:
            self.skip(rid)  # changed (or created, or removed) since the snapshot
            return
        if rec["deleted"]:
            ent = self.base.get(rid)
            if text is None or (ent and ent["hash"] == now and syncrec.remove_note(self.wiki_dir, rid)):
                self.base.pop(rid, None)
            else:
                self.skip(rid)
            return
        body = rec["data"]["body"]
        try:
            if text != body:
                syncrec.write_note(self.wiki_dir, rid, body)
                self.result.applied += 1
        except (ValueError, OSError):
            self.skip(rid)
            return
        self.written.append(rec)


def apply(conn, wiki_dir: Path, local, hashes, state, ex, result, notes: bool = True) -> None:
    """Step 4. Everything database-side happens in one write transaction."""
    stamp = db.now_iso()
    with db.write(conn):
        conn.execute("PRAGMA defer_foreign_keys = ON")
        ap = _Apply(conn, wiki_dir, local, hashes, state, ex, result, notes)
        for op, res in ex.results:
            if op["op"] == "put" and res["new_id"]:
                ap.place(op, res)
        for op, res in ex.results:
            ap.ack(op, res)
        ap.stone_uids = {t["uid"] for t in db.tombstones(conn)}
        for rec in sorted(ex.changes.values(), key=lambda r: (syncrec.KIND_ORDER.index(r["kind"]),
                                                              len(r["id"]), r["id"])):
            ap.change(rec)
        dbsync.raise_counters(conn, ex.counters)
        final = {key_of(r): r for r in dbsync.snapshot(conn).values()}
        for rec in ap.written:
            if rec["kind"] == "N":
                mine = syncrec.note_text(wiki_dir, rec["id"])
                digest = syncrec.record_hash({"data": {"body": mine or ""}, "tags": [], "links": []})
                where = rec["id"]
            elif rec["uid"] in final:
                digest, where = syncrec.record_hash(final[rec["uid"]]), final[rec["uid"]]["id"]
            else:
                continue
            ap.base[where] = {"rev": rec["rev"], "uid": rec["uid"], "updated": rec["updated"],
                              "hash": digest}
    state["cursor"], state["last_sync"] = ex.cursor, stamp
    state["retry"] = sorted(ap.retry)[:syncstate.MAX_RETRY]
