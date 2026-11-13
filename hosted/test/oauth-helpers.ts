import { BASE, OWNER, call } from "./helpers.js";

export const REDIRECT = "https://client.example/callback";
export const RESOURCE = `${BASE}/mcp`;

const b64url = (buf: ArrayBuffer | Uint8Array) =>
  btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export async function pkce(): Promise<{ verifier: string; challenge: string }> {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  return { verifier, challenge };
}

export async function registerClient(name = "Test Client", redirect = REDIRECT): Promise<string> {
  const res = await call("/register", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      client_name: name,
      redirect_uris: [redirect],
      token_endpoint_auth_method: "none",
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
    }),
  });
  if (res.status !== 201) throw new Error(`register failed: ${res.status} ${await res.text()}`);
  return ((await res.json()) as { client_id: string }).client_id;
}

export interface Page {
  status: number;
  html: string;
  headers: Headers;
  cookies: string;
  handle: string;
  csrf: string;
  url: string;
}

export function authorizeUrl(clientId: string, challenge: string, scope = "read write", redirect = REDIRECT, state = "st4te"): string {
  const q = new URLSearchParams({
    response_type: "code", client_id: clientId, redirect_uri: redirect, scope, state,
    code_challenge: challenge, code_challenge_method: "S256", resource: RESOURCE,
  });
  return `/authorize?${q}`;
}

const attr = (html: string, name: string) => new RegExp(`name="${name}" value="([^"]*)"`).exec(html)?.[1] ?? "";

export function cookieHeader(res: Response): string {
  return res.headers.getSetCookie().map((c) => c.split(";")[0]).join("; ");
}

export async function openConsent(url: string): Promise<Page> {
  const res = await call(url);
  const html = await res.text();
  return { status: res.status, html, headers: res.headers, cookies: cookieHeader(res), handle: attr(html, "handle"), csrf: attr(html, "csrf"), url };
}

export async function submitConsent(
  page: Page,
  fields: Record<string, string | string[]> = {},
  over: { cookies?: string; ip?: string } = {},
): Promise<Response> {
  const form = new URLSearchParams();
  const all: Record<string, string | string[]> = {
    handle: page.handle, csrf: page.csrf, owner_key: OWNER, device_name: "Test app", scope: ["read", "write"], decision: "approve", ...fields,
  };
  for (const [k, v] of Object.entries(all)) for (const x of Array.isArray(v) ? v : [v]) form.append(k, x);
  const headers: Record<string, string> = { "content-type": "application/x-www-form-urlencoded", cookie: over.cookies ?? page.cookies };
  if (over.ip) headers["cf-connecting-ip"] = over.ip;
  return call(page.url, { method: "POST", headers, body: form.toString(), redirect: "manual" });
}

export async function exchange(code: string, clientId: string, verifier: string): Promise<Response> {
  return call("/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: REDIRECT, client_id: clientId, code_verifier: verifier, resource: RESOURCE }).toString(),
  });
}

/** Run the whole flow: register, consent with the owner key, exchange the code. */
export async function oauthLogin(opts: { scope?: string; scopes?: string[]; name?: string; wikis?: string } = {}): Promise<{ access: string; refresh: string; clientId: string; deviceName: string }> {
  const clientId = await registerClient();
  const { verifier, challenge } = await pkce();
  const page = await openConsent(authorizeUrl(clientId, challenge, opts.scope ?? "read write"));
  if (page.status !== 200) throw new Error(`consent page ${page.status}: ${page.html}`);
  const deviceName = opts.name ?? "Chat app";
  const res = await submitConsent(page, {
    device_name: deviceName, ...(opts.scopes ? { scope: opts.scopes } : {}), ...(opts.wikis ? { wikis: opts.wikis } : {}),
  });
  if (res.status !== 302) throw new Error(`consent submit ${res.status}: ${await res.text()}`);
  const code = new URL(res.headers.get("location")!).searchParams.get("code");
  if (!code) throw new Error(`no code in ${res.headers.get("location")}`);
  const tok = await exchange(code, clientId, verifier);
  const body = (await tok.json()) as { access_token?: string; refresh_token?: string };
  if (!body.access_token) throw new Error(`token exchange failed: ${JSON.stringify(body)}`);
  return { access: body.access_token, refresh: body.refresh_token ?? "", clientId, deviceName };
}
