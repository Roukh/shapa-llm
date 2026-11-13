// The break-it pass, MCP half: transport checks and the same limits through the tools.

import { describe, expect, it } from "vitest";
import { call, mint, rec, send, setup, wiki } from "./helpers.js";
import { INIT, rpc, tool } from "./mcp-helpers.js";

describe("abuse: MCP search input", () => {
  it("never turns query text into FTS syntax", async () => {
    const { token, w } = await setup();
    await send("POST", rec(w), { token, body: { type: "memory", summary: "alpha beta gamma" } });
    for (const q of ['"', "NEAR(alpha beta)", "summary:alpha", "alpha*", "(alpha", "\ud800", "\u0000", "a".repeat(400), "'; DROP TABLE records; --"]) {
      const m = await tool(token, "search", { query: q });
      expect(m.isError && /fts|syntax|sql/i.test(m.text), q.slice(0, 30)).toBe(false);
    }
  });
});

describe("abuse: MCP credentials", () => {
  it("answers 401, never 500, for hostile bearer values", async () => {
    for (const t of ["a".repeat(100_000), "a:b:c", "::", "x".repeat(600), "shp_" + "z".repeat(5000), "%00", "../../etc", "null", "{}"]) {
      const r = await send("POST", "/mcp", { headers: { authorization: `Bearer ${t}` }, raw: "{}" });
      expect(r.status, t.slice(0, 12)).toBe(401);
      expect(r.headers.get("www-authenticate") ?? "").toContain("Bearer");
    }
  });
});

describe("abuse: MCP transport", () => {
  it("refuses a foreign Origin or Host and ignores GET/DELETE sessions", async () => {
    const { token } = await setup();
    const hdr = { authorization: `Bearer ${token}`, "content-type": "application/json", accept: "application/json, text/event-stream", host: "shapa.test" };
    const body = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: INIT });
    const evilOrigin = await call("/mcp", { method: "POST", headers: { ...hdr, origin: "https://evil.example" }, body });
    expect(evilOrigin.status).toBe(403);
    const evilHost = await call("/mcp", { method: "POST", headers: { ...hdr, host: "evil.example" }, body });
    expect(evilHost.status).toBe(403);
    const sameOrigin = await call("/mcp", { method: "POST", headers: { ...hdr, origin: "https://shapa.test" }, body });
    expect(sameOrigin.status).toBe(200);
    const getRes = await call("/mcp", { method: "GET", headers: { authorization: `Bearer ${token}`, accept: "text/event-stream", host: "shapa.test" } });
    expect([404, 405, 400]).toContain(getRes.status);
  });

  it("answers malformed JSON-RPC with a protocol error, not a crash", async () => {
    const { token } = await setup();
    const hdr = { authorization: `Bearer ${token}`, "content-type": "application/json", accept: "application/json, text/event-stream", host: "shapa.test" };
    for (const raw of ["{", "[]", "null", '{"jsonrpc":"2.0"}', '{"jsonrpc":"2.0","id":1,"method":"tools/call"}', '{"jsonrpc":"2.0","id":1,"method":"nope"}']) {
      const res = await call("/mcp", { method: "POST", headers: hdr, body: raw });
      expect(res.status, raw).toBeLessThan(500);
      await res.text();
    }
    const big = await tool(token, "save", { wiki: "x", type: "memory", summary: "y".repeat(6000) });
    expect(big.isError).toBe(true);
    const r = await rpc(token, "tools/call", { name: "save", arguments: { wiki: "w", type: "memory", summary: "x", __proto__: { admin: true } } });
    expect(r.status).toBeLessThan(500);
  });

  it("applies the same limits through MCP as through REST", async () => {
    const a = await setup();
    const lim = await mint("mcp-limited", ["read", "write"], [a.w]);
    const other = a.w + "-b";
    await wiki(a.token, other);
    expect((await tool(lim.token, "save", { wiki: other, type: "memory", summary: "x" })).text).toMatch(/^forbidden/);
    const hostile = await tool(lim.token, "save", { wiki: a.w, type: "note", path: "arch/../../etc/x.md", body: "x" });
    expect(hostile.isError).toBe(true);
    const ok = await tool(lim.token, "save", { wiki: a.w, type: "memory", summary: "fine" });
    expect(ok.isError).toBe(false);
    // nothing in MCP can delete
    const list = await rpc(lim.token, "tools/list");
    expect(JSON.stringify(list.json)).not.toMatch(/"name":"(delete|remove)/);
    // a tombstoned record is not reachable through MCP either
    await send("DELETE", rec(a.w, "/M1"), { token: a.token });
    expect((await tool(lim.token, "get", { id: "M1" })).text).toBe("No record M1.");
    expect((await tool(lim.token, "update", { wiki: a.w, id: "M1", summary: "revive" })).text).toMatch(/^not_found/);
  });
});
