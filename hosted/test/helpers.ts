import { env, exports } from "cloudflare:workers";

export const BASE = "https://shapa.test";
export const OWNER = "test-owner-key-0123456789abcdef0123456789";

/** Call the real Worker (and its Durable Object) like a client would. */
export function call(path: string, init: RequestInit = {}): Promise<Response> {
  return exports.default.fetch(new Request(`${BASE}${path}`, init));
}

export interface Reply<T = any> {
  status: number;
  body: T;
  headers: Headers;
  text: string;
}

export async function send(
  method: string,
  path: string,
  opts: { token?: string; body?: unknown; raw?: string; headers?: Record<string, string> } = {},
): Promise<Reply> {
  const headers: Record<string, string> = { ...opts.headers };
  if (opts.token) headers["authorization"] = `Bearer ${opts.token}`;
  let body: string | undefined;
  if (opts.raw !== undefined) body = opts.raw;
  else if (opts.body !== undefined) body = JSON.stringify(opts.body);
  if (body !== undefined && !headers["content-type"]) headers["content-type"] = "application/json";
  const res = await call(path, { method, headers, ...(body !== undefined ? { body } : {}) });
  const text = await res.text();
  let parsed: unknown = text;
  try {
    parsed = JSON.parse(text);
  } catch {
    /* keep the text */
  }
  return { status: res.status, body: parsed, headers: res.headers, text };
}

export const get = (path: string, token?: string) => send("GET", path, token ? { token } : {});
export const post = (path: string, body: unknown, token?: string) => send("POST", path, { body, ...(token ? { token } : {}) });

export interface Minted {
  id: string;
  token: string;
}

/** Mint a device token through the admin API with the owner key. */
export async function mint(name: string, scopes: string[] = ["read", "write"], wikis: string[] | null = null): Promise<Minted> {
  const r = await send("POST", "/admin/devices", { token: OWNER, body: { name, scopes, wikis } });
  if (r.status !== 201) throw new Error(`mint failed: ${r.status} ${r.text}`);
  return { id: r.body.id, token: r.body.token };
}

/** A device that can read, write and delete everything. */
export const mintAll = (name = "tester") => mint(name, ["read", "write", "delete"]);

export async function wiki(token: string, key: string, kind: "global" | "repo" = "repo", origin = ""): Promise<void> {
  const r = await send("PUT", `/v1/wikis/${key}`, { token, body: { name: key, kind, origin } });
  if (r.status !== 200 && r.status !== 201) throw new Error(`wiki failed: ${r.status} ${r.text}`);
}

export const TS = "2026-01-01T00:00:00Z";
export const TS2 = "2026-01-02T00:00:00Z";
export const TS3 = "2026-01-03T00:00:00Z";

/** A deterministic uid for a test record: the same id and created time give the same uid. */
export const uidOf = (id: string, created: string): string => `u${id}${created.replace(/\D/g, "")}`.padEnd(16, "x");

export function row(id: string, summary: string, over: Record<string, unknown> = {}) {
  return {
    id, uid: uidOf(id, String(over["created"] ?? TS)), kind: id[0], created: TS, updated: TS, deleted: false,
    data: { summary, body: "", alias: null, source: "", session: "" }, tags: [], links: [], ...over,
  };
}

export function job(id: string, title: string, parent: string | null = null, over: Record<string, unknown> = {}) {
  return {
    id, uid: uidOf(id, String(over["created"] ?? TS)), kind: id[0], created: TS, updated: TS, deleted: false,
    data: { title, body: "", parent, verify: null, status: "open", closed_reason: null, git_ref: null, alias: null, claimed_by: null, closed: null },
    tags: [], links: [], ...over,
  };
}

export const put = (record: unknown, base_rev: number | null = null) => ({ op: "put", base_rev, record });
/** A del op for the record that `row`/`job` made with this created time (or pass uid explicitly). */
export const del = (id: string, created: string, deleted_at: string, base_rev: number | null = null, uid = uidOf(id, created)) => ({ op: "del", id, uid, deleted_at, base_rev });

export function sync(token: string, wikiKey: string, req: { cursor?: number; ops?: unknown[]; want?: string[]; counters?: Record<string, number> }) {
  return send("POST", `/v1/wikis/${wikiKey}/sync`, { token, body: { cursor: 0, ops: [], want: [], counters: {}, ...req } });
}

let counter = 0;
/** A fresh wiki key, so tests never depend on each other's data. */
export const freshKey = (): string => `w${++counter}-${Math.random().toString(36).slice(2, 7)}`;

/** A device with every scope plus a registered repo wiki. */
export async function setup(): Promise<{ token: string; id: string; w: string }> {
  const d = await mintAll();
  const w = freshKey();
  await wiki(d.token, w);
  return { token: d.token, id: d.id, w };
}

export const rec = (w: string, tail = "") => `/v1/wikis/${w}/records${tail}`;

export { env };
