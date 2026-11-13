import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker from "../src/index.js";
import { BASE, OWNER, env, get, mintAll, send } from "./helpers.js";

let ipCounter = 0;
const freshIp = () => `203.0.113.${(++ipCounter % 250) + 1}`;
const adminFrom = (ip: string, token: string | undefined, method = "GET", path = "/admin/devices", body?: unknown) =>
  send(method, path, { ...(token ? { token } : {}), ...(body ? { body } : {}), headers: { "cf-connecting-ip": ip } });

describe("device tokens", () => {
  it("mints tokens only for the owner key and stores them as hashes", async () => {
    const ip = freshIp();
    expect((await adminFrom(ip, undefined, "POST", "/admin/devices", { name: "a", scopes: ["read"] })).status).toBe(401);
    expect((await adminFrom(ip, "wrong-key", "POST", "/admin/devices", { name: "a", scopes: ["read"] })).status).toBe(401);

    const r = await send("POST", "/admin/devices", { token: OWNER, body: { name: "laptop", scopes: ["write", "read"], wikis: ["a-wiki"] } });
    expect(r.status).toBe(201);
    expect(r.body.token).toMatch(/^shp_[0-9a-f]{16}_[0-9a-f]{64}$/);
    expect(r.body).toMatchObject({ name: "laptop", scopes: ["read", "write"], wikis: ["a-wiki"] });
    const secret = r.body.token.split("_")[2];

    const stub = env.STORE.getByName("main");
    const rows = await runInDurableObject(stub, (_i, state) => state.storage.sql.exec("SELECT token_hash, source FROM devices WHERE id = ?", r.body.id).toArray());
    expect(rows).toHaveLength(1);
    expect(rows[0]!["source"]).toBe("token");
    const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret)))].map((b) => b.toString(16).padStart(2, "0")).join("");
    expect(rows[0]!["token_hash"]).toBe(hash);
    expect(JSON.stringify(rows)).not.toContain(secret);

    const list = await send("GET", "/admin/devices", { token: OWNER });
    expect(JSON.stringify(list.body)).not.toContain(secret);
    expect(JSON.stringify(list.body)).not.toContain("token_hash");
    expect(list.body.devices.find((d: { id: string }) => d.id === r.body.id)).toMatchObject({ name: "laptop", source: "token", revoked: null });
  });

  it("validates what it mints", async () => {
    for (const body of [
      { name: "", scopes: ["read"] }, { name: "x".repeat(65), scopes: ["read"] }, { name: "x", scopes: [] }, { name: "x", scopes: ["admin"] },
      { name: "x", scopes: ["read"], wikis: ["Bad Key"] }, { name: "x" }, { name: " padded ", scopes: ["read"] }, { name: "line\nbreak", scopes: ["read"] },
    ]) {
      expect((await send("POST", "/admin/devices", { token: OWNER, body })).status, JSON.stringify(body)).toBe(400);
    }
  });

  it("refuses malformed, wrong and unknown tokens", async () => {
    const d = await mintAll();
    const [, id, secret] = d.token.split("_") as [string, string, string];
    for (const bad of [
      "", "shp_", "shp_x_y", `shp_${id}_${"0".repeat(64)}`, `shp_${"f".repeat(16)}_${secret}`, `${d.token}0`, d.token.toUpperCase(),
      `shp_${id}_${secret.slice(0, 63)}`, d.token.replace("shp_", "shx_"),
    ]) {
      const r = await send("GET", "/v1/whoami", { headers: { authorization: `Bearer ${bad}` } });
      expect(r.status, bad).toBe(401);
    }
    expect((await send("GET", "/v1/whoami", { headers: { authorization: `Basic ${d.token}` } })).status).toBe(401);
    expect((await send("GET", "/v1/whoami", { headers: { authorization: d.token } })).status).toBe(401);
    expect((await get("/v1/whoami", d.token)).status).toBe(200);
    // the secret in a query string is not a credential
    expect((await get(`/v1/whoami?token=${d.token}`)).status).toBe(401);
  });

  it("refuses a revoked device on its next request and revoking is idempotent", async () => {
    const d = await mintAll("temp");
    expect((await get("/v1/whoami", d.token)).status).toBe(200);
    const del = await send("DELETE", `/admin/devices/${d.id}`, { token: OWNER });
    expect(del.status).toBe(200);
    expect(del.body.device.revoked).toMatch(/^\d{4}-/);
    expect((await get("/v1/whoami", d.token)).status).toBe(401);
    expect((await get("/v1/wikis", d.token)).status).toBe(401);
    expect((await send("DELETE", `/admin/devices/${d.id}`, { token: OWNER })).status).toBe(200);
    expect((await send("DELETE", `/admin/devices/${"0".repeat(16)}`, { token: OWNER })).status).toBe(404);
    expect((await send("DELETE", `/admin/devices/not-an-id`, { token: OWNER })).status).toBe(400);
    // other devices are untouched
    const other = await mintAll("other");
    expect((await get("/v1/whoami", other.token)).status).toBe(200);
  });

  it("does not let a device token reach the admin API", async () => {
    const d = await mintAll();
    expect((await send("GET", "/admin/devices", { token: d.token })).status).toBe(401);
    expect((await send("POST", "/admin/devices", { token: d.token, body: { name: "evil", scopes: ["read"] } })).status).toBe(401);
  });
});

describe("owner key", () => {
  it("refuses admin and consent with 503 when the key is shorter than 32 characters", async () => {
    const weak = { STORE: env.STORE, OAUTH_KV: env.OAUTH_KV, SHAPA_OWNER_KEY: "short-key" } as unknown as Env;
    const ctx = { waitUntil() {}, passThroughOnException() {}, props: {} } as unknown as ExecutionContext;
    const admin = await worker.fetch(new Request(`${BASE}/admin/devices`, { headers: { authorization: "Bearer short-key" } }), weak, ctx);
    expect(admin.status).toBe(503);
    const none = { STORE: env.STORE, OAUTH_KV: env.OAUTH_KV } as unknown as Env;
    expect((await worker.fetch(new Request(`${BASE}/admin/devices`), none, ctx)).status).toBe(503);
  });

  it("allows 5 wrong keys per IP per 15 minutes, then 429 even for the right key", async () => {
    const ip = freshIp();
    for (let i = 0; i < 5; i++) expect((await adminFrom(ip, `wrong-${i}`)).status).toBe(401);
    const blocked = await adminFrom(ip, OWNER);
    expect(blocked.status).toBe(429);
    expect(Number(blocked.headers.get("retry-after"))).toBeGreaterThan(0);
    expect((await adminFrom(ip, "still-wrong")).status).toBe(429);
    // another address is not affected
    expect((await adminFrom(freshIp(), OWNER)).status).toBe(200);
    // a missing key is not a failed attempt
    const ip2 = freshIp();
    for (let i = 0; i < 8; i++) expect((await adminFrom(ip2, undefined)).status).toBe(401);
    expect((await adminFrom(ip2, OWNER)).status).toBe(200);
  });

  it("compares keys of any length without crashing", async () => {
    const ip = freshIp();
    for (const k of ["a", "x".repeat(10_000), OWNER.slice(0, -1), `${OWNER}x`, OWNER.toUpperCase()]) {
      expect((await adminFrom(ip, k)).status, k.slice(0, 20)).toBe(401);
      if (ip && k === OWNER.toUpperCase()) break;
    }
  });
});

