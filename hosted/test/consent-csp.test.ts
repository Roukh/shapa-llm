import { describe, expect, it } from "vitest";
import { formAction } from "../src/consent.js";
import { BASE, call } from "./helpers.js";
import { REDIRECT, authorizeUrl, openConsent, pkce, registerClient, submitConsent } from "./oauth-helpers.js";

const formActionOf = (res: { headers: Headers }) => /form-action ([^;]*);/.exec(res.headers.get("content-security-policy") ?? "")?.[1] ?? "";

async function registerMany(redirectUris: string[]): Promise<{ status: number; clientId: string }> {
  const res = await call("/register", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ client_name: "Multi", redirect_uris: redirectUris, token_endpoint_auth_method: "none", grant_types: ["authorization_code"], response_types: ["code"] }),
  });
  const body = (await res.json()) as { client_id?: string };
  return { status: res.status, clientId: body.client_id ?? "" };
}

describe("consent page CSP form-action", () => {
  it("allows the origin of the registered redirect URI, and only that", async () => {
    const clientId = await registerClient("ChatGPT", "https://chatgpt.com/connector_platform_oauth_redirect");
    const { challenge } = await pkce();
    const page = await openConsent(authorizeUrl(clientId, challenge, "read write", "https://chatgpt.com/connector_platform_oauth_redirect"));
    expect(page.status).toBe(200);
    expect(formActionOf(page)).toBe("'self' https://chatgpt.com");
    const csp = page.headers.get("content-security-policy") ?? "";
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toMatch(/script-src|unsafe-/);

    // the re-rendered error page (wrong owner key) keeps the same policy
    const again = await submitConsent(page, { owner_key: "wrong-owner-key" });
    expect(again.status).toBe(401);
    expect(formActionOf(again)).toBe("'self' https://chatgpt.com");
  });

  it("keeps a non-default port and allows http on localhost for development", async () => {
    for (const [uri, want] of [
      ["https://client.example:8443/cb", "'self' https://client.example:8443"],
      ["http://localhost:3000/cb", "'self' http://localhost:3000"],
      ["http://127.0.0.1:8080/cb", "'self' http://127.0.0.1:8080"],
    ] as const) {
      const clientId = await registerClient("Dev", uri);
      const { challenge } = await pkce();
      const page = await openConsent(authorizeUrl(clientId, challenge, "read", uri));
      expect(page.status, uri).toBe(200);
      expect(formActionOf(page), uri).toBe(want);
    }
  });

  it("uses the redirect URI of the request, not another one the client registered", async () => {
    const { status, clientId } = await registerMany([REDIRECT, "https://second.example/cb"]);
    expect(status).toBe(201);
    const { challenge } = await pkce();
    const page = await openConsent(authorizeUrl(clientId, challenge, "read", "https://second.example/cb"));
    expect(formActionOf(page)).toBe("'self' https://second.example");
  });

  it("never reflects a raw query value or an unregistered redirect into the CSP", async () => {
    const clientId = await registerClient();
    const { challenge } = await pkce();
    for (const evil of ["https://evil.example/cb", "https://client.example.evil.example/callback", "https://client.example/callback; script-src *", "http://client.example/callback"]) {
      const res = await call(authorizeUrl(clientId, challenge, "read", evil), { redirect: "manual" });
      expect(res.status, evil).toBe(400);
      expect(res.headers.get("content-security-policy") ?? "", evil).not.toContain("evil");
      expect(res.headers.get("content-security-policy") ?? "", evil).toContain("form-action 'self';");
    }
  });

  it("a registered redirect host with CSP syntax in it never reaches the policy", async () => {
    const { challenge } = await pkce();
    for (const uri of ["https://a;script-src.example/cb", "https://a'b.example/cb", "https://a,b.example/cb"]) {
      const reg = await registerMany([REDIRECT, uri]);
      if (reg.status !== 201) continue;
      const res = await call(authorizeUrl(reg.clientId, challenge, "read", uri), { redirect: "manual" });
      const csp = res.headers.get("content-security-policy") ?? "";
      expect(csp, uri).not.toMatch(/script-src|a'b|a,b/);
      expect(csp, uri).toContain("form-action 'self';");
    }
  });

  it("falls back to 'self' for a registered private-use scheme or a mixed list with a javascript: entry", async () => {
    // a client may list extra, unusable callbacks next to one that works; requesting one of those is refused
    const mixed = await registerMany([REDIRECT, "cursor://anysphere.cursor-mcp/oauth/callback"]);
    expect(mixed.status).toBe(201);
    const { challenge } = await pkce();
    const res = await call(authorizeUrl(mixed.clientId, challenge, "read", "cursor://anysphere.cursor-mcp/oauth/callback"), { redirect: "manual" });
    expect(res.status).toBe(400);
    expect(res.headers.get("location")).toBeNull();
    expect(res.headers.get("content-security-policy") ?? "").not.toContain("cursor");
    const js = await registerMany([REDIRECT, "javascript:alert(1)"]);
    const jsRes = await call(authorizeUrl(js.clientId || "x", challenge, "read", "javascript:alert(1)"), { redirect: "manual" });
    expect(jsRes.status).not.toBe(200);
    expect(jsRes.headers.get("content-security-policy") ?? "").not.toContain("javascript");
  });
});

describe("formAction", () => {
  it("adds only a plain https origin, or http on localhost / 127.0.0.1", () => {
    expect(formAction("https://claude.ai/api/mcp/auth_callback")).toBe("'self' https://claude.ai");
    expect(formAction("https://chatgpt.com/connector_platform_oauth_redirect?x=1#f")).toBe("'self' https://chatgpt.com");
    expect(formAction("http://localhost:33418/callback")).toBe("'self' http://localhost:33418");
    expect(formAction("http://127.0.0.1/cb")).toBe("'self' http://127.0.0.1");
  });

  it("falls back to 'self' for other schemes, remote http, malformed values and CSP-injection attempts", () => {
    const bad: (string | undefined)[] = [
      undefined, "", "javascript:alert(1)", "data:text/html,<script>1</script>", "vbscript:x", "file:///etc/passwd", "blob:https://a.example/x",
      "cursor://anysphere.cursor-mcp/oauth/callback", "ftp://a.example/x", "ws://a.example", "wss://a.example",
      "http://evil.example/cb", "http://localhost.evil.example/cb", "http://[::1]:3000/cb",
      "not a url", "//evil.example", "/relative", "https://",
      "https://a.example; script-src *", "https://a.example;script-src.example/cb", "https://a.example'/cb", "https://a,b.example/cb",
      "https://a.example\r\nX-Evil: 1", "https://a.example/cb\nContent-Security-Policy: x", "https://a.example *",
      "https://[::1]/cb", "https://xn--‮.example/cb", "https://user:pw@a.example/cb", "https://a.example%0d%0a/cb",
    ];
    for (const value of bad) {
      const out = formAction(value);
      expect(out, String(value)).toMatch(/^'self'( https?:\/\/[a-z0-9.-]+(:\d{1,5})?)?$/);
      // the URL parser drops userinfo and line breaks, so these end as a clean origin or nothing; never raw text
      if (value && /^https:\/\/user:pw@a\.example|^https:\/\/a\.example\/cb\nContent/.test(value)) expect(out).toBe("'self' https://a.example");
      else expect(out, String(value)).toBe("'self'");
    }
  });
});

describe("GET /.well-known/oauth-protected-resource (root)", () => {
  it("returns the same document as the /mcp path-suffixed form", async () => {
    const root = await call("/.well-known/oauth-protected-resource");
    expect(root.status).toBe(200);
    const body = await root.json<any>();
    expect(body).toMatchObject({ resource: `${BASE}/mcp`, authorization_servers: [BASE] });
    const suffixed = await (await call("/.well-known/oauth-protected-resource/mcp")).json<any>();
    expect(body).toEqual(suffixed);
  });

  it("does not turn the root path into an open door for other methods or paths", async () => {
    expect((await call("/.well-known/oauth-protected-resource", { method: "POST", body: "{}" })).status).not.toBe(200);
    expect((await call("/.well-known/oauth-protected-resource/other")).status).not.toBe(200);
  });
});
