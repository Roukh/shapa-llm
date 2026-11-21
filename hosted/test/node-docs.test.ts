// Repo nodes (the local engine's node.py writes one document per repo into the global wiki): hidden from a baseline
// device, and the alias prefix `node:` and the `shapa-node` tag belong to the operator's sync alone.

import { describe, expect, it } from "vitest";
import { OWNER, TS, freshKey, get, mint, put, rec, send, setup, sync, uidOf, wiki } from "./helpers.js";
import { sessionText, tool } from "./mcp-helpers.js";

const nodeDoc = (id: string, key: string, body: string, over: Record<string, unknown> = {}) => ({
  id, uid: uidOf(id, TS), kind: "D", created: TS, updated: TS, deleted: false,
  data: { title: `node: ${key}`, body, alias: `node:${key}`, mime: "application/json", source: "node" },
  tags: ["shapa-node"], links: [], ...over,
});

async function world() {
  const { token: op } = await setup();
  const g = "global";
  await wiki(op, g, "global");
  const b = await send("POST", "/admin/devices", { token: OWNER, body: { name: `outside-${freshKey()}`, scopes: ["read", "write", "delete"], reach: "baseline" } });
  expect(b.status).toBe(201);
  return { op, g, base: b.body.token as string };
}

describe("repo nodes", () => {
  it("is stored by the operator's sync and never visible to a baseline device (REST, MCP, search, session text)", async () => {
    const { op, g, base } = await world();
    const k = freshKey();
    const secret = `NODEPRINCIPLE${k.replace(/\W/g, "")}`;
    const r = await sync(op, g, { ops: [put(nodeDoc("D31", `repo-${k}`, JSON.stringify({ key: `repo-${k}`, principles: [["R1", "2026-01-01", secret]] })))] });
    expect(r.body.results[0].status).toBe("accepted");
    expect((await get(rec(g, "/D31"), op)).status).toBe(200);
    expect((await get(rec(g, "/D31"), base)).status).toBe(404);
    expect((await get(rec(g, `/${encodeURIComponent(`node:repo-${k}`)}`), base)).status).toBe(404);
    expect((await get(`/v1/search?q=${secret}&wiki=${g}`, base)).body.results).toEqual([]);
    expect((await get(`/v1/search?q=${secret}&wiki=${g}`, op)).body.results.length).toBeGreaterThan(0);
    for (const [name, args] of [["search", { query: secret }], ["get", { id: "D31", wiki: g }], ["get", { id: `node:repo-${k}`, wiki: g }]] as const) {
      expect((await tool(base, name, args as Record<string, unknown>)).text, name).not.toContain(secret);
    }
    expect(await sessionText(base)).not.toContain(secret);
  });

  it("refuses the node: alias and the shapa-node tag on every create and edit, from a baseline or a full device", async () => {
    const { op, g, base } = await world();
    const k = freshKey();
    for (const token of [op, base]) {
      for (const alias of [`node:evil-${k}`, `Node:evil-${k}`, ` node:evil-${k}`]) {
        const c = await send("POST", rec(g), { token, body: { type: "document", title: "x", body: "y", alias } });
        expect(c.status, `${alias} create`).toBe(400);
      }
      const t = await send("POST", rec(g), { token, body: { type: "document", title: "x", body: "y", tags: ["shapa-node"] } });
      expect([400, 403]).toContain(t.status);
    }
    const doc = await send("POST", rec(g), { token: base, body: { type: "document", title: `plain ${k}`, body: "y" } });
    expect(doc.status).toBe(201);
    const id = (doc.body.record?.id ?? doc.body.id) as string;
    for (const token of [op, base]) {
      const a = await send("PATCH", rec(g, `/${id}`), { token, body: { alias: `node:evil-${k}` } });
      expect(a.status, "alias edit").toBe(400);
      const t = await send("PATCH", rec(g, `/${id}`), { token, body: { tags: ["shapa-node"] } });
      expect([400, 403]).toContain(t.status);
    }
    expect((await tool(base, "save", { type: "document", summary: "x", body: "y", id: `node:evil-${k}`, wiki: g })).isError).toBe(true);
  });

  it("refuses the node: alias named by a note's frontmatter id, on create and on an edit of the body", async () => {
    const { op, g, base } = await world();
    const k = freshKey();
    for (const token of [op, base]) {
      const c = await send("POST", rec(g), { token, body: { type: "note", path: `n-${freshKey()}.md`, body: `---\nid: node:evil-${k}\n---\nx` } });
      expect(c.status, "note create").toBe(400);
    }
    const made = await send("POST", rec(g), { token: base, body: { type: "note", path: `plain-${k}.md`, body: "plain" } });
    expect(made.status).toBe(201);
    const id = (made.body.record?.id ?? made.body.id) as string;
    const e = await send("PATCH", rec(g, `/${encodeURIComponent(id)}`), { token: base, body: { body: `---\nid: NODE:evil-${k}\n---\nx` } });
    expect(e.status, "note edit").toBe(400);
  });

  it("refuses a node edited through the API even by a full device, and a baseline device does not sync", async () => {
    const { op, g, base } = await world();
    const k = freshKey();
    await sync(op, g, { ops: [put(nodeDoc("D41", `repo-${k}`, "{}"))] });
    const e = await send("PATCH", rec(g, "/D41"), { token: op, body: { body: "{}" } });
    expect(e.status).toBe(400);
    expect((await sync(base, g, { ops: [put(nodeDoc("D42", `x-${k}`, "{}"))] })).status).toBe(403);
  });

  it("accepts a node put only from the operator's own device (delete scope), not from a read/write app token", async () => {
    const { op, g } = await world();
    const k = freshKey();
    const app = (await mint(`app-${k}`, ["read", "write"])).token;
    const forged = (await sync(app, g, { ops: [put(nodeDoc("D61", `forged-${k}`, "{}"))] })).body.results[0];
    expect(forged.status).toBe("rejected");
    expect(forged.error).toContain("operator's own device");
    expect((await get(rec(g, "/D61"), op)).status).toBe(404);
    // the alias alone is as good as the tag
    const aliasOnly = nodeDoc("D62", `alias-${k}`, "{}", { tags: [] });
    expect((await sync(app, g, { ops: [put(aliasOnly)] })).body.results[0].status).toBe("rejected");
  });

  it("rejects a sync put whose alias prefix, tag and source do not go together", async () => {
    const { op, g } = await world();
    const k = freshKey();
    const noTag = nodeDoc("D51", `a-${k}`, "{}", { tags: [] });
    const noAlias = nodeDoc("D52", `b-${k}`, "{}", { data: { title: "t", body: "{}", alias: null, mime: "application/json", source: "node" } });
    const noSource = nodeDoc("D53", `c-${k}`, "{}", { data: { title: "t", body: "{}", alias: `node:c-${k}`, mime: "application/json", source: "save" } });
    const r = await sync(op, g, { ops: [put(noTag), put(noAlias), put(noSource)] });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(["rejected", "rejected", "rejected"]);
  });
});
