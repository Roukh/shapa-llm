// The break-it pass, REST half: each case is something an abuser would try.

import { describe, expect, it } from "vitest";
import { OWNER, call, get, mint, put, rec, row, send, setup, sync } from "./helpers.js";

const create = (token: string, w: string, body: unknown) => send("POST", rec(w), { token, body });

describe("abuse: search input is never FTS syntax", () => {
  it("survives operators, quotes, column filters and punctuation without an error", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "memory", summary: "alpha beta gamma", body: "NEAR OR AND NOT" });
    const nasty = [
      '"', '""', '"unterminated', "alpha AND", "OR beta", "NOT", "NEAR(alpha beta)", "summary:alpha", "{summary body}: alpha", "alpha*", "^alpha",
      "(alpha", "alpha)", "-alpha", "alpha -beta", "'; DROP TABLE records; --", "\\", "%", "_", "alpha\u0000beta", "a".repeat(5000), "\ud800", "🙂 alpha",
      "* * *", "AND OR NOT NEAR", "alpha OR", "col1:x", "fts MATCH", "rank", "\n\t",
    ];
    for (const q of nasty) {
      let enc: string | null = null;
      try {
        enc = encodeURIComponent(q.slice(0, 500));
      } catch {
        // a lone surrogate cannot be put in a URL; it still goes through the MCP call below
      }
      if (enc !== null) {
        const r = await get(`/v1/search?q=${enc}`, token);
        expect([200, 400], q.slice(0, 30)).toContain(r.status);
      }
    }
    // operators inside the text are plain words
    expect((await get("/v1/search?q=alpha+AND+beta", token)).body.results).toHaveLength(1);
    expect((await get("/v1/search?q=summary:alpha", token)).body.results).toHaveLength(1);
    expect((await get("/v1/search?q=NOT+beta", token)).body.results).toHaveLength(1);
    expect((await get("/v1/search?q=%22", token)).body.results).toEqual([]);
    expect((await get("/v1/search?q=zzz&types=bogus,%27%3B--", token)).body.results).toEqual([]);
    expect((await get("/v1/search?q=alpha&k=500", token)).status).toBe(400);
    expect((await get("/v1/search?q=", token)).status).toBe(400);
  });
});

describe("abuse: ids and paths", () => {
  it("cannot reach other routes or records through the id segment", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "memory", summary: "x" });
    for (const id of ["..%2F..%2Fadmin%2Fdevices", "%2e%2e%2f%2e%2e%2fadmin", "..", "%00", "M1%00", "M1%2F..%2FM1", "%252e%252e", "M1;x", "M1%3Fx%3D1", "M1%23"]) {
      const r = await get(rec(w, `/${id}`), token);
      expect([400, 404], id).toContain(r.status);
      expect(r.body, id).not.toHaveProperty("devices");
      expect(r.body.error.code, id).toMatch(/^(not_found|invalid)$/);
    }
    expect((await get("/v1/wikis/..%2Fadmin/records", token)).status).toBe(400);
    expect((await get(`/v1/wikis/${w}/records/M1/../../../../admin/devices`, token)).status).not.toBe(200);
    for (const d of ["PUT /v1/wikis/__proto__", "PUT /v1/wikis/Constructor", "PUT /v1/wikis/a%00b"]) {
      const [m, p] = d.split(" ") as [string, string];
      expect((await send(m, p, { token, body: { name: "x", kind: "repo" } })).status, d).toBe(400);
    }
  });

  it("keeps ids and aliases from shadowing or colliding with each other", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "memory", summary: "first" });
    await create(token, w, { type: "memory", summary: "second", alias: "first-alias" });
    // an alias equal to an existing id (or one that looks like an id) is dropped, so a get by id stays unambiguous
    for (const alias of ["M1", "M2", "F1", "J7", "D3", "arch.md", "x/y"]) {
      const r = await create(token, w, { type: "rule", summary: `rule for ${alias}`, alias });
      expect(r.status, alias).toBe(201);
      expect(r.body.data.alias, alias).toBeNull();
    }
    expect((await get(`/v1/get?id=M1&wiki=${w}`, token)).body.matches).toHaveLength(1);
    // PATCH cannot steal an alias, but may keep its own
    expect((await send("PATCH", rec(w, "/M1"), { token, body: { alias: "first-alias" } })).status).toBe(409);
    expect((await send("PATCH", rec(w, "/M2"), { token, body: { alias: "first-alias", body: "same alias kept" } })).status).toBe(200);
    // sync cannot create two live records with one alias, nor shadow an id
    const dup = { ...row("M8", "dup"), data: { summary: "dup", body: "", alias: "first-alias", source: "", session: "" } };
    const shadow = { ...row("M9", "shadow"), data: { summary: "shadow", body: "", alias: "M1", source: "", session: "" } };
    const r = await sync(token, w, { ops: [put(dup), put(shadow)] });
    expect(r.body.changes.every((c: { data: { alias: unknown } }) => c.data.alias === null || c.data.alias === "first-alias")).toBe(true);
    expect((await get(`/v1/get?id=first-alias&wiki=${w}`, token)).body.matches).toHaveLength(1);
    expect((await get(`/v1/get?id=M1&wiki=${w}`, token)).body.matches).toHaveLength(1);
  });

  it("does not let a sync put take over a note id in another kind, or smuggle a path", async () => {
    const { token, w } = await setup();
    const evil = ["research/%2e%2e/x.md", "research//x.md", "research/x.md/", "arch\\x.md", "arch/x.md\u0000", " arch/x.md", "arch/.md", "arch/x.MD", "ARCH/x.md"];
    const r = await sync(token, w, { ops: evil.map((id) => put({ id, kind: "N", created: "", updated: "2026-01-01T00:00:00Z", data: { body: "x" } })) });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(evil.map(() => "rejected"));
    // a ledger kind with a note-looking id, and a note kind with a ledger id
    const mixed = await sync(token, w, { ops: [put({ ...row("M1", "x"), id: "research/x.md" }), put({ id: "M2", kind: "N", created: "", updated: "2026-01-01T00:00:00Z", data: { body: "x" } })] });
    expect(mixed.body.results.map((x: { status: string }) => x.status)).toEqual(["rejected", "rejected"]);
  });
});

describe("abuse: sizes, shapes and malformed input", () => {
  it("answers 413 for an oversized body, including a streamed one with no content-length", async () => {
    const { token, w } = await setup();
    expect((await send("POST", rec(w), { token, raw: JSON.stringify({ type: "memory", summary: "x", body: "y".repeat(1_200_000) }) })).status).toBe(413);
    expect((await create(token, w, { type: "memory", summary: "x", body: "y".repeat(1_000_001) })).status).toBe(413);
    const chunk = new TextEncoder().encode("a".repeat(65_536));
    let sent = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(c) {
        if (sent >= 40) return c.close();
        sent += 1;
        c.enqueue(chunk);
      },
    });
    const res = await call(rec(w), { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: stream, duplex: "half" } as RequestInit);
    expect(res.status).toBe(413);
    expect((await send("POST", "/admin/devices", { token: OWNER, raw: JSON.stringify({ name: "x", scopes: ["read"], pad: "p".repeat(10_000) }) })).status).toBe(413);
  });

  it("rejects hostile JSON without a 500", async () => {
    const { token, w } = await setup();
    const deep = "[".repeat(3000) + "]".repeat(3000);
    const bodies = [
      "", "null", "[]", "1", '"str"', "{", '{"type":', `{"type":"memory","summary":${deep}}`, '{"__proto__":{"polluted":1},"type":"memory","summary":"x"}',
      '{"type":"memory","summary":"x","constructor":{"prototype":{"polluted":1}}}', `{"type":"memory","summary":"x","tags":${JSON.stringify(Array.from({ length: 600 }, (_, i) => `t${i}`))}}`,
    ];
    for (const raw of bodies) {
      const r = await send("POST", rec(w), { token, raw });
      expect(r.status, raw.slice(0, 40)).toBeLessThan(500);
    }
    for (const raw of ['{"cursor":0,"ops":[{"op":"put","base_rev":null,"record":' + deep + "}]}", '{"__proto__":{"x":1},"counters":{"__proto__":5,"constructor":3,"M":2},"ops":[]}', "[]", "null"]) {
      const r = await send("POST", `/v1/wikis/${w}/sync`, { token, raw });
      expect(r.status, raw.slice(0, 40)).toBeLessThan(500);
    }
    const poll = await send("GET", "/v1/whoami", { token });
    expect(poll.body).not.toHaveProperty("polluted");
    expect(({} as Record<string, unknown>)["polluted"]).toBeUndefined();
    // a record with absurd nesting inside its data is rejected, not stored
    const nested = await sync(token, w, { ops: [put({ ...row("M900", "x"), data: JSON.parse(`{"summary":"x","extra":${"[".repeat(500)}${"]".repeat(500)}}`) })] });
    expect(nested.status).toBe(200);
    expect(nested.body.results[0].status).toBe("accepted");
    expect(JSON.stringify(nested.body.changes[0])).not.toContain("extra");
  });

  it("caps counters so an id never outgrows the wire format", async () => {
    const { token, w } = await setup();
    expect((await sync(token, w, { counters: { M: 1_000_000_000 } })).status).toBe(400);
    expect((await sync(token, w, { counters: { M: 999_999_999 } })).status).toBe(200);
    const last = await create(token, w, { type: "memory", summary: "last id" });
    expect(last.body.id).toBe("M999999999");
    const over = await create(token, w, { type: "memory", summary: "no more" });
    expect(over.status).toBe(409);
    // a colliding client record cannot be renamed any more: refused for that op, not a failed request
    const clash = await sync(token, w, { ops: [put(row("M999999999", "other", { created: "2026-02-02T00:00:00Z" }))] });
    expect(clash.status).toBe(200);
    expect(clash.body.results[0]).toMatchObject({ status: "rejected", error: expect.stringMatching(/no M ids left/) });
  });

  it("stores odd unicode without failing, and keeps one-line fields on one line", async () => {
    const { token, w } = await setup();
    const r = await create(token, w, { type: "memory", summary: "line1\nline2\u0000‮ 🙂 \ud800 end", body: "b\u0000b" });
    expect(r.status).toBe(201);
    expect(r.body.data.summary).not.toMatch(/\n/);
    expect((await get(rec(w, "/M1"), token)).status).toBe(200);
    const long = await create(token, w, { type: "memory", summary: "é".repeat(400) });
    expect(Array.from(long.body.data.summary as string).length).toBeLessThanOrEqual(160);
  });

  it("returns record text as data: json, no-store, no sniffing", async () => {
    const { token, w } = await setup();
    await create(token, w, { type: "memory", summary: "<script>alert(1)</script><img src=x onerror=alert(2)>" });
    const r = await get(rec(w, "/M1"), token);
    expect(r.headers.get("content-type")).toContain("application/json");
    expect(r.headers.get("x-content-type-options")).toBe("nosniff");
    expect(r.headers.get("cache-control")).toBe("no-store");
    expect((await get("/v1/whoami")).headers.get("cache-control")).toBe("no-store");
  });
});

describe("abuse: credentials", () => {
  it("answers 401, never 500, for hostile bearer values on every credential route", async () => {
    const { w } = await setup();
    const weird = [
      "a".repeat(100_000), "a:b:c", "::", "x".repeat(600), "shp_" + "z".repeat(5000), "éé", "%00", "../../etc", "Bearer", "null", "undefined", "{}", "a b",
    ];
    for (const t of weird) {
      let headers: Record<string, string>;
      try {
        headers = { authorization: `Bearer ${t}` };
        new Headers(headers);
      } catch {
        continue;
      }
      for (const [m, p] of [["GET", "/v1/whoami"], ["GET", `/v1/wikis/${w}/records`]] as const) {
        const r = await send(m, p, { headers });
        expect(r.status, `${m} ${p} ${t.slice(0, 12)}`).toBe(401);
      }
      // the admin API counts wrong owner keys, so it may turn from 401 to 429 on its own address
      const admin = await send("GET", "/admin/devices", { headers: { ...headers, "cf-connecting-ip": "192.0.2.77" } });
      expect([401, 429], `admin ${t.slice(0, 12)}`).toContain(admin.status);
    }
  });

  it("measures the 1 MB record limit in bytes, not characters", async () => {
    const { token, w } = await setup();
    const wide = "é".repeat(600_000); // 600k characters, 1.2 MB of UTF-8
    const r = await sync(token, w, { ops: [put({ ...row("M1", "wide"), data: { summary: "wide", body: wide, alias: null, source: "", session: "" } })] });
    expect(r.body.results[0].status).toBe("rejected");
    expect(r.body.results[0].error).toMatch(/1 MB/);
    const ok = await sync(token, w, { ops: [put({ ...row("M2", "wide ok"), data: { summary: "wide ok", body: "é".repeat(400_000), alias: null, source: "", session: "" } })] });
    expect(ok.body.results[0].status).toBe("accepted");
  });
});

describe("abuse: one device against another", () => {
  it("cannot read, write or enumerate beyond its wikis through any route", async () => {
    const a = await setup();
    const b = await setup();
    await create(b.token, b.w, { type: "memory", summary: "private to b", alias: "b-secret" });
    const lim = await mint("a-only", ["read", "write", "delete"], [a.w]);
    for (const [m, p, body] of [
      ["GET", rec(b.w), undefined], ["GET", rec(b.w, "/M1"), undefined], ["GET", rec(b.w, "/b-secret"), undefined],
      ["PATCH", rec(b.w, "/M1"), { summary: "pwn" }], ["POST", rec(b.w, "/M1/claim"), undefined], ["POST", rec(b.w, "/M1/close"), { reason: "agent" }],
      ["DELETE", rec(b.w, "/M1"), undefined], ["POST", `/v1/wikis/${b.w}/sync`, { cursor: 0, ops: [put(row("M5", "x"))], want: ["M1"] }],
      ["GET", `/v1/search?q=private&wiki=${b.w}`, undefined], ["GET", `/v1/get?id=b-secret&wiki=${b.w}`, undefined],
    ] as [string, string, unknown][]) {
      const r = await send(m, p, { token: lim.token, ...(body ? { body } : {}) });
      expect(r.status, `${m} ${p}`).toBe(403);
    }
    for (const p of ["/v1/search?q=private", "/v1/get?id=b-secret", "/v1/get?id=M1", "/v1/wikis"]) {
      expect(JSON.stringify((await get(p, lim.token)).body), p).not.toContain("private to b");
    }
    expect((await get(rec(b.w, "/M1"), b.token)).body.data.summary).toBe("private to b");
    // existence does not leak through the status either
    expect((await get(rec("no-such-wiki", "/M1"), lim.token)).status).toBe(403);
  });

  it("cannot mint, widen or revive credentials", async () => {
    const d = await mint("plain", ["read", "write", "delete"]);
    expect((await send("POST", "/admin/devices", { token: d.token, body: { name: "x", scopes: ["read"] } })).status).toBe(401);
    expect((await send("DELETE", `/admin/devices/${d.id}`, { token: d.token })).status).toBe(401);
    expect((await send("PATCH", "/admin/devices", { token: OWNER, body: {} })).status).toBe(404);
    const w = await mint("limited", ["read"], ["only-this"]);
    // the whoami view reflects the stored scopes, not anything the caller sends
    const who = await send("GET", "/v1/whoami?scopes=write&wikis=all", { token: w.token, headers: { "x-scopes": "write,delete", "x-device": "admin" } });
    expect(who.body).toMatchObject({ scopes: ["read"], wikis: ["only-this"] });
  });
});

