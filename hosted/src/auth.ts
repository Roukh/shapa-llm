// Credentials other than OAuth: the owner key (admin and the consent page) and device tokens
// `shp_<device_id>_<secret>`. A device token resolves to a device row in the store on every
// request, so revoking the device refuses it on the next call. Nothing here logs a token or the
// owner key. OAuth access tokens are handled in oauth.ts.

import { randomHex, timingSafeEqualStr } from "./devices.js";
import { ApiError, unwrap } from "./errors.js";
import { STORE_NAME, type Cred, type ShapaStore } from "./store.js";

export const OWNER_KEY_MIN = 32;
const TOKEN_RE = /^shp_([0-9a-f]{16})_([0-9a-f]{64})$/;

/** The store's own methods, each as an async RPC call. Workers RPC's generic stub type flattens
 * tuples (`[dst, rel]` becomes `string[]`), so the stub is typed from the class instead. */
export type StoreApi = {
  [K in Exclude<keyof ShapaStore, keyof DurableObject>]: ShapaStore[K] extends (...a: infer A) => infer R
    ? (...a: A) => Promise<R>
    : never;
};

export function store(env: Env): StoreApi {
  return env.STORE.getByName(STORE_NAME) as unknown as StoreApi;
}

export async function sha256Hex(text: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function bearerOf(request: Request): string | null {
  const h = request.headers.get("authorization");
  const m = h ? /^Bearer\s+(\S+)$/i.exec(h) : null;
  return m ? m[1]! : null;
}

/** A well-formed device token as a credential for the store, or null for any other string. */
export async function deviceCred(token: string): Promise<Cred | null> {
  const m = TOKEN_RE.exec(token);
  return m ? { device: m[1]!, secretHash: await sha256Hex(m[2]!) } : null;
}

// --- owner key --------------------------------------------------------------------------------

export function clientIp(request: Request): string {
  return request.headers.get("cf-connecting-ip")?.slice(0, 64) || "unknown";
}

/** Constant-time check of a presented owner key. The key length (32+) is enforced first: a Worker
 * with a short or missing key refuses admin and consent (503) instead of accepting a weak secret. */
async function ownerKeyMatches(env: Env, presented: string): Promise<boolean> {
  const [a, b] = await Promise.all([sha256Hex(env.SHAPA_OWNER_KEY), sha256Hex(presented)]);
  return timingSafeEqualStr(a, b);
}

export function ownerKeyConfigured(env: Env): boolean {
  return typeof env.SHAPA_OWNER_KEY === "string" && env.SHAPA_OWNER_KEY.length >= OWNER_KEY_MIN;
}

/** Check an owner key with the per-IP failure limit (5 per 15 minutes, then 429). A missing
 * key is a plain 401 and does not count; a wrong key does. */
export async function requireOwnerKey(env: Env, request: Request, presented: string | null): Promise<void> {
  if (!ownerKeyConfigured(env)) {
    throw new ApiError("unavailable", "the owner key is not configured (SHAPA_OWNER_KEY needs 32 or more characters)");
  }
  const s = store(env);
  const ip = clientIp(request);
  const wait = await unwrap(s.ownerBlocked(ip));
  if (wait > 0) throw new ApiError("rate_limited", "too many failed attempts; try again later", undefined, { "Retry-After": String(wait) });
  if (!presented) throw new ApiError("unauthorized", "owner key required");
  if (await ownerKeyMatches(env, presented)) return;
  await unwrap(s.ownerFailed(ip));
  throw new ApiError("unauthorized", "wrong owner key");
}

/** A device token to a store credential, or null unless the device behind it is live (so
 * revoking a device takes effect on its next request). */
export async function resolveDeviceBearer(env: Env, token: string): Promise<Cred | null> {
  const cred = await deviceCred(token);
  if (!cred) return null;
  const who = await store(env).whoami(cred);
  return who.ok ? cred : null;
}

/** A fresh 256-bit token secret, hex. */
export const randomToken = (): string => randomHex(32);

export function unauthorized(extra = ""): Response {
  return Response.json(
    { error: { code: "unauthorized", message: "a valid device token or OAuth access token is required" } },
    { status: 401, headers: { "WWW-Authenticate": `Bearer realm="shapa"${extra}` } },
  );
}
