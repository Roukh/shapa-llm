import { describe, expect, it } from "vitest";
import { TS, TS2, mint, put, rec, send, setup, sync, uidOf } from "./helpers.js";
import { tool } from "./mcp-helpers.js";

const doc = (id: string, title: string, body: string, tags: string[] = [], over: Record<string, unknown> = {}) => ({
  id, uid: uidOf(id, TS), kind: "D", created: TS, updated: TS, deleted: false,
  data: { title, body, alias: null, mime: "text/markdown", source: "" }, tags, links: [], ...over,
});
const create = (token: string, w: string, body: Record<string, unknown>) =>
  send("POST", rec(w), { token, body: { type: "document", ...body } });

describe("the lean gate: one document per project", () => {
  it("refuses a second document naming the first, and merging appends a section instead", async () => {
    const { token, w } = await setup();
    const first = await create(token, w, { title: "Principles", body: "rule one" });
    expect(first.status).toBe(201);
    const second = await create(token, w, { title: "Models", body: "model text" });
    expect(second.status).toBe(409);
    expect(second.body.error.message).toContain(first.body.id);
    expect(second.body.error.message).toContain("Principles");
    const merged = await create(token, w, { title: "Models", body: "model text", merge: true });
    expect(merged.status).toBe(200); // not created: the existing one grew
    expect(merged.body.id).toBe(first.body.id);
    expect(merged.body.data.body).toBe("rule one\n\n## Models\n\nmodel text");
    const docs = await send("GET", rec(w, "?kind=D"), { token });
    expect(docs.body.records).toHaveLength(1);
  });

  it("gives a subproject its own document and refuses a bad key", async () => {
    const { token, w } = await setup();
    await create(token, w, { title: "Repo doc", body: "x" });
    const sub = await create(token, w, { title: "Hosted doc", body: "y", project: "hosted" });
    expect(sub.status).toBe(201);
    expect(sub.body.tags).toContain("project:hosted");
    expect((await create(token, w, { title: "Hosted again", body: "z", project: "hosted" })).status).toBe(409);
    for (const bad of ["Has Space", "../up", "x".repeat(81)]) {
      expect((await create(token, w, { title: "t", body: "b", project: bad })).status, bad).toBe(400);
    }
    // project and merge mean nothing on another type
    expect((await send("POST", rec(w), { token, body: { type: "memory", summary: "m", merge: true } })).status).toBe(400);
  });

  it("is per wiki: each scope has its own project document", async () => {
    const a = await setup();
    const b = await setup();
    expect((await create(a.token, a.w, { title: "A", body: "x" })).status).toBe(201);
    expect((await create(a.token, b.w, { title: "B", body: "x" })).status).toBe(201);
  });

  it("does not count a focus record or a skill version, and they cannot be made as documents", async () => {
    const { token, w } = await setup();
    await send("PUT", `/v1/wikis/${w}/focus`, { token, body: { northstar: "north" } });
    expect((await create(token, w, { title: "Principles", body: "the one" })).status).toBe(201);
    for (const t of ["focus", "skill"]) {
      expect((await create(token, w, { title: "Sneaky", body: "x", tags: [t] })).status, t).toBe(400);
    }
  });

  it("covers a retag: a document cannot move into a held project or take a reserved tag", async () => {
    const { token, w } = await setup();
    await create(token, w, { title: "One", body: "x" });
    const other = await create(token, w, { title: "Two", body: "y", project: "other" });
    const move = await send("PATCH", rec(w, `/${other.body.id}`), { token, body: { tags: [] } });
    expect(move.status).toBe(409);
    expect(move.body.error.message).toContain("already has document");
    const sneaky = await send("PATCH", rec(w, `/${other.body.id}`), { token, body: { tags: ["project:other", "skill"] } });
    expect(sneaky.status).toBe(400);
  });

  it("applies through MCP save, with merge", async () => {
    const { token, w } = await setup();
    const first = await tool(token, "save", { wiki: w, type: "document", title: "Principles", body: "one" });
    expect(first.isError).toBe(false);
    const second = await tool(token, "save", { wiki: w, type: "document", title: "More", body: "two" });
    expect(second.isError).toBe(true);
    expect(second.text).toContain("already has document D1");
    const merged = await tool(token, "save", { wiki: w, type: "document", title: "More", body: "two", merge: true });
    expect(merged.isError).toBe(false);
    expect(merged.text).toContain("Merged into D1");
    expect((await tool(token, "save", { wiki: w, type: "document", title: "Hosted", body: "h", project: "hosted" })).isError).toBe(false);
  });

  it("refuses an oversized merge", async () => {
    const { token, w } = await setup();
    await create(token, w, { title: "Big", body: "x".repeat(600_000) });
    const r = await create(token, w, { title: "More", body: "y".repeat(600_000), merge: true });
    expect(r.status).toBe(413);
  });
});

describe("the lean gate over sync", () => {
  it("accepts the first document, refuses a different one for the same project naming the first", async () => {
    const { token, w } = await setup();
    const a = await sync(token, w, { ops: [put(doc("D1", "Principles", "from device a", [], { uid: "device-a-uid-0000001" }))] });
    expect(a.body.results[0].status).toBe("accepted");
    const b = await sync(token, w, { ops: [put(doc("D1", "Principles too", "from device b", [], { uid: "device-b-uid-0000001" }))] });
    expect(b.body.results[0].status).toBe("rejected");
    expect(b.body.results[0].error).toContain("already has document D1");
    const docs = await send("GET", rec(w, "?kind=D"), { token });
    expect(docs.body.records).toHaveLength(1);
    expect(docs.body.records[0].data.body).toBe("from device a");
  });

  it("lets the first writer's own edits through, and a second document once the first is deleted", async () => {
    const { token, w } = await setup();
    const first = doc("D1", "Principles", "v1", [], { uid: "device-a-uid-0000001" });
    await sync(token, w, { ops: [put(first)] });
    const edit = await sync(token, w, { ops: [put({ ...first, updated: TS2, data: { ...first.data, body: "v2" } })] });
    expect(edit.body.results[0].status).toBe("accepted");
    const del = await send("DELETE", rec(w, "/D1"), { token });
    expect(del.status).toBe(200);
    const next = await sync(token, w, { ops: [put(doc("D2", "Principles again", "fresh", [], { uid: "device-b-uid-0000001" }))] });
    expect(next.body.results[0].status).toBe("accepted");
  });

  it("keeps two different projects apart and exempts a focus record and a skill version", async () => {
    const { token, w } = await setup();
    const ops = [
      put(doc("D1", "Principles", "x", [], { uid: "aaaaaaaaaaaaaaaa1" })),
      put(doc("D2", "Hosted", "y", ["project:hosted"], { uid: "aaaaaaaaaaaaaaaa2" })),
      put(doc("D3", "Focus", JSON.stringify({ v: 1, northstar: "n", fires: [null, null, null] }), ["focus"], { uid: "aaaaaaaaaaaaaaaa3", data: { title: "Focus", body: JSON.stringify({ v: 1, northstar: "n", fires: [null, null, null] }), alias: "focus", mime: "application/json", source: "" } })),
      put(doc("D4", "tdd v1", "---\nname: tdd\ndescription: d\n---\nb", ["skill", "skill:tdd", "v:1", "status:active"], { uid: "aaaaaaaaaaaaaaaa4" })),
    ];
    const r = await sync(token, w, { ops });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(["accepted", "accepted", "accepted", "accepted"]);
  });

  it("refuses a skill or focus tag that does not carry what it promises, so the tags are no way around the gate", async () => {
    const { token, w } = await setup();
    await sync(token, w, { ops: [put(doc("D1", "Principles", "the one", [], { uid: "aaaaaaaaaaaaaaaa1" }))] });
    const sneaky = await sync(token, w, { ops: [
      put(doc("D2", "Sneaky", "x", ["skill"], { uid: "aaaaaaaaaaaaaaaa2" })),
      put(doc("D3", "Sneaky", "x", ["skill", "skill:Bad Name", "v:1", "status:active"], { uid: "aaaaaaaaaaaaaaaa3" })),
      put(doc("D4", "Sneaky", "not json", ["focus"], { uid: "aaaaaaaaaaaaaaaa4" })),
    ] });
    expect(sneaky.body.results.map((x: { status: string }) => x.status)).toEqual(["rejected", "rejected", "rejected"]);
  });

  it("is the single writer: two devices racing in one request cannot make two", async () => {
    const { token, w } = await setup();
    const r = await sync(token, w, { ops: [
      put(doc("D1", "From a", "a", [], { uid: "race-uid-aaaaaaaa1" })),
      put(doc("D2", "From b", "b", [], { uid: "race-uid-bbbbbbbb1" })),
    ] });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(["accepted", "rejected"]);
  });

  it("needs the write scope: a read-only device cannot push a document or a pending skill", async () => {
    const { w } = await setup();
    const ro = await mint("reader", ["read"]);
    const r = await sync(ro.token, w, { ops: [put(doc("D1", "tdd v2", "b", ["skill", "skill:tdd", "v:2", "status:pending"], { uid: "aaaaaaaaaaaaaaaa1" }))] });
    expect(r.status).toBe(403);
  });
});
