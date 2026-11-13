import { describe, expect, it } from "vitest";
import worker from "../src/index.js";
import { BASE, OWNER, call, env, get, rec, send, setup } from "./helpers.js";
import {
  REDIRECT, authorizeUrl, exchange, oauthLogin, openConsent, pkce, registerClient, submitConsent,
} from "./oauth-helpers.js";

let ipCounter = 100;
const freshIp = () => `198.51.100.${(++ipCounter % 250) + 1}`;
const adminFrom = (ip: string, token: string | undefined) =>
  send("GET", "/admin/devices", { ...(token ? { token } : {}), headers: { "cf-connecting-ip": ip } });

describe("consent page without an owner key", () => {
  it("answers 503 when the key is shorter than 32 characters", async () => {
    const weak = { STORE: env.STORE, OAUTH_KV: env.OAUTH_KV, SHAPA_OWNER_KEY: "short-key" } as unknown as Env;
    const ctx = { waitUntil() {}, passThroughOnException() {}, props: {} } as unknown as ExecutionContext;
    const auth = await worker.fetch(new Request(`${BASE}/authorize?response_type=code`), weak, ctx);
    expect(auth.status).toBe(503);
  });
});

describe("OAuth 2.1", () => {
  it("publishes discovery metadata for the issuer", async () => {
    const as = await (await call("/.well-known/oauth-authorization-server")).json<any>();
    expect(as).toMatchObject({
      issuer: BASE, authorization_endpoint: `${BASE}/authorize`, token_endpoint: `${BASE}/token`, registration_endpoint: `${BASE}/register`,
    });
    expect(as.code_challenge_methods_supported).toEqual(["S256"]);
    expect(as.scopes_supported).toEqual(["read", "write", "delete"]);
  });

  it("runs register, authorize with the owner key, token exchange, then serves /v1", async () => {
    const { access, refresh, deviceName } = await oauthLogin({ name: "Phone chat" });
    const me = await get("/v1/whoami", access);
    expect(me.status).toBe(200);
    expect(me.body).toMatchObject({ name: deviceName, scopes: ["read", "write"], wikis: null });
    expect(refresh).not.toBe("");
    // the token works for real operations, attributed to this device
    const w = (await setup()).w;
    await send("PUT", `/v1/wikis/${w}`, { token: access, body: { name: w, kind: "repo" } });
    const made = await send("POST", rec(w), { token: access, body: { type: "memory", summary: "from the phone" } });
    expect(made.body.device).toBe(deviceName);
    // the grant shows up as a device, source oauth
    const list = await send("GET", "/admin/devices", { token: OWNER });
    expect(list.body.devices.find((d: { name: string }) => d.name === deviceName)).toMatchObject({ source: "oauth" });
  });

  it("applies the scopes and wiki list chosen on the consent page", async () => {
    const a = await setup();
    const { access } = await oauthLogin({ scopes: ["read"], wikis: `${a.w}`, name: "Reader" });
    expect((await get("/v1/whoami", access)).body).toMatchObject({ scopes: ["read"], wikis: [a.w] });
    expect((await send("POST", rec(a.w), { token: access, body: { type: "memory", summary: "no" } })).status).toBe(403);
    expect((await get(rec(a.w), access)).status).toBe(200);
    expect((await get("/v1/search?q=anything", access)).status).toBe(200);
  });

  it("stops both the access and refresh tokens when the device is revoked", async () => {
    const { access, refresh, clientId } = await oauthLogin({ name: "To revoke" });
    const dev = (await send("GET", "/admin/devices", { token: OWNER })).body.devices.find((d: { name: string }) => d.name === "To revoke");
    expect((await get("/v1/whoami", access)).status).toBe(200);
    expect((await send("DELETE", `/admin/devices/${dev.id}`, { token: OWNER })).status).toBe(200);
    expect((await get("/v1/whoami", access)).status).toBe(401);
    const again = await call("/token", {
      method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: refresh, client_id: clientId }).toString(),
    });
    expect(again.status).toBe(400);
    expect(((await again.json()) as { error: string }).error).toBe("invalid_grant");
  });

  it("refuses a wrong owner key on the consent page, re-renders it, and rate limits per IP", async () => {
    const ip = freshIp();
    const clientId = await registerClient();
    const { challenge } = await pkce();
    const url = authorizeUrl(clientId, challenge);
    for (let i = 0; i < 5; i++) {
      const page = await openConsent(url);
      const res = await submitConsent(page, { owner_key: `nope-${i}` }, { ip });
      expect(res.status).toBe(401);
      expect(res.headers.get("location")).toBeNull();
      const html = await res.text();
      expect(html).toContain("That is not the owner key.");
      expect(html).not.toContain(`nope-${i}`);
    }
    const page = await openConsent(url);
    const blocked = await submitConsent(page, { owner_key: OWNER }, { ip });
    expect(blocked.status).toBe(429);
    expect(blocked.headers.get("location")).toBeNull();
    // the same limit covers the admin API
    expect((await adminFrom(ip, OWNER)).status).toBe(429);
  });

  it("defends the consent page against CSRF, framing and injection", async () => {
    const clientId = await registerClient(`<script>alert(1)</script>"><img src=x onerror=alert(2)>`);
    const { challenge } = await pkce();
    const page = await openConsent(authorizeUrl(clientId, challenge));
    expect(page.status).toBe(200);
    const csp = page.headers.get("content-security-policy") ?? "";
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("default-src 'none'");
    expect(csp).not.toMatch(/script-src|unsafe-inline|unsafe-eval/);
    expect(page.headers.get("x-frame-options")).toBe("DENY");
    expect(page.headers.get("cache-control")).toBe("no-store");
    // client-supplied text is escaped; the page has no script and no external resource
    expect(page.html).not.toContain("<script");
    expect(page.html).not.toContain("<img");
    expect(page.html).toContain("&#60;script&#62;");
    expect(page.html).not.toMatch(/(src|href)=["']?https?:/i);
    expect(page.cookies).toContain("__Host-shapa-csrf=");
    expect(page.html).toMatch(/<form method="post">/);

    // no CSRF cookie, a wrong token, or a missing token: forbidden, and nothing is created
    const before = (await send("GET", "/admin/devices", { token: OWNER })).body.devices.length;
    const noCookie = await submitConsent(page, {}, { cookies: page.cookies.split("; ").filter((c) => !c.startsWith("__Host-shapa-csrf")).join("; ") });
    expect(noCookie.status).toBe(403);
    expect((await submitConsent(page, { csrf: "0".repeat(32) })).status).toBe(403);
    expect((await submitConsent(page, { csrf: "" })).status).toBe(403);
    expect((await send("GET", "/admin/devices", { token: OWNER })).body.devices.length).toBe(before);
  });

  it("never redirects to an unregistered redirect URI", async () => {
    const clientId = await registerClient();
    const { challenge } = await pkce();
    for (const evil of ["https://evil.example/cb", "http://client.example/callback", "javascript:alert(1)", "//evil.example", `${REDIRECT}/../x`]) {
      const res = await call(authorizeUrl(clientId, challenge, "read", evil), { redirect: "manual" });
      expect(res.status, evil).toBe(400);
      expect(res.headers.get("location"), evil).toBeNull();
    }
    const unknown = await call(authorizeUrl("no-such-client", challenge), { redirect: "manual" });
    expect(unknown.headers.get("location")).toBeNull();
  });

  it("sends access_denied to the registered redirect when the owner denies, and creates no device", async () => {
    const clientId = await registerClient();
    const { challenge } = await pkce();
    const page = await openConsent(authorizeUrl(clientId, challenge));
    const before = (await send("GET", "/admin/devices", { token: OWNER })).body.devices.length;
    const res = await submitConsent(page, { decision: "deny", owner_key: "" });
    expect(res.status).toBe(302);
    const loc = new URL(res.headers.get("location")!);
    expect(loc.origin + loc.pathname).toBe(REDIRECT);
    expect(loc.searchParams.get("error")).toBe("access_denied");
    expect(loc.searchParams.get("state")).toBe("st4te");
    expect((await send("GET", "/admin/devices", { token: OWNER })).body.devices.length).toBe(before);
  });

  it("validates the device name, scopes and wiki list, and a consent handle works once", async () => {
    const clientId = await registerClient();
    const { challenge, verifier } = await pkce();
    const url = authorizeUrl(clientId, challenge);
    for (const fields of <Record<string, string | string[]>[]>[{ device_name: "" }, { device_name: "x".repeat(65) }, { device_name: "a\u0000b" }, { scope: [] as string[] }, { scope: ["root"] }, { wikis: "Bad Key!" }]) {
      const page = await openConsent(url);
      const res = await submitConsent(page, fields);
      expect(res.status, JSON.stringify(fields)).toBe(400);
      expect(res.headers.get("location")).toBeNull();
    }
    const page = await openConsent(url);
    const ok = await submitConsent(page, { device_name: "Once" });
    expect(ok.status).toBe(302);
    const replay = await submitConsent(page, { device_name: "Twice" });
    expect(replay.headers.get("location")).toBeNull();
    expect([400, 403]).toContain(replay.status);
    // an authorization code cannot be exchanged with another client's verifier
    const code = new URL(ok.headers.get("location")!).searchParams.get("code")!;
    const bad = await exchange(code, clientId, `${verifier}x`);
    expect(bad.status).toBe(400);
  });
});
