"""Step 4 of the hosted sync: apply the server's answers to the wiki.

Everything database-side happens in ONE write transaction (renames first, then
acknowledgements, then each change, then counters). A change is applied only
when the local record is unchanged since the snapshot the plan was built from,
so a write that landed during the network call is never clobbered; skipped ids
are asked for again on the next sync (``state["retry"]``). Note files are
written atomically and only through ``shapa.syncrec``'s path checks.
"""

from __future__ import annotations

from pathlib import Path

from shapa import db, dbsync, syncrec, syncstate


class _Apply:
    """The state one apply step threads through its helpers."""

    def __init__(self, conn, wiki_dir, snap_hash, state, ex, result):
        self.conn, self.wiki_dir, self.state, self.ex, self.result = conn, wiki_dir, state, ex, result
        self.base = state["base"]
        self.snap_hash = snap_hash
        self.refresh()
        self.written: list[str] = []
        self.retry: set[str] = set()  # skipped incoming records: asked for again next sync

    def refresh(self) -> None:
        """Re-read the live database: what "changed since the snapshot" is judged against."""
        self.cur = dbsync.snapshot(self.conn)
        self.cur_hash = syncrec.record_hashes(self.cur)

    def rename(self, op, res) -> None:
        old, new = op["record"]["id"], res["new_id"]
        mine = self.cur.get(old)
        if mine is None or mine["created"] != op["record"]["created"] or dbsync.exists(self.conn, new):
            self.skip(old)
            return
        dbsync.relabel(self.conn, old, new)
        self.base.pop(old, None)
        digest = self.snap_hash.pop(old)
        self.snap_hash[new] = digest
        self.base[new] = {"rev": res["rev"], "created": op["record"]["created"],
                          "updated": op["record"]["updated"], "hash": digest}
        self.result.renamed += 1

    def ack(self, op, res) -> None:
        if op["op"] == "put":
            rec = op["record"]
            if res["status"] in ("accepted", "noop") and rec["id"] in self.snap_hash:
                self.base[rec["id"]] = {"rev": res["rev"], "created": rec["created"],
                                        "updated": rec["updated"], "hash": self.snap_hash[rec["id"]]}
        else:
            dbsync.drop_tombstones(self.conn, [op["id"]])
            if res["status"] in ("accepted", "noop"):
                self.base.pop(op["id"], None)

    def skip(self, rid: str) -> None:
        self.result.skipped += 1
        self.retry.add(rid)

    def unchanged_locally(self, rid: str) -> bool:
        return rid in self.snap_hash and self.cur_hash.get(rid) == self.snap_hash[rid]

    def change(self, rec: dict) -> None:
        rid = rec["id"]
        if rec["kind"] == "N":
            return self.note_change(rec)
        mine = self.cur.get(rid)
        if rec["deleted"]:
            if mine is None:
                self.base.pop(rid, None)
            elif mine["created"] == rec["created"] and self.unchanged_locally(rid):
                db.remove(self.conn, [rid], tombstone=False, keep_inbound=True)
                self.cur.pop(rid), self.cur_hash.pop(rid)
                self.base.pop(rid, None)
            else:
                self.skip(rid)
            return
        if mine is not None and mine["created"] != rec["created"]:
            self.skip(rid)  # a different record under this label: never overwrite
            return
        if mine is not None and not self.unchanged_locally(rid):
            self.skip(rid)  # edited since the snapshot: pushed next time
            return
        if mine is None and rid in dbsync.local_tombstones(self.conn):
            self.skip(rid)  # deleted here, the delete is not sent yet
            return
        if mine is None or syncrec.record_hash(rec) != self.cur_hash[rid]:
            dbsync.upsert(self.conn, rec)
            self.result.applied += 1
        self.written.append(rid)

    def note_change(self, rec: dict) -> None:
        rid, text = rec["id"], syncrec.note_text(self.wiki_dir, rec["id"])
        was = self.snap_hash.get(rid)
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
        self.written.append(rid)


def apply(conn, wiki_dir: Path, snap_hash, state, ex, result) -> None:
    """Step 4. Everything database-side happens in one write transaction."""
    stamp = db.now_iso()
    with db.write(conn):
        conn.execute("PRAGMA defer_foreign_keys = ON")
        ap = _Apply(conn, wiki_dir, snap_hash, state, ex, result)
        for op, res in ex.results:
            if res["status"] == "renamed":
                ap.rename(op, res)
        ap.refresh()
        for op, res in ex.results:
            if res["status"] != "renamed":
                ap.ack(op, res)
        for rec in sorted(ex.changes.values(), key=lambda r: (syncrec.KIND_ORDER.index(r["kind"]),
                                                              len(r["id"]), r["id"])):
            ap.change(rec)
        dbsync.raise_counters(conn, ex.counters)
        final = dbsync.snapshot(conn)
        for rid in ap.written:
            rec = ex.changes[rid]
            if rec["kind"] == "N":
                mine = syncrec.note_text(wiki_dir, rid)
                digest = syncrec.record_hash({"data": {"body": mine or ""}, "tags": [], "links": []})
            elif rid in final:
                digest = syncrec.record_hash(final[rid])
            else:
                continue
            ap.base[rid] = {"rev": rec["rev"], "created": rec["created"],
                            "updated": rec["updated"], "hash": digest}
    state["cursor"], state["last_sync"] = ex.cursor, stamp
    state["retry"] = sorted(ap.retry)[:syncstate.MAX_RETRY]
