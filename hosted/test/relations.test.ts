import { describe, expect, it } from "vitest";
import { freshKey, get, mint, put, rec, row, send, setup, sync, wiki } from "./helpers.js";
import { tool } from "./mcp-helpers.js";

const create = (token: string, w: string, body: Record<string, unknown>) => send("POST", rec(w), { token, body });
const tagsOf = async (token: string, q = "") => (await get(`/v1/tags${q}`, token)).body.tags as { tag: string; total: number; counts: Record<string, number> }[];
const one = async (token: string, tag: string, q = "") => (await tagsOf(token, q)).find((t) => t.tag === tag);

describe("tags are the relations: the tag index", () => {
  it("counts live records by type per tag, in step with create, retag, close and delete", async () => {
    const { token, w } = await setup();
    const t = `topic-${freshKey()}`;
    await create(token, w, { type: "rule", summary: "r", tags: [t] });
    await create(token, w, { type: "memory", summary: "m", tags: [t] });
    await create(token, w, { type: "idea", summary: "i", tags: [t] });
    await create(token, w, { type: "issue", summary: "x", tags: [t] });
    await create(token, w, { type: "document", title: "d", tags: [t] });
    await create(token, w, { type: "task", title: "t", tags: [t] });
    await create(token, w, { type: "note", path: "research/a.md", body: "n", tags: [t] });
    expect(await one(token, t, `?wiki=${w}`)).toEqual({ tag: t, total: 7, counts: { rule: 1, memory: 2, issue: 1, document: 1, task: 1, note: 1 } });
    // a retag moves the counts, a delete takes the record out
    await send("PATCH", rec(w, "/R1"), { token, body: { tags: [] } });
    await send("DELETE", rec(w, "/M1"), { token });
    expect(await one(token, t, `?wiki=${w}`)).toEqual({ tag: t, total: 5, counts: { memory: 1, issue: 1, document: 1, task: 1, note: 1 } });
    // closing a task keeps it live (still a task of the tag)
    await send("POST", rec(w, "/T1/close"), { token, body: { reason: "agent" } });
    expect((await one(token, t, `?wiki=${w}`))!.counts["task"]).toBe(1);
  });

  it("leaves the system tags out unless asked, and orders by count", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "rule", summary: "r1", tags: ["alpha", "stage:2", "project:p1"] });
    await create(token, w, { type: "rule", summary: "r2", tags: ["alpha", "beta"] });
    await create(token, w, { type: "idea", summary: "i" });
    const plain = (await tagsOf(token, `?wiki=${w}`)).map((x) => x.tag);
    expect(plain).toEqual(["alpha", "beta"]);
    const all = (await tagsOf(token, `?wiki=${w}&system=1`)).map((x) => x.tag);
    expect(all).toEqual(expect.arrayContaining(["alpha", "beta", "stage:2", "project:p1", "idea"]));
    expect((await tagsOf(token, `?wiki=${w}&prefix=be`)).map((x) => x.tag)).toEqual(["beta"]);
    expect((await get(`/v1/tags?wiki=${w}&limit=1`, token)).body.tags).toHaveLength(1);
    expect((await tool(token, "tags", { wiki: w })).text).toContain("alpha (2)");
  });

  it("stays correct for 10,000 distinct tags and answers fast", async () => {
    const { token, w } = await setup();
    for (let b = 0; b < 25; b++) {
      const ops = Array.from({ length: 4 }, (_, i) => {
        const n = b * 4 + i;
        return put(row(`R${n + 1}`, `wide ${n}`, { tags: Array.from({ length: 100 }, (_, k) => `wide-${n}-${k}`) }));
      });
      const r = await sync(token, w, { ops });
      expect(r.body.results.every((x: { status: string }) => x.status === "accepted")).toBe(true);
    }
    const started = Date.now();
    const r = await get(`/v1/tags?wiki=${w}&limit=1000`, token);
    expect(r.status).toBe(200);
    expect(r.body.tags).toHaveLength(1000);
    const session = await get(`/v1/instructions?wiki=${w}`, token);
    expect(session.status).toBe(200);
    expect(session.body.text.split("\n").filter((l: string) => l.startsWith("  wide-")).length).toBeLessThanOrEqual(40);
    expect(Date.now() - started).toBeLessThan(3000);
    // and removing the records takes their tags out
    await send("DELETE", rec(w, "/R1"), { token });
    expect((await tagsOf(token, `?wiki=${w}&limit=1000&prefix=wide-0-`)).length).toBe(0);
  });
});

describe("tags are the relations: search by tag", () => {
  it("fetches by tag alone, requires every tag, combines with words and ranks a lower stage first", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "rule", summary: "alpha one", tags: ["lang", "style"] });
    await create(token, w, { type: "rule", summary: "alpha two", tags: ["lang", "stage:3"] });
    await create(token, w, { type: "rule", summary: "alpha three", tags: ["lang", "stage:1"] });
    await create(token, w, { type: "memory", summary: "unrelated", tags: ["style"] });
    const lang = await get(`/v1/search?tags=lang&wiki=${w}`, token);
    expect(lang.body.results.map((r: { summary: string }) => r.summary)).toEqual(["alpha three", "alpha two", "alpha one"]); // stage 1, 3, none
    const both = await get(`/v1/search?tags=lang,style&wiki=${w}`, token);
    expect(both.body.results.map((r: { summary: string }) => r.summary)).toEqual(["alpha one"]);
    const words = await get(`/v1/search?q=alpha&tags=stage:3&wiki=${w}`, token);
    expect(words.body.results.map((r: { summary: string }) => r.summary)).toEqual(["alpha two"]);
    // on a words tie, the lower stage ranks first
    const tie = await get(`/v1/search?q=alpha&tags=lang&wiki=${w}`, token);
    expect(tie.body.results[0].summary).toBe("alpha three");
    expect((await get(`/v1/search?wiki=${w}`, token)).status).toBe(400);
    expect((await get(`/v1/search?q=%20&wiki=${w}`, token)).status).toBe(400);
    const viaMcp = await tool(token, "search", { tags: ["lang", "style"], wiki: w });
    expect(viaMcp.text).toContain("alpha one");
    expect(viaMcp.text).not.toContain("alpha two");
    expect((await tool(token, "search", { wiki: w })).isError).toBe(true);
  });
});

describe("tags are the relations: related", () => {
  it("lists linked records first (either direction), then shared tags, then newest; honors the limit", async () => {
    const { token, w } = await setup();
    const a = (await create(token, w, { type: "rule", summary: "anchor", tags: ["x", "y"] })).body.id;
    const sharedTwo = (await create(token, w, { type: "memory", summary: "shares two", tags: ["x", "y"] })).body.id;
    const sharedOne = (await create(token, w, { type: "memory", summary: "shares one", tags: ["x"] })).body.id;
    await create(token, w, { type: "memory", summary: "nothing", tags: ["z"] });
    const linkedOut = (await create(token, w, { type: "memory", summary: "linked out" })).body.id;
    // a record that links to the anchor, and one the anchor links to (sync carries links)
    const inbound = row("I7", "links to anchor", { links: [[a, "related"]] });
    await sync(token, w, { ops: [put(inbound)] });
    const anchor = (await get(rec(w, `/${a}`), token)).body;
    await sync(token, w, { ops: [put({ ...anchor, updated: "2026-06-01T00:00:00Z", links: [[linkedOut, "about"]] }, anchor.rev)] });
    const r = await get(rec(w, `/${a}/related`), token);
    expect(r.status).toBe(200);
    const ids = r.body.related.map((h: { id: string }) => h.id);
    expect(new Set(ids.slice(0, 2))).toEqual(new Set([linkedOut, "I7"]));
    expect(ids.slice(2)).toEqual([sharedTwo, sharedOne]);
    expect(r.body.related[0].via).toBe("link");
    expect(r.body.related[2]).toMatchObject({ via: "tags", shared: 2 });
    expect((await get(rec(w, `/${a}/related?limit=1`), token)).body.related).toHaveLength(1);
    expect((await get(rec(w, `/${a}/related?limit=51`), token)).status).toBe(400);
    expect((await get(rec(w, `/NOPE/related`), token)).status).toBe(404);
    expect((await tool(token, "related", { id: a, wiki: w })).text).toContain("shares two");
  });
});

describe("tags, tasks, projects and related honor the device's wiki list", () => {
  it("shows a device limited to one wiki nothing of another", async () => {
    const a = await setup();
    const b = await setup();
    await wiki(a.token, "global", "global");
    const secret = `marker-${freshKey()}`;
    await create(a.token, b.w, { type: "rule", summary: `hidden ${secret}`, tags: [secret] });
    await create(a.token, b.w, { type: "human_task", title: `hidden task ${secret}`, project: secret.toLowerCase() });
    await create(a.token, a.w, { type: "rule", summary: "visible rule", tags: ["visible-tag"] });
    const lim = await mint("limited", ["read", "write"], [a.w]);
    expect((await tagsOf(lim.token)).map((t) => t.tag)).toContain("visible-tag");
    expect(JSON.stringify(await tagsOf(lim.token, "?system=1"))).not.toContain(secret);
    expect((await get(`/v1/tags?wiki=${b.w}`, lim.token)).status).toBe(403);
    expect((await get(`/v1/search?tags=${secret}`, lim.token)).body.results).toEqual([]);
    expect((await get(`/v1/search?tags=${secret}&wiki=${b.w}`, lim.token)).status).toBe(403);
    expect((await get(rec(b.w, "/R1/related"), lim.token)).status).toBe(403);
    expect((await get(`/v1/projects/${secret.toLowerCase()}?wiki=${b.w}`, lim.token)).status).toBe(403);
    expect((await get(`/v1/projects?wiki=${b.w}`, lim.token)).status).toBe(403);
    const tasks = await get("/v1/tasks", lim.token);
    expect(JSON.stringify(tasks.body)).not.toContain(secret);
    expect((await get(`/v1/tasks?wiki=${b.w}`, lim.token)).status).toBe(403);
    expect(JSON.stringify((await tool(lim.token, "tags", { system: true })).text)).not.toContain(secret);
    expect((await tool(lim.token, "tasks", {})).text).not.toContain(secret);
    expect((await tool(lim.token, "related", { id: "R1", wiki: b.w })).isError).toBe(true);
    expect((await get("/v1/instructions?full=1", lim.token)).body.text).not.toContain(secret);
  });
});
