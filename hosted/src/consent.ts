// The OAuth consent page (GET and POST /authorize): server-rendered, no script, no third-party
// content. The owner proves who they are with the owner key, names the device and picks the
// scopes; each approved grant becomes one device row. Defences: a CSRF token (double-submit
// cookie) on top of the provider's browser-bound consent handle, `frame-ancestors 'none'`, a
// nonce-only style policy, every client-supplied string escaped, the owner-key failure limit, and
// no redirect except the provider's own to a validated, registered redirect URI.

import { AuthorizationError, type OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import { ownerKeyConfigured, requireOwnerKey, store } from "./auth.js";
import { randomHex, scopesCoherent, timingSafeEqualStr, validDeviceName } from "./devices.js";
import { ApiError, unwrap } from "./errors.js";
import type { OAuthKit } from "./oauth.js";
import { SCOPES, WIKI_KEY_RE, type Scope } from "./records.js";

const CSRF_COOKIE = "__Host-shapa-csrf";
const DEFAULT_SCOPES: Scope[] = ["read", "write"];

export const escapeHtml = (value: string): string => value.replace(/[&<>"'`]/g, (ch) => `&#${ch.charCodeAt(0)};`);

function securityHeaders(headers: Headers, nonce: string): Headers {
  headers.set("Content-Type", "text/html; charset=utf-8");
  headers.set(
    "Content-Security-Policy",
    `default-src 'none'; style-src 'nonce-${nonce}'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`,
  );
  headers.set("X-Frame-Options", "DENY");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "no-referrer");
  headers.set("Cache-Control", "no-store");
  return headers;
}

function csrfFrom(request: Request): string {
  const m = new RegExp(`(?:^|;\\s*)${CSRF_COOKIE}=([0-9a-f]{32})(?:;|$)`).exec(request.headers.get("cookie") ?? "");
  return m ? m[1]! : "";
}

const field = (form: FormData, name: string): string => {
  const v = form.get(name);
  return typeof v === "string" ? v : "";
};

interface View {
  clientName: string;
  clientDomain?: string;
  redirectHost: string;
  redirectIsLoopback: boolean;
  requested: string[];
}

function page(view: View, handle: string, csrf: string, nonce: string, error: string): string {
  const requested = view.requested.filter((s): s is Scope => (SCOPES as readonly string[]).includes(s));
  const checked = new Set<string>(requested.length ? requested : DEFAULT_SCOPES);
  const boxes = SCOPES.map(
    (s) => `<label class="check"><input type="checkbox" name="scope" value="${s}"${checked.has(s) ? " checked" : ""}> ${s}</label>`,
  ).join("");
  const origin = view.clientDomain
    ? `Published by <strong>${escapeHtml(view.clientDomain)}</strong>.`
    : "This app registered itself; its name is not verified.";
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize an app</title>
<style nonce="${nonce}">
body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a;background:#fff}
@media (prefers-color-scheme:dark){body{color:#eee;background:#111}input,button{color:inherit;background:#222}}
label{display:block;margin:.75rem 0 .25rem;font-weight:600}.check{display:inline-block;margin-right:1rem;font-weight:400}
input[type=text],input[type=password]{width:100%;padding:.5rem;box-sizing:border-box;font:inherit}
button{padding:.5rem 1rem;font:inherit;margin-right:.5rem}.err{color:#b00020;font-weight:600}.warn{border-left:4px solid #b36b00;padding-left:.75rem}
</style></head><body>
<h1>Allow ${escapeHtml(view.clientName)} to use your shapa?</h1>
<p>${origin} Access will be sent to <strong>${escapeHtml(view.redirectHost)}</strong>.</p>
${view.redirectIsLoopback ? '<p class="warn"><strong>This sends access to an app on your computer.</strong> Continue only if you just started signing in from it.</p>' : ""}
${error ? `<p class="err" role="alert">${escapeHtml(error)}</p>` : ""}
<p>A chat app reads your stage-0 baseline, notes, tasks, ideas and project files, and writes notes, tasks, ideas, decisions and files. It never syncs, edits rules or the focus, or deletes.</p>
<form method="post">
<input type="hidden" name="handle" value="${escapeHtml(handle)}">
<input type="hidden" name="csrf" value="${escapeHtml(csrf)}">
<label for="owner_key">Owner key</label>
<input id="owner_key" name="owner_key" type="password" autocomplete="off" required>
<label for="device_name">Device name</label>
<input id="device_name" name="device_name" type="text" maxlength="64" autocomplete="off" required>
<label>Scopes</label><div>${boxes}</div>
<label for="wikis">Limit to wikis (optional, space separated; empty means all)</label>
<input id="wikis" name="wikis" type="text" maxlength="500" autocomplete="off">
<p><button name="decision" value="approve" type="submit">Allow</button><button name="decision" value="deny" type="submit" formnovalidate>Deny</button></p>
</form></body></html>`;
}

function plain(message: string, status: number): Response {
  const headers = securityHeaders(new Headers(), randomHex(8));
  headers.set("Content-Type", "text/plain; charset=utf-8");
  return new Response(message, { status, headers });
}

function authError(e: unknown): Response {
  if (e instanceof AuthorizationError) {
    return e.redirectTo ? Response.redirect(e.redirectTo, 302) : plain(`Authorization failed: ${e.description}`, 400);
  }
  if (e instanceof ApiError) return plain(e.message, e.status);
  throw e;
}

async function render(oauth: OAuthHelpers, request: Request, error: string, status: number): Promise<Response> {
  const authReq = await oauth.parseAuthRequest(request);
  const details = await oauth.describeConsent(authReq);
  const consent = await oauth.beginConsent(authReq);
  const nonce = randomHex(16);
  const csrf = randomHex(16);
  const headers = securityHeaders(consent.headers, nonce);
  headers.append("Set-Cookie", `${CSRF_COOKIE}=${csrf}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=900`);
  const view: View = {
    clientName: details.clientName,
    redirectHost: details.redirectHost,
    redirectIsLoopback: details.redirectIsLoopback,
    requested: details.scope,
    ...(details.clientDomain ? { clientDomain: details.clientDomain } : {}),
  };
  return new Response(page(view, consent.handle, csrf, nonce, error), { status, headers });
}

export async function authorize(request: Request, env: Env, kit: OAuthKit): Promise<Response> {
  if (request.method !== "GET" && request.method !== "POST") return plain("method not allowed", 405);
  if (!ownerKeyConfigured(env)) return plain("The owner key is not configured on this server.", 503);
  const oauth = kit.server.getOAuthApi(env);
  try {
    if (request.method === "GET") return await render(oauth, request, "", 200);
    return await decide(oauth, request, env);
  } catch (e) {
    return authError(e);
  }
}

async function decide(oauth: OAuthHelpers, request: Request, env: Env): Promise<Response> {
  const form = await request.formData();
  const cookie = csrfFrom(request);
  if (!cookie || !timingSafeEqualStr(cookie, field(form, "csrf"))) {
    return plain("This form has expired or did not come from this page. Go back to the app and start again.", 403);
  }
  const handle = field(form, "handle");
  if (field(form, "decision") !== "approve") {
    const denied = await oauth.denyConsent(request, handle);
    denied.headers.set("Location", denied.redirectTo);
    return new Response(null, { status: 302, headers: denied.headers });
  }

  const again = (message: string, status: number) => render(oauth, request, message, status);
  try {
    await requireOwnerKey(env, request, field(form, "owner_key") || null);
  } catch (e) {
    if (e instanceof ApiError && e.code === "unauthorized") return again("That is not the owner key.", 401);
    if (e instanceof ApiError && e.code === "rate_limited") return plain("Too many wrong owner keys. Try again later.", 429);
    throw e;
  }
  const name = field(form, "device_name");
  if (!validDeviceName(name)) return again("Give the device a name of 1 to 64 characters.", 400);
  const scopes = SCOPES.filter((s) => form.getAll("scope").includes(s));
  if (scopes.length === 0) return again("Pick at least one scope.", 400);
  if (!scopesCoherent(scopes)) return again("Write needs read, and delete needs read and write.", 400);
  const wikiText = field(form, "wikis").trim();
  const wikis = wikiText ? [...new Set(wikiText.split(/[\s,]+/).filter(Boolean))] : null;
  if (wikis && !wikis.every((w) => WIKI_KEY_RE.test(w))) return again("Wiki keys are lowercase letters, digits, dots, dashes and underscores.", 400);

  const approved = await oauth.approveConsent(request, handle, { scope: scopes });
  const s = store(env);
  const device = await unwrap(s.createDevice({ name, scopes, wikis, source: "oauth", tokenHash: "" }));
  try {
    const { redirectTo } = await oauth.completeAuthorization({
      request: approved.request,
      userId: device.id,
      metadata: { device: device.name },
      scope: scopes,
      props: { deviceId: device.id },
    });
    approved.headers.set("Location", redirectTo);
    return new Response(null, { status: 302, headers: approved.headers });
  } catch (e) {
    await s.revokeDevice(device.id);
    throw e;
  }
}
