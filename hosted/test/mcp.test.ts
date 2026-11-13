import { describe, expect, it } from "vitest";
import { BASE, call, get, mint, mintAll, rec, send, setup, wiki } from "./helpers.js";
import { INIT, rpc, tool } from "./mcp-helpers.js";
import { oauthLogin } from "./oauth-helpers.js";

const TOOLS = ["session_start", "search", "get", "save", "update", "ledger_list", "ledger_add", "ledger_set", "wikis"];

describe("MCP: discovery", () => {
  it("publishes the protected resource and challenges an unauthenticated caller", async () => {
    const pr = await (await call("/.well-known/oauth-protected-resource/mcp")).json<any>();
    expect(pr).toMatchObject({ resource: `${BASE}/mcp`, authorization_servers: [BASE] });
    const unauth = await call("/mcp", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
    expect(unauth.status).toBe(401);
    expect(unauth.headers.get("www-authenticate")).toContain("resource_metadata=");
    const as = await (await call("/.well-known/oauth-authorization-server")).json<any>();
    expect(as.protected_resources).toEqual([`${BASE}/mcp`]);
  });
});

describe("MCP: protocol surface", () => {
  it("initializes with instructions that say to call session_start first", async () => {
    const { token } = await setup();
    const r = await rpc(token, "initialize", INIT);
    expect(r.status).toBe(200);
    expect(r.json.result.serverInfo.name).toBe("shapa");
    expect(r.json.result.instructions).toMatch(/Call `session_start` first/);
    expect(r.json.result.capabilities.tools).toBeDefined();
  });

  it("lists exactly the contract's tools, with no delete tool and honest annotations", async () => {
    const { token } = await setup();
    const r = await rpc(token, "tools/list");
    const tools: { name: string; annotations?: Record<string, boolean>; description: string; inputSchema: unknown }[] = r.json.result.tools;
    expect(tools.map((t) => t.name).sort()).toEqual([...TOOLS].sort());
    expect(tools.some((t) => /delete|remove|forget/i.test(t.name))).toBe(false);
    const by = Object.fromEntries(tools.map((t) => [t.name, t]));
    for (const ro of ["session_start", "search", "get", "ledger_list", "wikis"]) expect(by[ro]!.annotations?.readOnlyHint, ro).toBe(true);
    for (const rw of ["save", "update", "ledger_add", "ledger_set"]) expect(by[rw]!.annotations?.readOnlyHint, rw).toBe(false);
    expect(by["update"]!.annotations?.destructiveHint).toBe(true);
    for (const t of tools) {
      expect(t.description.length, t.name).toBeGreaterThan(20);
      expect(t.inputSchema).toBeTruthy();
    }
  });

  it("refuses an unauthenticated or revoked caller with a discovery challenge", async () => {
    const none = await rpc(undefined, "tools/list");
    expect(none.status).toBe(401);
    expect(none.headers.get("www-authenticate")).toContain("resource_metadata");
    expect((await rpc("shp_0000000000000000_" + "0".repeat(64), "tools/list")).status).toBe(401);
    const d = await mintAll("gone");
    expect((await rpc(d.token, "tools/list")).status).toBe(200);
    await send("DELETE", `/admin/devices/${d.id}`, { token: "test-owner-key-0123456789abcdef0123456789" });
    expect((await rpc(d.token, "tools/list")).status).toBe(401);
  });
});

describe("MCP: tools call the same store as REST", () => {
  it("saves, searches, gets and updates, visible through REST", async () => {
    const { token, w } = await setup();
    const saved = await tool(token, "save", { wiki: w, type: "memory", summary: "the operator keeps backups on a NAS", tags: ["ops"], alias: "backups" });
    expect(saved.isError).toBe(false);
    expect(saved.text).toBe(`Saved M1 in ${w}.`);
    expect((await tool(token, "save", { wiki: w, type: "memory", summary: "the operator keeps backups on a NAS" })).text).toBe(`Already saved as M1 in ${w}.`);
    expect((await get(rec(w, "/backups"), token)).body.id).toBe("M1");

    const found = await tool(token, "search", { query: "backups NAS" });
    expect(found.text).toBe(`${w}/M1 [memory] the operator keeps backups on a NAS`);
    expect((await tool(token, "search", { query: "zzzznothing" })).text).toBe("No matches.");
    const got = JSON.parse((await tool(token, "get", { id: "backups" })).text);
    expect(got[0]).toMatchObject({ wiki: w, id: "M1", rev: expect.any(Number) });

    const upd = await tool(token, "update", { wiki: w, id: "M1", summary: "backups moved to the cloud", base_rev: got[0].rev });
    expect(upd.text).toMatch(/^Updated M1/);
    const stale = await tool(token, "update", { wiki: w, id: "M1", summary: "lost", base_rev: got[0].rev });
    expect(stale.isError).toBe(true);
    expect(stale.text).toMatch(/^conflict:/);
    expect(stale.text).toContain("backups moved to the cloud");
    expect((await get(rec(w, "/M1"), token)).body.data.summary).toBe("backups moved to the cloud");
  });

  it("saves documents, notes, rules, issues and ideas", async () => {
    const { token, w } = await setup();
    expect((await tool(token, "save", { wiki: w, type: "document", title: "Spec", body: "text" })).text).toBe(`Saved D1 in ${w}.`);
    expect((await tool(token, "save", { wiki: w, type: "note", path: "research/x.md", body: "# X" })).text).toBe(`Saved research/x.md in ${w}.`);
    expect((await tool(token, "save", { wiki: w, type: "rule", summary: "always run tests" })).text).toBe(`Saved R1 in ${w}.`);
    expect((await tool(token, "save", { wiki: w, type: "issue", summary: "forgot the tests" })).text).toBe(`Saved I1 in ${w}.`);
    const idea = await tool(token, "save", { wiki: w, type: "idea", summary: "try X" });
    expect(idea.text).toBe(`Saved M1 in ${w}.`);
    expect((await get(rec(w, "/M1"), token)).body.tags).toEqual(["idea"]);
    expect((await tool(token, "search", { query: "try", types: ["idea"] })).text).toContain("M1");
    expect((await tool(token, "search", { query: "try", types: ["rule"] })).text).toBe("No matches.");
    const bad = await tool(token, "save", { wiki: w, type: "note", path: "../x.md", body: "x" });
    expect(bad.isError).toBe(true);
    expect(bad.text).toMatch(/^invalid:/);
  });

  it("runs the ledger: add, list, claim, edit, close", async () => {
    const { token, w } = await setup();
    expect((await tool(token, "ledger_add", { wiki: w, kind: "feature", title: "Hosted" })).text).toBe(`Added F1 in ${w}.`);
    expect((await tool(token, "ledger_add", { wiki: w, kind: "job", title: "Store", parent: "F1" })).text).toBe(`Added J1 in ${w}.`);
    expect((await tool(token, "ledger_add", { wiki: w, kind: "task", title: "Schema", parent: "J1" })).text).toBe(`Added T1 in ${w}.`);
    expect((await tool(token, "ledger_add", { wiki: w, kind: "job", title: "x", parent: "T1" })).isError).toBe(true);

    expect((await tool(token, "ledger_set", { wiki: w, id: "J1", action: "claim" })).text).toBe("Claimed J1.");
    const list = await tool(token, "ledger_list", { wiki: w });
    expect(list.text).toBe(`# ${w}\nF1 [feature] Hosted\n  J1 [job] Store (claimed by tester)\n    T1 [task] Schema`);
    expect((await tool(token, "ledger_set", { wiki: w, id: "J1", action: "edit", fields: { title: "Store v2", git_ref: "abc" } })).text).toMatch(/^Edited J1/);
    expect((await tool(token, "ledger_set", { wiki: w, id: "J1", action: "release" })).text).toBe("Released J1.");
    expect((await tool(token, "ledger_set", { wiki: w, id: "F1", action: "close", fields: { reason: "operator", git_ref: "abc" } })).text).toBe("Closed F1, J1, T1.");
    expect((await tool(token, "ledger_set", { wiki: w, id: "F1", action: "close" })).text).toBe("F1 was already closed.");
    expect((await tool(token, "ledger_list", { wiki: w })).text).toBe(`# ${w}\n(no live work)`);
    expect((await get(rec(w, "/J1"), token)).body.data).toMatchObject({ title: "Store v2", status: "closed", closed_reason: "parent" });
  });

  it("lists wikis and validates tool arguments", async () => {
    const { token, w } = await setup();
    const wikis = JSON.parse((await tool(token, "wikis")).text);
    expect(wikis.map((x: { key: string }) => x.key)).toContain(w);
    const r = await rpc(token, "tools/call", { name: "save", arguments: { wiki: w, type: "memory" } });
    const res = r.json.result;
    expect(res.isError).toBe(true);
    expect(res.content[0].text).toMatch(/invalid:|Invalid|Input validation/i);
    const unknown = await rpc(token, "tools/call", { name: "delete_record", arguments: {} });
    expect(unknown.json.error ?? unknown.json.result?.isError).toBeTruthy();
  });
});

describe("MCP: scopes and allowlists hold on every tool", () => {
  it("lets a read-only device read but not write", async () => {
    const a = await setup();
    await tool(a.token, "save", { wiki: a.w, type: "memory", summary: "visible" });
    const ro = await mint("reader", ["read"]);
    expect((await tool(ro.token, "search", { query: "visible" })).text).toContain("M1");
    expect((await tool(ro.token, "get", { id: "M1" })).isError).toBe(false);
    expect((await tool(ro.token, "ledger_list")).isError).toBe(false);
    expect((await tool(ro.token, "wikis")).isError).toBe(false);
    expect((await tool(ro.token, "session_start")).isError).toBe(false);
    for (const [name, args] of [
      ["save", { wiki: a.w, type: "memory", summary: "no" }],
      ["update", { wiki: a.w, id: "M1", summary: "no" }],
      ["ledger_add", { wiki: a.w, kind: "task", title: "no" }],
      ["ledger_set", { wiki: a.w, id: "M1", action: "claim" }],
    ] as [string, Record<string, unknown>][]) {
      const r = await tool(ro.token, name, args);
      expect(r.isError, name).toBe(true);
      expect(r.text, name).toMatch(/^forbidden:/);
    }
  });

  it("lets a read-only device read but not write", async () => {
    const a = await setup();
    const ro = await mint("reader2", ["read"]);
    for (const [name, args] of [
      ["save", { wiki: a.w, type: "memory", summary: "blind write" }],
      ["ledger_add", { wiki: a.w, kind: "feature", title: "x" }],
    ] as [string, Record<string, unknown>][]) {
      const r = await tool(ro.token, name, args);
      expect(r.isError, name).toBe(true);
      expect(r.text, name).toMatch(/^forbidden:/);
    }
    expect((await tool(ro.token, "wikis", {})).isError).toBe(false);
  });

  it("keeps a restricted device inside its wiki list", async () => {
    const a = await setup();
    const other = a.w + "-other";
    await wiki(a.token, other);
    await tool(a.token, "save", { wiki: other, type: "memory", summary: "secret zebra elsewhere" });
    await tool(a.token, "save", { wiki: a.w, type: "memory", summary: "shared zebra here" });
    const lim = await mint("limited", ["read", "write"], [a.w]);
    expect((await tool(lim.token, "search", { query: "zebra" })).text).toBe(`${a.w}/M1 [memory] shared zebra here`);
    expect((await tool(lim.token, "search", { query: "zebra", wiki: other })).text).toMatch(/^forbidden:/);
    expect((await tool(lim.token, "get", { id: "M1", wiki: other })).text).toMatch(/^forbidden:/);
    expect(JSON.parse((await tool(lim.token, "get", { id: "M1" })).text).map((m: { wiki: string }) => m.wiki)).toEqual([a.w]);
    expect((await tool(lim.token, "save", { wiki: other, type: "memory", summary: "sneak" })).text).toMatch(/^forbidden:/);
    expect((await tool(lim.token, "ledger_add", { wiki: other, kind: "task", title: "sneak" })).text).toMatch(/^forbidden:/);
    expect((await tool(lim.token, "update", { wiki: other, id: "M1", summary: "sneak" })).text).toMatch(/^forbidden:/);
    expect((await tool(lim.token, "ledger_list", { wiki: other })).text).toMatch(/^forbidden:/);
    expect(JSON.parse((await tool(lim.token, "wikis")).text).map((x: { key: string }) => x.key)).toEqual([a.w]);
    expect((await tool(lim.token, "session_start", { wiki: other })).text).toMatch(/^forbidden:/);
  });
});

describe("MCP: OAuth tokens", () => {
  it("serves an OAuth client and honors the scopes it was granted", async () => {
    const a = await setup();
    const { access } = await oauthLogin({ scopes: ["read"], name: "Chat reader" });
    expect((await rpc(access, "tools/list")).status).toBe(200);
    expect((await tool(access, "wikis")).isError).toBe(false);
    const w = await tool(access, "save", { wiki: a.w, type: "memory", summary: "no" });
    expect(w.isError).toBe(true);
    expect(w.text).toMatch(/^forbidden:/);

    const rw = await oauthLogin({ name: "Chat writer" });
    expect((await tool(rw.access, "save", { wiki: a.w, type: "memory", summary: "from a chat app" })).isError).toBe(false);
    const rec1 = (await get(rec(a.w, "/M1"), a.token)).body;
    expect(rec1.device).toBe("Chat writer");

    const dev = (await send("GET", "/admin/devices", { token: "test-owner-key-0123456789abcdef0123456789" })).body.devices.find((d: { name: string }) => d.name === "Chat writer");
    await send("DELETE", `/admin/devices/${dev.id}`, { token: "test-owner-key-0123456789abcdef0123456789" });
    expect((await rpc(rw.access, "tools/list")).status).toBe(401);
  });
});
