import { describe, expect, it } from "vitest";
import { freshKey, get, mint, rec, send, setup, wiki } from "./helpers.js";

const create = (token: string, w: string, body: unknown) => send("POST", rec(w), { token, body });

describe("REST: create", () => {
  it("allocates ids per kind and nests job under feature under the ledger rules", async () => {
    const { token, w } = await setup();
    const f = await create(token, w, { type: "feature", title: "Hosted mode" });
    expect(f.status).toBe(201);
    expect(f.body).toMatchObject({ id: "F1", kind: "F", deleted: false, device: "tester" });
    expect(f.body.data).toMatchObject({ title: "Hosted mode", status: "open", parent: null });
    expect(f.body.rev).toBeGreaterThan(0);

    const j = await create(token, w, { type: "job", title: "Store", parent: "F1" });
    expect(j.body).toMatchObject({ id: "J1" });
    expect(j.body.data.parent).toBe("F1");
    const t = await create(token, w, { type: "task", title: "Schema", parent: "J1" });
    expect(t.body.id).toBe("T1");

    // a job's parent is a feature, a task's a job; a feature has none
    expect((await create(token, w, { type: "job", title: "x", parent: "J1" })).status).toBe(400);
    expect((await create(token, w, { type: "task", title: "x", parent: "F1" })).status).toBe(400);
    expect((await create(token, w, { type: "feature", title: "x", parent: "F1" })).status).toBe(400);
    expect((await create(token, w, { type: "job", title: "x", parent: "F99" })).status).toBe(404);
    // standalone is fine
    expect((await create(token, w, { type: "job", title: "alone" })).body.id).toBe("J2");
  });

  it("gives every created record a server-made uid and ignores a client-chosen one", async () => {
    const { token, w } = await setup();
    const chosen = "client-chosen-uid-0001";
    const a = await create(token, w, { type: "memory", summary: "one", uid: chosen });
    const b = await create(token, w, { type: "memory", summary: "two" });
    const f = await create(token, w, { type: "feature", title: "feat" });
    const j = await create(token, w, { type: "job", title: "job", parent: "F1" });
    const d = await create(token, w, { type: "document", title: "doc" });
    for (const r of [a, b, f, j, d]) expect(r.body.uid).toMatch(/^[A-Za-z0-9_-]{16,64}$/);
    expect(a.body.uid).not.toBe(chosen);
    expect(new Set([a, b, f, j, d].map((r) => r.body.uid)).size).toBe(5);
    // a note has no uid; the uid survives edits, claims and deletion
    const note = await create(token, w, { type: "note", path: "research/u.md", body: "x" });
    expect(note.body.uid).toBe("");
    const patched = await send("PATCH", rec(w, "/M1"), { token, body: { summary: "edited", uid: "other-uid-0123456789" } });
    expect(patched.body.uid).toBe(a.body.uid);
    expect((await send("POST", rec(w, "/J1/claim"), { token })).body.uid).toBe(j.body.uid);
    expect((await get(rec(w, "/M1"), token)).body.uid).toBe(a.body.uid);
    const gone = await send("DELETE", rec(w, "/M2"), { token });
    expect(gone.body.deleted).toBe(true);
    expect(gone.body.uid).toBe(b.body.uid);
    // identical text returns the first row, with its uid
    expect((await create(token, w, { type: "memory", summary: "edited" })).body.uid).toBe(a.body.uid);
  });

  it("refuses a child under a closed parent", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "feature", title: "f" });
    await send("POST", rec(w, "/F1/close"), { token, body: { reason: "agent" } });
    const r = await create(token, w, { type: "job", title: "late", parent: "F1" });
    expect(r.status).toBe(409);
    expect(r.body.error.code).toBe("conflict");
  });

  it("returns the existing row for identical text and merges tags", async () => {
    const { token, w } = await setup();
    const a = await create(token, w, { type: "memory", summary: "Same words", body: "detail" });
    const b = await create(token, w, { type: "memory", summary: "Same  words", body: "detail", tags: ["x"] });
    expect(b.status).toBe(200);
    expect(b.body.id).toBe(a.body.id);
    expect(b.body.tags).toEqual(["x"]);
    // another kind with the same text is a different row
    expect((await create(token, w, { type: "rule", summary: "Same words", body: "detail" })).body.id).toBe("R1");
  });

  it("stores ideas as tagged memories, documents and notes", async () => {
    const { token, w } = await setup();
    const idea = await create(token, w, { type: "idea", summary: "try a thing" });
    expect(idea.body).toMatchObject({ id: "M1", tags: ["idea"] });
    const doc = await create(token, w, { type: "document", title: "Spec", body: "# Spec", mime: "text/markdown" });
    expect(doc.body).toMatchObject({ id: "D1", kind: "D" });
    const note = await create(token, w, { type: "note", path: "arch/hosted.md", body: "---\nid: hosted\nsummary: Hosted design\n---\n# Hosted" });
    expect(note.body).toMatchObject({ id: "arch/hosted.md", kind: "N", created: "" });
    expect(note.body.data).toMatchObject({ alias: "hosted", summary: "Hosted design" });
    expect((await create(token, w, { type: "note", path: "arch/hosted.md", body: "again" })).status).toBe(409);
    // by alias and by URL-encoded path
    expect((await get(rec(w, "/hosted"), token)).body.id).toBe("arch/hosted.md");
    expect((await get(rec(w, "/arch%2Fhosted.md"), token)).body.id).toBe("arch/hosted.md");
  });

  it("rejects hostile note paths", async () => {
    const { token, w } = await setup();
    const bad = ["../x.md", "/etc/passwd.md", "a\\b.md", "arch/../x.md", "x\u0000.md", "x.txt", "AGENTS.md", "agents.md", "placement.md", "sub/dir/x.md", ".hidden.md", "arch/", ""];
    for (const path of bad) {
      const r = await create(token, w, { type: "note", path, body: "x" });
      expect(r.status, JSON.stringify(path)).toBe(400);
    }
    expect((await create(token, w, { type: "note", path: "research/ok-1.md", body: "x" })).status).toBe(201);
  });

  it("enforces aliases as unique among live records", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "memory", summary: "one", alias: "pref" });
    const r = await create(token, w, { type: "memory", summary: "two", alias: "pref" });
    expect(r.status).toBe(409);
    // an alias that looks like an id is dropped, never shadows the id
    const ok = await create(token, w, { type: "memory", summary: "three", alias: "M1" });
    expect(ok.body.data.alias).toBeNull();
    expect((await get(rec(w, "/pref"), token)).body.id).toBe("M1");
  });

  it("masks secrets in every text field", async () => {
    const { token, w } = await setup();
    const key = ["sk-an", "t-api03-", "A1b2C3d4E5f6G7h8I9j0"].join("");
    const r = await create(token, w, { type: "memory", summary: `use ${key} now`, body: `Bearer abcdefghij.klmnop` });
    expect(r.body.data.summary).toBe("use [REDACTED] now");
    expect(r.body.data.body).toBe("Bearer [REDACTED]");
    expect(JSON.stringify((await get(rec(w, "/M1"), token)).body)).not.toContain("A1b2C3d4");
  });

  it("validates input", async () => {
    const { token, w } = await setup();
    expect((await create(token, w, { type: "memory" })).status).toBe(400);
    expect((await create(token, w, { type: "bogus", summary: "x" })).status).toBe(400);
    expect((await create(token, w, { type: "memory", summary: "x", parent: "F1" })).status).toBe(400);
    expect((await send("POST", rec(w), { token, raw: "{not json" })).status).toBe(400);
    expect((await create(token, "no-such-wiki", { type: "memory", summary: "x" })).status).toBe(404);
  });
});

describe("REST: read and edit", () => {
  it("gets, lists with filters and pages", async () => {
    const { token, w } = await setup();
    for (let i = 1; i <= 5; i++) await create(token, w, { type: "memory", summary: `memory ${i}`, tags: i % 2 ? ["odd"] : [] });
    await create(token, w, { type: "feature", title: "a feature" });
    const all = await get(rec(w, "?kind=M&limit=2"), token);
    expect(all.body.records.map((r: { id: string }) => r.id)).toEqual(["M1", "M2"]);
    expect(all.body.next).toBe(all.body.records[1].rev);
    const next = await get(rec(w, `?kind=M&limit=2&after=${all.body.next}`), token);
    expect(next.body.records.map((r: { id: string }) => r.id)).toEqual(["M3", "M4"]);
    const odd = await get(rec(w, "?tag=odd"), token);
    expect(odd.body.records).toHaveLength(3);
    expect((await get(rec(w, "?kind=F&status=open"), token)).body.records).toHaveLength(1);
    expect((await get(rec(w, "?kind=F&status=closed"), token)).body.records).toHaveLength(0);
    expect((await get(rec(w, "/M9"), token)).status).toBe(404);
    expect((await get(rec(w, "?limit=9999"), token)).status).toBe(400);
  });

  it("edits fields and guards lost updates with base_rev", async () => {
    const { token, w } = await setup();
    const m = (await create(token, w, { type: "memory", summary: "old", tags: ["a"] })).body;
    const p = await send("PATCH", rec(w, "/M1"), { token, body: { summary: "new", body: "b", tags: ["z", "y"], base_rev: m.rev } });
    expect(p.status).toBe(200);
    expect(p.body.data).toMatchObject({ summary: "new", body: "b" });
    expect(p.body.tags).toEqual(["y", "z"]);
    expect(p.body.rev).toBeGreaterThan(m.rev);

    const stale = await send("PATCH", rec(w, "/M1"), { token, body: { summary: "lost", base_rev: m.rev } });
    expect(stale.status).toBe(409);
    expect(stale.body.error.code).toBe("conflict");
    expect(stale.body.error.current.data.summary).toBe("new");

    // an edit that changes nothing does not take a new rev
    const same = await send("PATCH", rec(w, "/M1"), { token, body: { summary: "new" } });
    expect(same.body.rev).toBe(p.body.rev);

    expect((await send("PATCH", rec(w, "/M404"), { token, body: { summary: "x" } })).status).toBe(404);
    expect((await send("PATCH", rec(w, "/M1"), { token, body: { summary: "   " } })).status).toBe(400);
  });

  it("moves a job between features and edits ledger fields", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "feature", title: "one" });
    await create(token, w, { type: "feature", title: "two" });
    await create(token, w, { type: "job", title: "j", parent: "F1" });
    const moved = await send("PATCH", rec(w, "/J1"), { token, body: { parent: "F2", git_ref: "abc", title: "renamed" } });
    expect(moved.body.data).toMatchObject({ parent: "F2", git_ref: "abc", title: "renamed" });
    expect((await send("PATCH", rec(w, "/J1"), { token, body: { parent: "J1" } })).status).toBe(400);
    expect((await send("PATCH", rec(w, "/F1"), { token, body: { parent: "F2" } })).status).toBe(400);
  });
});

describe("REST: ledger moves", () => {
  it("claims, releases and closes with a cascade", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "feature", title: "f" });
    await create(token, w, { type: "job", title: "j", parent: "F1" });
    await create(token, w, { type: "task", title: "t", parent: "J1" });
    await create(token, w, { type: "task", title: "t2", parent: "J1" });

    const c = await send("POST", rec(w, "/J1/claim"), { token });
    expect(c.body.data).toMatchObject({ status: "claimed", claimed_by: "tester" });
    const r = await send("POST", rec(w, "/J1/release"), { token });
    expect(r.body.data).toMatchObject({ status: "open", claimed_by: null });
    // releasing what is not claimed changes nothing
    expect((await send("POST", rec(w, "/J1/release"), { token })).body.rev).toBe(r.body.rev);

    await send("POST", rec(w, "/T1/close"), { token, body: { reason: "agent" } });
    const closed = await send("POST", rec(w, "/F1/close"), { token, body: { reason: "operator", git_ref: "abc123" } });
    expect(closed.body.closed).toEqual(["F1", "J1", "T2"]);
    expect(closed.body.record.data).toMatchObject({ status: "closed", closed_reason: "operator", git_ref: "abc123" });
    expect((await get(rec(w, "/J1"), token)).body.data).toMatchObject({ status: "closed", closed_reason: "parent" });
    expect((await get(rec(w, "/T1"), token)).body.data.closed_reason).toBe("agent");

    expect((await send("POST", rec(w, "/F1/close"), { token, body: { reason: "agent" } })).body.closed).toEqual([]);
    expect((await send("POST", rec(w, "/F1/claim"), { token })).status).toBe(409);
    expect((await send("POST", rec(w, "/F1/close"), { token, body: { reason: "merge" } })).status).toBe(400);
    expect((await create(token, w, { type: "memory", summary: "m" })).status).toBe(201);
    expect((await send("POST", rec(w, "/M1/claim"), { token })).status).toBe(400);
  });
});

describe("REST: delete and ids", () => {
  it("tombstones with the delete scope, and never reuses an id", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "memory", summary: "gone soon", alias: "soon" });
    const rw = await mint("writer", ["read", "write"]);
    expect((await send("DELETE", rec(w, "/M1"), { token: rw.token })).status).toBe(403);

    const gone = await send("DELETE", rec(w, "/M1"), { token });
    expect(gone.status).toBe(200);
    expect(gone.body).toMatchObject({ id: "M1", deleted: true, data: {}, tags: [], links: [] });
    expect((await get(rec(w, "/M1"), token)).status).toBe(404);
    expect((await get(rec(w, "/soon"), token)).status).toBe(404);
    expect((await get("/v1/search?q=gone+soon", token)).body.results).toEqual([]);
    expect((await create(token, w, { type: "memory", summary: "next" })).body.id).toBe("M2");
    // the alias is free again
    expect((await create(token, w, { type: "memory", summary: "taken over", alias: "soon" })).status).toBe(201);
  });
});

describe("REST: scopes and wiki allowlists", () => {
  it("enforces read, write and delete on every route", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "memory", summary: "x" });
    const ro = await mint("reader", ["read"]);
    const status = async (t: string, m: string, p: string, b?: unknown) => (await send(m, p, { token: t, ...(b ? { body: b } : {}) })).status;
    for (const [m, p, b] of [
      ["POST", rec(w), { type: "memory", summary: "y" }],
      ["PATCH", rec(w, "/M1"), { summary: "z" }],
      ["POST", rec(w, "/M1/claim"), undefined],
      ["POST", rec(w, "/M1/close"), { reason: "agent" }],
      ["DELETE", rec(w, "/M1"), undefined],
      ["PUT", `/v1/wikis/${freshKey()}`, { name: "n", kind: "repo" }],
      ["POST", `/v1/wikis/${w}/sync`, { cursor: 0, ops: [], want: [], counters: {} }],
    ] as [string, string, unknown][]) {
      expect(await status(ro.token, m, p, b), `${m} ${p}`).toBe(403);
    }
    expect(await status(ro.token, "GET", rec(w, "/M1"))).toBe(200);
  });

  it("keeps a restricted device inside its wiki list", async () => {
    const a = await setup();
    const other = freshKey();
    await wiki(a.token, other);
    await create(a.token, a.w, { type: "memory", summary: "in allowed wiki zebra" });
    await create(a.token, other, { type: "memory", summary: "in other wiki zebra" });
    const lim = await mint("limited", ["read", "write", "delete"], [a.w]);

    expect((await get(rec(other), lim.token)).status).toBe(403);
    expect((await get(rec(other, "/M1"), lim.token)).status).toBe(403);
    expect((await create(lim.token, other, { type: "memory", summary: "sneak" })).status).toBe(403);
    expect((await send("DELETE", rec(other, "/M1"), { token: lim.token })).status).toBe(403);
    expect((await send("POST", `/v1/wikis/${other}/sync`, { token: lim.token, body: {} })).status).toBe(403);
    expect((await send("PUT", `/v1/wikis/${freshKey()}`, { token: lim.token, body: { name: "n", kind: "repo" } })).status).toBe(403);
    expect((await get(`/v1/search?q=zebra&wiki=${other}`, lim.token)).status).toBe(403);
    expect((await get(`/v1/get?id=M1&wiki=${other}`, lim.token)).status).toBe(403);

    // unscoped reads see only the allowed wiki
    const hits = (await get("/v1/search?q=zebra", lim.token)).body.results;
    expect(hits.map((h: { wiki: string }) => h.wiki)).toEqual([a.w]);
    expect((await get("/v1/get?id=M1", lim.token)).body.matches.map((m: { wiki: string }) => m.wiki)).toEqual([a.w]);
    expect((await get("/v1/wikis", lim.token)).body.wikis.map((x: { key: string }) => x.key)).toEqual([a.w]);
    expect((await create(lim.token, a.w, { type: "memory", summary: "ok" })).status).toBe(201);
    // the device that can see everything still sees both
    expect((await get("/v1/search?q=zebra", a.token)).body.results).toHaveLength(2);
  });
});

describe("REST: wikis", () => {
  it("registers idempotently and guards the origin", async () => {
    const d = await mint("w", ["read", "write"]);
    const key = freshKey();
    const first = await send("PUT", `/v1/wikis/${key}`, { token: d.token, body: { name: "Repo", kind: "repo", origin: "example.com/o/r" } });
    expect(first.status).toBe(201);
    const again = await send("PUT", `/v1/wikis/${key}`, { token: d.token, body: { name: "Repo renamed", kind: "repo", origin: "example.com/o/r" } });
    expect(again.status).toBe(200);
    expect(again.body.name).toBe("Repo renamed");
    expect((await send("PUT", `/v1/wikis/${key}`, { token: d.token, body: { name: "x", kind: "repo", origin: "example.com/o/other" } })).status).toBe(409);
    expect((await send("PUT", `/v1/wikis/${key}`, { token: d.token, body: { name: "x", kind: "global" } })).status).toBe(400);
    expect((await send("PUT", `/v1/wikis/Bad_Key`, { token: d.token, body: { name: "x", kind: "repo" } })).status).toBe(400);
    expect((await send("PUT", `/v1/wikis/${freshKey()}`, { token: d.token, body: { name: "x", kind: "repo", origin: "https://user:pass@host/r" } })).status).toBe(400);
    expect((await send("PUT", `/v1/wikis/global`, { token: d.token, body: { name: "g", kind: "repo" } })).status).toBe(400);
  });
});

describe("REST: errors and docs", () => {
  it("answers in the documented error shape", async () => {
    const { token } = await setup();
    const nf = await get("/v1/nope", token);
    expect(nf.status).toBe(404);
    const r = await send("GET", "/no/such/route");
    expect(r.status).toBe(404);
    expect(r.body).toEqual({ error: { code: "not_found", message: "no such route" } });
    const unauth = await get("/v1/whoami");
    expect(unauth.status).toBe(401);
    expect(unauth.body.error.code).toBe("unauthorized");
    expect(unauth.headers.get("www-authenticate")).toContain("Bearer");
  });

  it("generates openapi.json from the schemas", async () => {
    const doc = (await get("/openapi.json")).body;
    expect(doc.openapi).toMatch(/^3\.1/);
    for (const p of [
      "/v1/whoami", "/v1/wikis", "/v1/wikis/{wiki}", "/v1/search", "/v1/get",
      "/v1/wikis/{wiki}/records", "/v1/wikis/{wiki}/records/{id}", "/v1/wikis/{wiki}/records/{id}/claim",
      "/v1/wikis/{wiki}/records/{id}/release", "/v1/wikis/{wiki}/records/{id}/close", "/v1/wikis/{wiki}/sync",
      "/admin/devices", "/admin/devices/{id}",
    ]) expect(doc.paths, p).toHaveProperty([p]);
    expect(doc.components.schemas).toHaveProperty(["SyncRequest"]);
    expect(doc.components.securitySchemes).toHaveProperty(["bearer"]);
    expect(doc.paths["/v1/wikis/{wiki}/sync"].post.requestBody.content["application/json"].schema).toBeDefined();
  });
});
