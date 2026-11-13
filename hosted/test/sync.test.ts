import { describe, expect, it } from "vitest";
import { TS, TS2, TS3, del, get, job, mint, mintAll, put, rec, row, send, setup, sync, uidOf } from "./helpers.js";

const ids = (changes: { id: string }[]) => changes.map((c) => c.id);
const statuses = (r: { body: { results: { status: string }[] } }) => r.body.results.map((x) => x.status);

describe("sync: put", () => {
  it("inserts an unknown id as accepted and reports it back with a server rev", async () => {
    const { token, w } = await setup();
    const r = await sync(token, w, { ops: [put(row("M1", "hello"))] });
    expect(r.status).toBe(200);
    expect(r.body.results).toEqual([{ id: "M1", status: "accepted", rev: expect.any(Number) }]);
    expect(r.body.changes).toHaveLength(1);
    expect(r.body.changes[0]).toMatchObject({ id: "M1", rev: r.body.results[0].rev, device: "tester", deleted: false });
    expect(r.body.more).toBe(false);
    expect(r.body.counters.M).toBe(2);
    // a second pull from the returned cursor has nothing new
    const again = await sync(token, w, { cursor: r.body.cursor });
    expect(again.body.changes).toEqual([]);
  });

  it("never trusts the client for rev or device", async () => {
    const { token, w } = await setup();
    const r = await sync(token, w, { ops: [put({ ...row("M1", "x"), rev: 999999, device: "forged" })] });
    expect(r.body.results[0].rev).not.toBe(999999);
    expect(r.body.changes[0].rev).toBeLessThan(1000);
    expect(r.body.changes[0].device).toBe("tester");
    // forged rev does not make the stored record look newer to a later base_rev check
    const cur = r.body.changes[0].rev;
    const ok = await sync(token, w, { cursor: r.body.cursor, ops: [put(row("M1", "edited", { updated: TS }), cur)] });
    expect(ok.body.results[0].status).toBe("accepted");
  });

  it("renames when the id is held by a record with another created time, tombstones included", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(row("M1", "server one")), put(row("M2", "server two"))] });
    await sync(token, w, { ops: [del("M2", TS, TS2)] });
    const r = await sync(token, w, { ops: [put(row("M1", "client one", { created: TS2, updated: TS2 })), put(row("M2", "client two", { created: TS3, updated: TS3 }))] });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(["renamed", "renamed"]);
    const [a, b] = r.body.results;
    expect(a.new_id).toBe("M3");
    expect(b.new_id).toBe("M4");
    const byId = Object.fromEntries(r.body.changes.map((c: { id: string }) => [c.id, c]));
    expect(byId["M3"].data.summary).toBe("client one");
    expect(byId["M1"]?.data.summary).not.toBe("client one");
    expect(r.body.counters.M).toBe(5);
  });

  it("rewrites later ops that name a renamed id as parent or link dst", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(job("F1", "server feature"))] });
    const r = await sync(token, w, {
      ops: [
        put(job("F1", "client feature", null, { created: TS2, updated: TS2 })),
        put(job("J1", "client job", "F1", { created: TS2, updated: TS2 })),
        put(row("M1", "note", { links: [["F1", "about"], ["J1", "related"]] })),
      ],
    });
    expect(statuses(r)).toEqual(["renamed", "accepted", "accepted"]);
    const newF = r.body.results[0].new_id;
    expect(newF).toBe("F2");
    const byId = Object.fromEntries(r.body.changes.map((c: { id: string }) => [c.id, c]));
    expect(byId["J1"].data.parent).toBe(newF);
    expect(byId["M1"].links).toEqual([[newF, "about"], ["J1", "related"]]);
    // the server's own F1 is untouched
    expect(byId["F1"]?.data.title).toBe("server feature");
  });

  it("replaces on base_rev, even with an older updated time", async () => {
    const { token, w } = await setup();
    const first = await sync(token, w, { ops: [put(row("M1", "v1", { updated: TS3 }))] });
    const rev = first.body.results[0].rev;
    const r = await sync(token, w, { cursor: first.body.cursor, ops: [put(row("M1", "v2", { updated: TS }), rev)] });
    expect(r.body.results[0].status).toBe("accepted");
    expect(r.body.changes[0].data.summary).toBe("v2");
  });

  it("is a noop for equal content", async () => {
    const { token, w } = await setup();
    const first = await sync(token, w, { ops: [put(row("M1", "same"))] });
    const rev = first.body.results[0].rev;
    const r = await sync(token, w, { cursor: first.body.cursor, ops: [put(row("M1", "same"), rev), put(row("M1", "same"), null)] });
    expect(r.body.results).toEqual([{ id: "M1", status: "noop", rev }, { id: "M1", status: "noop", rev }]);
    expect(r.body.changes).toEqual([]);
  });

  it("lets a strictly newer updated win without base_rev, and rejects equal or older with the current record", async () => {
    const { token, w } = await setup();
    const first = await sync(token, w, { ops: [put(row("M1", "server", { updated: TS2 }))] });
    const cursor = first.body.cursor;
    const older = await sync(token, w, { cursor, ops: [put(row("M1", "older", { updated: TS }), 0)] });
    expect(older.body.results[0].status).toBe("rejected");
    expect(older.body.changes.map((c: { id: string; data: { summary: string } }) => [c.id, c.data.summary])).toEqual([["M1", "server"]]);
    const equal = await sync(token, w, { cursor, ops: [put(row("M1", "equal", { updated: TS2 }), 0)] });
    expect(equal.body.results[0].status).toBe("rejected");
    const newer = await sync(token, w, { cursor, ops: [put(row("M1", "newer", { updated: TS3 }), 0)] });
    expect(newer.body.results[0].status).toBe("accepted");
    expect(newer.body.changes[0].data.summary).toBe("newer");
  });

  it("stores notes with a derived alias and summary and refuses unsafe note ids", async () => {
    const { token, w } = await setup();
    const note = { id: "research/topic.md", uid: "", kind: "N", created: "", updated: TS, deleted: false, data: { body: "---\nid: topic-x\n---\n# A topic\ntext" }, tags: [], links: [] };
    const r = await sync(token, w, { ops: [put(note)] });
    expect(r.body.results[0].status).toBe("accepted");
    expect(r.body.changes[0].data).toMatchObject({ alias: "topic-x", summary: "A topic" });
    expect((await get(rec(w, "/topic-x"), token)).body.id).toBe("research/topic.md");
    const bad = ["../x.md", "/abs.md", "a\\b.md", "x\u0000.md", "AGENTS.md", "placement.md", "arch/../x.md", "other/x.md", "x.txt"];
    const out = await sync(token, w, { cursor: r.body.cursor, ops: bad.map((id) => put({ ...note, id })) });
    expect(out.body.results.every((x: { status: string }) => x.status === "rejected")).toBe(true);
    expect(out.body.results[0].error).toMatch(/note id/);
    expect(out.body.changes).toEqual([]);
  });

  it("rejects an invalid record without failing the rest of the request", async () => {
    const { token, w } = await setup();
    const r = await sync(token, w, {
      ops: [put(row("M1", "good")), put({ id: "M2", kind: "M" }), put(row("M3", "")), put(row("J9", "kind mismatch", { kind: "M" })), put(row("M5", "bad time", { updated: "yesterday" })), put(row("M6", "good too"))],
    });
    expect(statuses(r)).toEqual(["accepted", "rejected", "rejected", "rejected", "rejected", "accepted"]);
    expect(r.body.results[1].error).toBeTruthy();
    expect(ids(r.body.changes)).toEqual(["M1", "M6"]);
  });

  it("refuses a put whose parent has the wrong kind", async () => {
    const { token, w } = await setup();
    const r = await sync(token, w, { ops: [put(job("J1", "j", "J5")), put(job("T1", "t", "F1"))] });
    expect(statuses(r)).toEqual(["rejected", "rejected"]);
  });

  it("drops an alias another live record already holds", async () => {
    const { token, w } = await setup();
    const a = { ...row("M1", "first"), data: { summary: "first", body: "", alias: "shared", source: "", session: "" } };
    const b = { ...row("M2", "second"), data: { summary: "second", body: "", alias: "shared", source: "", session: "" } };
    const r = await sync(token, w, { ops: [put(a), put(b)] });
    expect(statuses(r)).toEqual(["accepted", "accepted"]);
    const byId = Object.fromEntries(r.body.changes.map((c: { id: string }) => [c.id, c]));
    expect(byId["M1"].data.alias).toBe("shared");
    expect(byId["M2"].data.alias).toBeNull();
  });
});

describe("sync: del", () => {
  it("is a noop for a missing, already deleted or differently created record", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(row("M1", "x"))] });
    const r = await sync(token, w, { ops: [del("M7", TS, TS2), del("M1", TS3, TS3)] });
    expect(statuses(r)).toEqual(["noop", "noop"]);
    await sync(token, w, { ops: [del("M1", TS, TS2)] });
    const again = await sync(token, w, { ops: [del("M1", TS, TS3)] });
    expect(statuses(again)).toEqual(["noop"]);
  });

  it("tombstones on base_rev, or when the record is not newer than the delete", async () => {
    const { token, w } = await setup();
    const a = await sync(token, w, { ops: [put(row("M1", "a", { updated: TS2 })), put(row("M2", "b", { updated: TS2 }))] });
    const [r1, r2] = a.body.results;
    const byRev = await sync(token, w, { cursor: a.body.cursor, ops: [del("M1", TS, TS, r1.rev)] });
    expect(byRev.body.results[0].status).toBe("accepted");
    expect(byRev.body.changes[0]).toMatchObject({ id: "M1", deleted: true, data: {}, tags: [], links: [], created: TS });
    const byTime = await sync(token, w, { cursor: byRev.body.cursor, ops: [del("M2", TS, TS3, r2.rev + 100)] });
    expect(byTime.body.results[0].status).toBe("accepted");
    expect((await get(rec(w, "/M1"), token)).status).toBe(404);
    // a tombstone is delivered to a client pulling from the start
    const all = await sync(token, w, { cursor: 0 });
    expect(all.body.changes.filter((c: { deleted: boolean }) => c.deleted).map((c: { id: string }) => c.id)).toEqual(["M1", "M2"]);
  });

  it("rejects when a later edit beats the delete, returning the current record", async () => {
    const { token, w } = await setup();
    const a = await sync(token, w, { ops: [put(row("M1", "edited later", { updated: TS3 }))] });
    const r = await sync(token, w, { cursor: a.body.cursor, ops: [del("M1", TS, TS2, 0)] });
    expect(r.body.results[0]).toMatchObject({ status: "rejected", rev: a.body.results[0].rev });
    expect(r.body.changes[0]).toMatchObject({ id: "M1", deleted: false });
  });

  it("needs the delete scope, for a del op and for a put that carries the tombstone flag", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(row("M1", "keep"))] });
    const rw = await mint("writer", ["read", "write"]);
    const r = await sync(rw.token, w, { ops: [del("M1", TS, TS3), put(row("M1", "", { deleted: true, updated: TS3 }))] });
    expect(statuses(r)).toEqual(["rejected", "rejected"]);
    expect(r.body.results[0].error).toMatch(/delete scope/);
    expect((await get(rec(w, "/M1"), token)).status).toBe(200);
    // with the scope, the tombstone flag deletes
    const ok = await sync(token, w, { ops: [put(row("M1", "", { deleted: true, updated: TS3 }), 0)] });
    expect(statuses(ok)).toEqual(["accepted"]);
    expect((await get(rec(w, "/M1"), token)).status).toBe(404);
  });

  it("lets an edit resurrect a tombstone only when it is newer or knows the tombstone", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(row("M1", "a", { updated: TS }))] });
    const t = await sync(token, w, { ops: [del("M1", TS, TS2)] });
    const stale = await sync(token, w, { cursor: t.body.cursor, ops: [put(row("M1", "old edit", { updated: TS }), 0)] });
    expect(stale.body.results[0].status).toBe("rejected");
    const newer = await sync(token, w, { cursor: t.body.cursor, ops: [put(row("M1", "new edit", { updated: TS3 }), 0)] });
    expect(newer.body.results[0].status).toBe("accepted");
    expect((await get(rec(w, "/M1"), token)).body.data.summary).toBe("new edit");
  });
});

describe("sync: cursor, pagination, want, counters", () => {
  it("pages changes oldest first with more:true until drained", async () => {
    const { token, w } = await setup();
    for (let batch = 0; batch < 3; batch++) {
      const ops = Array.from({ length: 200 }, (_, i) => put(row(`M${batch * 200 + i + 1}`, `memory ${batch * 200 + i + 1}`)));
      expect((await sync(token, w, { ops })).status).toBe(200);
    }
    const p1 = await sync(token, w, { cursor: 0 });
    expect(p1.body.changes).toHaveLength(500);
    expect(p1.body.more).toBe(true);
    const revs = p1.body.changes.map((c: { rev: number }) => c.rev);
    expect(revs).toEqual([...revs].sort((a: number, b: number) => a - b));
    expect(p1.body.cursor).toBe(revs[499]);
    const p2 = await sync(token, w, { cursor: p1.body.cursor });
    expect(p2.body.changes).toHaveLength(100);
    expect(p2.body.more).toBe(false);
    expect(new Set([...ids(p1.body.changes), ...ids(p2.body.changes)]).size).toBe(600);
    const done = await sync(token, w, { cursor: p2.body.cursor });
    expect(done.body.changes).toEqual([]);
  });

  it("includes every want id even below the cursor, and ignores unknown ones", async () => {
    const { token, w } = await setup();
    const a = await sync(token, w, { ops: [put(row("M1", "a")), put(row("M2", "b"))] });
    const r = await sync(token, w, { cursor: a.body.cursor, want: ["M2", "M404", "M1"] });
    expect(ids(r.body.changes)).toEqual(["M1", "M2"]);
  });

  it("takes the maximum of client and server counters, and counts stored ids", async () => {
    const { token, w } = await setup();
    const r = await sync(token, w, { counters: { M: 40, F: 3, X: 9 }, ops: [put(row("R12", "rule"))] });
    expect(r.body.counters).toMatchObject({ M: 40, F: 3, R: 13, J: 1, T: 1, I: 1, D: 1 });
    expect(r.body.counters).not.toHaveProperty("X");
    const lower = await sync(token, w, { counters: { M: 5 } });
    expect(lower.body.counters.M).toBe(40);
    // REST allocation continues from the synced counter
    const made = await send("POST", rec(w), { token, body: { type: "memory", summary: "next" } });
    expect(made.body.id).toBe("M40");
  });

  it("starts over when the cursor is ahead of the server", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(row("M1", "x"))] });
    const r = await sync(token, w, { cursor: 999_999_999 });
    expect(ids(r.body.changes)).toEqual(["M1"]);
  });

  it("keeps wikis apart", async () => {
    const a = await setup();
    const b = await setup();
    await sync(a.token, a.w, { ops: [put(row("M1", "only in a"))] });
    expect((await sync(b.token, b.w, { cursor: 0 })).body.changes).toEqual([]);
    expect((await sync(a.token, "nowhere", {})).status).toBe(404);
  });
});

describe("sync: replays", () => {
  it("changes nothing when the same request is sent twice", async () => {
    const { token, w } = await setup();
    const ops = [put(job("F1", "feature")), put(job("J1", "job", "F1")), put(row("M1", "mem", { tags: ["a"] })), del("M9", TS, TS2)];
    const first = await sync(token, w, { ops });
    const second = await sync(token, w, { ops });
    expect(statuses(second)).toEqual(["noop", "noop", "noop", "noop"]);
    expect(second.body.results.map((x: { rev: number }) => x.rev).slice(0, 3)).toEqual(first.body.results.map((x: { rev: number }) => x.rev).slice(0, 3));
    expect(second.body.cursor).toBe(first.body.cursor);
    expect((await get(rec(w), token)).body.records).toHaveLength(3);
  });

  it("answers a replayed rename with the same new id and stores no second copy", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(row("M1", "server"))] });
    const ops = [put(row("M1", "client", { created: TS2, updated: TS2 })), put(row("M7", "refers", { links: [["M1", "about"]] }))];
    const first = await sync(token, w, { ops });
    const second = await sync(token, w, { ops });
    // the uid is now stored under the new id, so the replay targets that record
    expect(second.body.results[0]).toMatchObject({ status: "noop", new_id: first.body.results[0].new_id });
    expect(second.body.results[1].status).toBe("noop");
    const all = await get(rec(w, "?kind=M"), token);
    expect(all.body.records.map((r: { id: string }) => r.id)).toEqual(["M1", first.body.results[0].new_id, "M7"]);
    expect(all.body.records.find((r: { id: string }) => r.id === "M7").links).toEqual([[first.body.results[0].new_id, "about"]]);
  });

  it("is atomic: a refused request applies none of its ops", async () => {
    const { token, w } = await setup();
    const rw = await mint("wo", ["write"]);
    const tooMany = Array.from({ length: 201 }, (_, i) => put(row(`M${i + 1}`, "x")));
    const r = await sync(token, w, { ops: tooMany });
    expect(r.status).toBe(413);
    expect((await get(rec(w, "?kind=M"), token)).body.records).toEqual([]);
    const mixed = await sync(rw.token, "nowhere", { ops: [put(row("M1", "x"))] });
    expect(mixed.status).toBe(404);
  });
});

describe("sync: limits and shape", () => {
  it("enforces the record, ops and body limits", async () => {
    const { token, w } = await setup();
    const big = await sync(token, w, { ops: [put(row("M1", "huge", { data: { summary: "huge", body: "x".repeat(1_100_000), alias: null, source: "", session: "" } }))] });
    expect(big.status).toBe(200);
    expect(big.body.results[0]).toMatchObject({ status: "rejected" });
    expect(big.body.results[0].error).toMatch(/1 MB/);
    const body = JSON.stringify({ cursor: 0, ops: [], want: [], counters: {}, pad: "x".repeat(9 * 1024 * 1024) });
    expect((await send("POST", `/v1/wikis/${w}/sync`, { token, raw: body })).status).toBe(413);
    expect((await send("POST", `/v1/wikis/${w}/sync`, { token, raw: "{oops" })).status).toBe(400);
    expect((await send("POST", `/v1/wikis/${w}/sync`, { token, body: { ops: [{ op: "frob" }] } })).status).toBe(400);
    expect((await send("POST", `/v1/wikis/${w}/sync`, { token, body: { cursor: -1 } })).status).toBe(400);
    expect((await send("POST", `/v1/wikis/${w}/sync`, { token, body: { want: Array.from({ length: 501 }, (_, i) => `M${i}`) } })).status).toBe(413);
    expect((await send("POST", `/v1/wikis/${w}/sync`, { token, body: { counters: { M: -3 } } })).status).toBe(400);
  });

  it("masks secrets that arrive through sync", async () => {
    const { token, w } = await setup();
    const key = ["gh", "p_", "A1b2C3d4E5f6G7h8I9j0", "abcd"].join("");
    const r = await sync(token, w, { ops: [put(row("M1", `token ${key}`, { data: { summary: `token ${key}`, body: `password: ${"s3cretValue12345"}`, alias: null, source: "", session: "" } }))] });
    expect(r.body.changes[0].data.summary).toBe("token [REDACTED]");
    expect(r.body.changes[0].data.body).toBe("password: [REDACTED]");
  });
});

describe("sync: record identity is the uid", () => {
  const UID_A = "device-a-uid-0001";
  const UID_B = "device-b-uid-0002";
  const byId = (records: { id: string }[]) => Object.fromEntries(records.map((x) => [x.id, x])) as Record<string, any>;

  it("keeps both records when two devices create the same id in the same second", async () => {
    const a = await setup();
    const b = await mintAll("device-b");
    // device A and device B each created R2 and J2 offline with the same created time
    const fromA = await sync(a.token, a.w, { ops: [put(row("R2", "rule from A", { uid: UID_A })), put(job("J2", "job from A", null, { uid: UID_A + "j" }))] });
    expect(statuses(fromA)).toEqual(["accepted", "accepted"]);
    const fromB = await sync(b.token, a.w, {
      ops: [
        put(row("R2", "rule from B", { uid: UID_B })),
        put(job("J2", "job from B", null, { uid: UID_B + "j" })),
        put(job("T2", "task of B job", "J2", { uid: UID_B + "t" })),
      ],
    });
    expect(statuses(fromB)).toEqual(["renamed", "renamed", "accepted"]);
    const [r, j] = fromB.body.results;
    expect(r.new_id).not.toBe("R2");
    expect(j.new_id).not.toBe("J2");
    const all = byId((await get(rec(a.w), a.token)).body.records);
    // A kept its labels; B was relabelled; nothing was lost
    expect(all["R2"].data.summary).toBe("rule from A");
    expect(all["J2"].data.title).toBe("job from A");
    expect(all[r.new_id].data.summary).toBe("rule from B");
    expect(all[j.new_id].data.title).toBe("job from B");
    // the task that named B's job by its old id now hangs under B's job, not A's
    expect(all["T2"].data.parent).toBe(j.new_id);
    expect(all[j.new_id].uid).toBe(UID_B + "j");
    expect(all["R2"].uid).toBe(UID_A);
  });

  it("does not let a put change the uid of a stored id", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(row("M1", "original", { uid: UID_A }))] });
    const swap = await sync(token, w, { ops: [put(row("M1", "hijack", { uid: UID_B, updated: TS3 }), 0)] });
    expect(swap.body.results[0].status).toBe("renamed");
    const m1 = (await get(rec(w, "/M1"), token)).body;
    expect(m1.uid).toBe(UID_A);
    expect(m1.data.summary).toBe("original");
    expect((await get(rec(w, `/${swap.body.results[0].new_id}`), token)).body.uid).toBe(UID_B);
    // a tombstone holds its id and uid just the same
    await sync(token, w, { ops: [del("M1", TS, TS2, null, UID_A)] });
    const again = await sync(token, w, { ops: [put(row("M1", "after delete", { uid: "device-c-uid-0003", updated: TS3 }))] });
    expect(again.body.results[0].status).toBe("renamed");
    const tomb = again.body.changes.find((c: { id: string }) => c.id === "M1");
    expect(tomb.deleted).toBe(true);
    expect(tomb.uid).toBe(UID_A);
  });

  it("targets the stored record when the uid is already stored under another id", async () => {
    const { token, w } = await setup();
    const first = await sync(token, w, { ops: [put(job("J1", "server job", null, { uid: UID_A, updated: TS2 }))] });
    const rev = first.body.results[0].rev;
    // the client calls it J7 (relabelled locally): same uid, so it is J1
    const equal = await sync(token, w, { ops: [put(job("J7", "server job", null, { uid: UID_A, updated: TS2 }))] });
    expect(equal.body.results[0]).toEqual({ id: "J7", status: "noop", rev, new_id: "J1" });
    const stale = await sync(token, w, { ops: [put(job("J7", "older edit", null, { uid: UID_A, updated: TS }), 0)] });
    expect(stale.body.results[0]).toMatchObject({ status: "rejected", new_id: "J1" });
    expect(stale.body.changes.map((c: { id: string }) => c.id)).toContain("J1");
    const newer = await sync(token, w, {
      ops: [put(job("J7", "newer edit", null, { uid: UID_A, updated: TS3 }), 0), put(job("T1", "task", "J7", { uid: UID_B }))],
    });
    expect(statuses(newer)).toEqual(["accepted", "accepted"]);
    expect(newer.body.results[0].new_id).toBe("J1");
    const byBase = await sync(token, w, { ops: [put(job("J7", "by base rev", null, { uid: UID_A, updated: TS }), newer.body.results[0].rev)] });
    expect(byBase.body.results[0]).toMatchObject({ status: "accepted", new_id: "J1" });
    const all = byId((await get(rec(w), token)).body.records);
    expect(Object.keys(all).sort()).toEqual(["J1", "T1"]);
    expect(all["J1"].data.title).toBe("by base rev");
    // the later op that named J7 as parent follows it to J1
    expect(all["T1"].data.parent).toBe("J1");
  });

  it("deletes by uid: a wrong uid is a noop, the right one a tombstone", async () => {
    const { token, w } = await setup();
    const a = await sync(token, w, { ops: [put(row("M1", "x", { uid: UID_A }))] });
    const wrong = await sync(token, w, { ops: [del("M1", TS, TS3, null, UID_B), del("M1", TS, TS3, null, "")] });
    expect(statuses(wrong)).toEqual(["noop", "noop"]);
    expect((await get(rec(w, "/M1"), token)).status).toBe(200);
    const right = await sync(token, w, { cursor: a.body.cursor, ops: [del("M1", TS, TS3, null, UID_A)] });
    expect(right.body.results[0].status).toBe("accepted");
    expect(right.body.changes[0]).toMatchObject({ id: "M1", uid: UID_A, deleted: true });
    // a put carrying the tombstone flag is a delete by uid too
    await sync(token, w, { ops: [put(row("M2", "y", { uid: UID_B }))] });
    const flag = await sync(token, w, {
      ops: [put(row("M2", "", { uid: UID_A + "z", deleted: true, updated: TS3 })), put(row("M2", "", { uid: UID_B, deleted: true, updated: TS3 }))],
    });
    expect(statuses(flag)).toEqual(["noop", "accepted"]);
  });

  it("validates the uid", async () => {
    const { token, w } = await setup();
    const bad = ["", "short", "x".repeat(65), "has space 0123456789", "semi;colon-0123456789", "é".repeat(16)];
    const r = await sync(token, w, { ops: bad.map((uid, i) => put(row(`M${i + 1}`, "x", { uid }))) });
    expect(statuses(r)).toEqual(bad.map(() => "rejected"));
    expect(r.body.results[0].error).toMatch(/uid/);
    const edge = await sync(token, w, { ops: [put(row("M20", "min", { uid: "a".repeat(16) })), put(row("M21", "max", { uid: "A_-".repeat(21) + "z" }))] });
    expect(statuses(edge)).toEqual(["accepted", "accepted"]);
    // a note has no uid; a uid on a note is refused
    const note = { id: "research/n.md", kind: "N", created: "", updated: TS, deleted: false, data: { body: "x" }, tags: [], links: [] };
    const notes = await sync(token, w, { ops: [put({ ...note, uid: UID_A }), put({ ...note, uid: "" })] });
    expect(statuses(notes)).toEqual(["rejected", "accepted"]);
    // a uid cannot be reused for a record of another kind
    const kinds = await sync(token, w, { ops: [put(row("R1", "rule", { uid: "a".repeat(16) }))] });
    expect(kinds.body.results[0].status).toBe("rejected");
    expect(kinds.body.results[0].error).toMatch(/another kind/);
  });

  it("returns the uid on every record, tombstones included", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(row("M1", "x", { uid: UID_A })), put(row("M2", "y", { uid: UID_B }))] });
    await sync(token, w, { ops: [del("M2", TS, TS2, null, UID_B)] });
    const all = await sync(token, w, { cursor: 0 });
    expect(all.body.changes.map((c: { id: string; uid: string }) => [c.id, c.uid])).toEqual([["M1", UID_A], ["M2", UID_B]]);
    expect(uidOf("M1", TS)).toMatch(/^[A-Za-z0-9_-]{16,64}$/);
  });
});
