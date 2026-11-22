// The configurable hierarchy on the Worker: a repo's node carries its path, type, read_stage and global_access.
// read_stage above 0 hides the repo from a baseline device; global_access caps what a session start reads of the
// global wiki; the session text says where the repo sits; an agent deletes only its own records.

import { describe, expect, it } from "vitest";
import { HIERARCHY_PREFIX, MAX_SIBLINGS, hierarchyLine } from "../src/instructions.js";
import { parseNode } from "../src/nodes.js";
import { OWNER, TS, freshKey, get, mint, mintAll, put, rec, row, send, sync, uidOf, wiki } from "./helpers.js";
import { mempmIn, sessionText, tool } from "./mcp-helpers.js";

const nodeDoc = (id: string, key: string, extra: Record<string, unknown>, updated = TS) => ({
  id, uid: uidOf(id, TS), kind: "D", created: TS, updated, deleted: false,
  data: {
    title: `node: ${key}`, alias: `node:${key}`, mime: "application/json", source: "node",
    body: JSON.stringify({ key, updated: "2026-01-01", northstar: "", fires: [], principles: [], mistakes: 0, counts: {}, ...extra }),
  },
  tags: ["shapa-node"], links: [],
});

/** The node documents already pushed: key -> [document id, rev]. */
const pushed = new Map<string, [string, number]>();
let nodeN = 100;
/** The operator's machine pushes (or rewrites) a repo node in the global wiki. */
async function pushNode(op: string, key: string, extra: Record<string, unknown>) {
  const had = pushed.get(key);
  const id = had ? had[0] : `D${++nodeN}`;
  const r = await sync(op, "global", { ops: [put(nodeDoc(id, key, extra, had ? new Date(Date.now() + (++nodeN) * 1000).toISOString().replace(/\.\d+Z$/, "Z") : TS), had ? had[1] : null)] });
  expect(r.body.results[0].status).toBe("accepted");
  pushed.set(key, [id, r.body.results[0].rev]);
}

async function world() {
  const op = (await mintAll()).token;
  await wiki(op, "global", "global");
  const k = freshKey();
  const w = `repo-${k}`;
  await wiki(op, w);
  const baseline = await send("POST", "/admin/devices", { token: OWNER, body: { name: `chat-${k}`, scopes: ["read", "write"], reach: "baseline" } });
  expect(baseline.status).toBe(201);
  return { op, w, k, base: baseline.body.token as string };
}

describe("a node carries the repo's place in the hierarchy", () => {
  it("reads path, type, read_stage and global_access; a node written before them reads with the defaults", () => {
    const body = (o: object) => ({ source: "node", body: JSON.stringify({ key: "a-repo", ...o }) });
    expect(parseNode("node:a-repo", ["shapa-node"], body({ path: ["acme", "core"], type: "main", read_stage: 2, global_access: 3 }))).toEqual({ key: "a-repo", path: ["acme", "core"], type: "main", readStage: 2, globalAccess: 3 });
    expect(parseNode("node:a-repo", ["shapa-node"], body({}))).toEqual({ key: "a-repo", path: [], type: "", readStage: 0, globalAccess: null });
    expect(parseNode("node:a-repo", ["shapa-node"], body({ path: ["ok", "bad name", 7], type: "x y", read_stage: 12, global_access: -1 }))).toEqual({ key: "a-repo", path: ["ok"], type: "", readStage: 0, globalAccess: null });
  });

  it("is trusted only when alias, tag, source and key agree", () => {
    const good = { source: "node", body: JSON.stringify({ key: "a-repo", read_stage: 5 }) };
    expect(parseNode("node:other", ["shapa-node"], good)).toBeNull();
    expect(parseNode("node:a-repo", [], good)).toBeNull();
    expect(parseNode("node:a-repo", ["shapa-node"], { ...good, source: "save" })).toBeNull();
    expect(parseNode("node:a-repo", ["shapa-node"], { source: "node", body: "not json" })).toBeNull();
  });

  it("lists the repo's path and type in the wikis a device may use", async () => {
    const { op, w, base } = await world();
    await pushNode(op, w, { path: ["acme", "core"], type: "main" });
    const mine = (await get("/v1/wikis", op)).body.wikis.find((x: { key: string }) => x.key === w);
    expect(mine).toMatchObject({ path: ["acme", "core"], type: "main" });
    expect((await get("/v1/wikis", base)).body.wikis.find((x: { key: string }) => x.key === w)).toMatchObject({ path: ["acme", "core"] });
    const other = freshKey();
    await wiki(op, other);
    const plain = (await get("/v1/wikis", op)).body.wikis.find((x: { key: string }) => x.key === other);
    expect(plain.path).toBeUndefined();
    expect(plain.type).toBeUndefined();
  });
});

describe("read_stage above 0 hides a repo from a baseline device", () => {
  async function seeded() {
    const x = await world();
    const secret = `HIDDENREPO${x.k.replace(/\W/g, "")}`;
    const made = await send("POST", rec(x.w), { token: x.op, body: { type: "idea", summary: `${secret} idea` } });
    expect(made.status).toBe(201);
    const id = (made.body.record?.id ?? made.body.id) as string;
    return { ...x, secret, id };
  }

  it("shows it until its node says so, then removes the wiki, its records, search hits and tool results", async () => {
    const { op, w, base, secret, id } = await seeded();
    expect((await get(rec(w, `/${id}`), base)).status).toBe(200);
    expect((await get("/v1/wikis", base)).body.wikis.map((x: { key: string }) => x.key)).toContain(w);
    await pushNode(op, w, { read_stage: 1 });
    expect((await get(rec(w, `/${id}`), base)).status).toBeGreaterThanOrEqual(400);
    expect((await get(rec(w), base)).status).toBeGreaterThanOrEqual(400);
    expect((await get("/v1/wikis", base)).body.wikis.map((x: { key: string }) => x.key)).not.toContain(w);
    expect((await get(`/v1/search?q=${secret}`, base)).body.results).toEqual([]);
    expect((await get(`/v1/search?q=${secret}&wiki=${w}`, base)).body.results ?? []).toEqual([]);
    for (const [name, args] of [["search", { query: secret }], ["get", { id, wiki: w }], ["get", { id }], ["wikis", {}]] as const) {
      expect((await tool(base, name, args as Record<string, unknown>)).text, name).not.toContain(secret);
    }
    expect((await tool(base, "wikis", {})).text).not.toContain(w);
    // the operator's own device still reads everything
    expect((await get(rec(w, `/${id}`), op)).status).toBe(200);
    expect((await get(`/v1/search?q=${secret}`, op)).body.results.length).toBeGreaterThan(0);
  });

  it("leaves a baseline device no route into the hidden wiki (reads, writes, tags, tasks, projects, skills, focus)", async () => {
    const { op, w, base, secret, id } = await seeded();
    await pushNode(op, w, { read_stage: 1 });
    for (const path of [`/v1/wikis/${w}`, `/v1/wikis/${w}/focus`, `/v1/wikis/${w}/skills`, `/v1/wikis/${w}/records/${id}/related`, `/v1/tags?wiki=${w}`, `/v1/tasks?wiki=${w}`, `/v1/get?id=${id}&wiki=${w}`, `/v1/projects?wiki=${w}`]) {
      const r = await get(path, base);
      expect(r.status >= 400 || !r.text.includes(secret), `${path}: ${r.status} ${r.text.slice(0, 120)}`).toBe(true);
      expect(r.status >= 400 || !r.text.includes(w), `${path} names the hidden wiki`).toBe(true);
    }
    const write = await send("POST", rec(w), { token: base, body: { type: "idea", summary: "sneaking in" } });
    expect(write.status).toBeGreaterThanOrEqual(400);
    expect((await send("POST", `/v1/wikis/${w}/sync`, { token: base, body: {} })).status).toBeGreaterThanOrEqual(400);
    expect((await send("PATCH", rec(w, `/${id}`), { token: base, body: { summary: "edited" } })).status).toBeGreaterThanOrEqual(400);
  });

  it("is lifted when the node's read_stage returns to 0", async () => {
    const { op, w, base, id } = await seeded();
    await pushNode(op, w, { read_stage: 3 });
    expect((await get(rec(w, `/${id}`), base)).status).toBeGreaterThanOrEqual(400);
    await pushNode(op, w, { read_stage: 0 });
    expect((await get(rec(w, `/${id}`), base)).status).toBe(200);
  });

  it("hides the repo's project entry in the global wiki, its session text and its tasks from the baseline device", async () => {
    const { op, w, base, k } = await seeded();
    expect((await send("PUT", `/v1/projects/${w}`, { token: op })).status).toBe(201);
    expect((await get("/v1/projects", base)).body.projects.map((p: { key: string }) => p.key)).toContain(w);
    const task = await send("POST", rec(w), { token: op, body: { type: "human_task", title: `HIDDENTASK ${k}` } });
    expect(task.status).toBe(201);
    await pushNode(op, w, { read_stage: 2 });
    expect((await get("/v1/projects", base)).body.projects.map((p: { key: string }) => p.key)).not.toContain(w);
    expect((await get(`/v1/projects/${w}`, base)).status).toBe(404);
    expect((await get(`/v1/projects/${w}`, op)).status).toBe(200);
    expect((await tool(base, "tasks", {})).text).not.toContain(`HIDDENTASK ${k}`);
    expect((await tool(op, "tasks", {})).text).toContain(`HIDDENTASK ${k}`);
    expect(await sessionText(base, { wiki: w })).toMatch(/^forbidden|not_found|no wiki/m);
  });

  it("leaves the repo visible to a node without read_stage (an old node) and to read_stage 0", async () => {
    const { op, w, base, id } = await seeded();
    await pushNode(op, w, {});
    expect((await get(rec(w, `/${id}`), base)).status).toBe(200);
  });
});

describe("session text: where this repo sits", () => {
  it("prints the path, the type and the siblings after the fires and before the mempm", async () => {
    const { op, w, k } = await world();
    const sib = `sib-${k}`;
    await wiki(op, sib);
    const group = [`acme-${k}`, "core"];
    await pushNode(op, w, { path: group, type: "main" });
    await pushNode(op, sib, { path: group, type: "sub" });
    await send("PUT", `/v1/wikis/${w}/focus`, { token: op, body: { northstar: "north", fires: [{ text: "a fire" }] } });
    await sync(op, "global", { ops: [put(row("R300", "hierarchy baseline rule", { tags: ["stage:0"] }))] });
    const t = await sessionText(op, { wiki: w });
    expect(t).toContain(`${HIERARCHY_PREFIX}${group.join(" > ")} > ${w} (type main); siblings: ${sib}`);
    const at = (s: string) => t.indexOf(s);
    expect(at("FIRES (")).toBeLessThan(at(HIERARCHY_PREFIX));
    expect(at(HIERARCHY_PREFIX)).toBeLessThan(at('{"P":'));
    expect(await sessionText(op)).not.toContain(HIERARCHY_PREFIX);              // no repo wiki asked for: no line
  });

  it("has no line for a repo with neither a path nor a type, and omits siblings a baseline device cannot read", async () => {
    const { op, w, k, base } = await world();
    await pushNode(op, w, {});
    expect(await sessionText(op, { wiki: w })).not.toContain(HIERARCHY_PREFIX);
    const hid = `hid-${k}`;
    const vis = `vis-${k}`;
    for (const key of [hid, vis]) await wiki(op, key);
    const g = [`g-${k}`];
    await pushNode(op, w, { path: g, type: "main" });
    await pushNode(op, hid, { path: g, read_stage: 4 });
    await pushNode(op, vis, { path: g });
    expect(await sessionText(op, { wiki: w })).toContain(`siblings: ${hid}, ${vis}`);
    const forBase = await sessionText(base, { wiki: w });
    expect(forBase).toContain(`siblings: ${vis}`);
    expect(forBase).not.toContain(hid);
  });

  it("names no sibling the device's wiki list leaves out", async () => {
    const { op, w, k } = await world();
    const sib = `sib-${k}`;
    const walled = `walled-${k}`;
    for (const key of [sib, walled]) await wiki(op, key);
    const g = [`g-${k}`];
    for (const key of [w, sib, walled]) await pushNode(op, key, { path: g, type: "main" });
    const limited = (await mint(`limited-${freshKey()}`, ["read", "write"], [w, sib, "global"])).token;
    const text = await sessionText(limited, { wiki: w });
    expect(text).toContain(`siblings: ${sib}`);
    expect(text).not.toContain(walled);
  });

  it("builds the same line the local engine builds, with at most eight siblings", () => {
    expect(hierarchyLine(["a", "b"], "me", "main", ["x", "y"])).toBe("HIERARCHY: a > b > me (type main); siblings: x, y");
    expect(hierarchyLine([], "me", "main", [])).toBe("HIERARCHY: me (type main)");
    expect(hierarchyLine([], "me", "", [])).toBe("");
    const many = Array.from({ length: 12 }, (_, i) => `s${i}`);
    expect(hierarchyLine(["a"], "me", "", many)).toBe(`HIERARCHY: a > me; siblings: ${many.slice(0, MAX_SIBLINGS).join(", ")} (+4 more)`);
  });
});

describe("global_access caps what a repo's session start reads of the global wiki", () => {
  it("leaves out global records above it, untagged ones included, and caps the requested stage", async () => {
    const { op, w, k } = await world();
    const tag = k.replace(/\W/g, "");
    await sync(op, "global", { ops: [
      put(row("R410", `base rule ${tag}`, { tags: ["stage:0"] })),
      put(row("R411", `two rule ${tag}`, { tags: ["stage:2"] })),
      put(row("M410", `five memory ${tag}`, { tags: ["stage:5"] })),
      put(row("R412", `plain principle ${tag}`, { tags: ["principle"] })),
      put(row("I410", `global lesson ${tag}`, { data: { summary: `global lesson ${tag}`, body: "", alias: null, source: "correction", session: "" } })),
    ] });
    const texts = (t: string) => [...mempmIn(t).P, ...mempmIn(t).X, ...mempmIn(t).M].map((e) => e[2]);
    const open = texts(await sessionText(op, { wiki: w, stage: 9 }));
    for (const s of ["base rule", "two rule", "five memory", "plain principle", "global lesson"]) expect(open.some((x) => x.startsWith(s)), s).toBe(true);
    await pushNode(op, w, { global_access: 2 });
    const capped = texts(await sessionText(op, { wiki: w, stage: 9 }));
    expect(capped.some((x) => x.startsWith("base rule"))).toBe(true);
    expect(capped.some((x) => x.startsWith("two rule"))).toBe(true);
    for (const s of ["five memory", "plain principle", "global lesson"]) expect(capped.some((x) => x.startsWith(s)), s).toBe(false);
    await pushNode(op, w, { global_access: 0 });
    const zero = texts(await sessionText(op, { wiki: w, stage: 9 }));
    expect(zero.some((x) => x.startsWith("two rule"))).toBe(false);
    expect(zero.some((x) => x.startsWith("base rule"))).toBe(true);
    // the same device without a repo wiki in play keeps today's behaviour
    expect(texts(await sessionText(op, { stage: 9 })).some((x) => x.startsWith("five memory"))).toBe(true);
  });
});

describe("the delete tool removes only the caller's own records", () => {
  const save = (token: string, w: string, summary: string, type = "memory") => tool(token, "save", { wiki: w, type, summary });

  it("lets a write-only device delete what it saved, and refuses another device's record", async () => {
    const { op, w } = await world();
    const agent = (await mint(`agent-${freshKey()}`, ["read", "write"])).token;
    const mine = await save(agent, w, "an agent note to remove");
    const id = /^Saved (\S+)/.exec(mine.text)![1]!;
    const theirs = await save(op, w, "the operator's memory");
    const theirId = /^Saved (\S+)/.exec(theirs.text)![1]!;
    const refused = await tool(agent, "delete", { wiki: w, id: theirId });
    expect(refused.isError).toBe(true);
    expect(refused.text).toMatch(/^forbidden: .*not written by this device/);
    expect((await get(rec(w, `/${theirId}`), op)).status).toBe(200);
    const done = await tool(agent, "delete", { wiki: w, id });
    expect(done.isError).toBe(false);
    expect(done.text).toBe(`Deleted ${id} in ${w}.`);
    expect((await get(rec(w, `/${id}`), op)).status).toBe(404);
    expect((await tool(agent, "delete", { wiki: w, id })).isError).toBe(true);   // already gone
  });

  it("is refused to a baseline device, which never deletes", async () => {
    const { w, base } = await world();
    const mine = await tool(base, "save", { wiki: w, type: "idea", summary: "a baseline idea" });
    const id = /^Saved (\S+)/.exec(mine.text)![1]!;
    const r = await tool(base, "delete", { wiki: w, id });
    expect(r.isError).toBe(true);
    expect(r.text).toMatch(/^forbidden/);
  });

  it("is refused once someone else edited the record, and for the engine's records and stage 0", async () => {
    const { op, w, k } = await world();
    const agent = (await mint(`agent-${freshKey()}`, ["read", "write"])).token;
    const saved = await save(agent, w, `edited later ${k}`);
    const id = /^Saved (\S+)/.exec(saved.text)![1]!;
    expect((await send("PATCH", rec(w, `/${id}`), { token: op, body: { summary: `operator reworded ${k}` } })).status).toBe(200);
    expect((await tool(agent, "delete", { wiki: w, id })).isError).toBe(true);
    // a record the agent wrote that carries the baseline marker or an engine tag is not its to delete
    const doc = await tool(agent, "save", { wiki: w, type: "document", title: `plain doc ${k}`, body: "x" });
    expect((await tool(agent, "delete", { wiki: w, id: /^Saved (\S+)/.exec(doc.text)![1]! })).isError).toBe(false);
    await send("PUT", `/v1/wikis/${w}/focus`, { token: op, body: { northstar: "n" } });
    const focusId = (await get(`/v1/wikis/${w}/focus`, op)).body.id ?? "focus";
    expect((await tool(agent, "delete", { wiki: w, id: focusId })).isError).toBe(true);
  });

  it("deletes a note this device wrote through sync, without the delete scope", async () => {
    const { op, w } = await world();
    const agent = await mint(`agent-${freshKey()}`, ["read", "write"]);
    const note = (path: string, body: string) => ({ id: path, uid: "", kind: "N", created: "", updated: TS, rev: 0, deleted: false, device: "", data: { body }, tags: [], links: [] });
    await sync(agent.token, w, { ops: [put(note("arch/mine.md", "mine"))] });
    await sync(op, w, { ops: [put(note("arch/theirs.md", "theirs"))] });
    const del = (id: string) => ({ op: "del", id, uid: "", deleted_at: "2026-02-01T00:00:00Z", base_rev: null });
    const r = await sync(agent.token, w, { ops: [del("arch/mine.md"), del("arch/theirs.md")] });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(["accepted", "rejected"]);
    expect(r.body.results[1].error).toMatch(/delete scope/);
    expect((await get(rec(w, "/arch%2Fmine.md"), op)).status).toBe(404);
    expect((await get(rec(w, "/arch%2Ftheirs.md"), op)).status).toBe(200);
  });
});
