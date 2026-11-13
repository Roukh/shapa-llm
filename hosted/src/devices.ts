// Devices: one row per credential holder (a minted token or one OAuth grant). Tokens are
// stored only as SHA-256 of their secret. The same table backs scope and wiki-allowlist checks
// and the failed-owner-key counter.

import { ApiError } from "./errors.js";
import { SCOPES, WIKI_KEY_RE, nowIso, type Scope } from "./records.js";

export interface Device {
  id: string;
  name: string;
  scopes: Scope[];
  wikis: string[] | null;
  source: "token" | "oauth";
  created: string;
  last_seen: string;
  revoked: string | null;
}

export const FAIL_LIMIT = 5;
export const FAIL_WINDOW_MS = 15 * 60 * 1000;
const TOUCH_EVERY_MS = 60_000;

export function randomHex(bytes: number): string {
  const b = new Uint8Array(bytes);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

/** Constant-time comparison of two strings (a length mismatch still walks the longer one). */
export function timingSafeEqualStr(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  const n = Math.max(x.length, y.length);
  for (let i = 0; i < n; i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

export function validDeviceName(name: unknown): name is string {
  return typeof name === "string" && name.length >= 1 && name.length <= 64 && !/[\p{Cc}\p{Cf}\u2028\u2029]/u.test(name) && name.trim() === name;
}

export function validWikis(wikis: unknown): wikis is string[] | null {
  return wikis === null || (Array.isArray(wikis) && wikis.length <= 100 && wikis.every((w) => typeof w === "string" && WIKI_KEY_RE.test(w)));
}

export function validScopes(scopes: unknown): scopes is Scope[] {
  return Array.isArray(scopes) && scopes.length >= 1 && scopes.every((s) => (SCOPES as readonly string[]).includes(s));
}

type Row = Record<string, SqlStorageValue>;

function fromRow(r: Row): Device {
  return {
    id: r["id"] as string,
    name: r["name"] as string,
    scopes: JSON.parse(r["scopes"] as string) as Scope[],
    wikis: r["wikis"] === null ? null : (JSON.parse(r["wikis"] as string) as string[]),
    source: r["source"] as "token" | "oauth",
    created: r["created"] as string,
    last_seen: r["last_seen"] as string,
    revoked: (r["revoked"] as string | null) ?? null,
  };
}

export class Devices {
  constructor(private readonly sql: SqlStorage) {}

  create(d: { name: string; scopes: Scope[]; wikis: string[] | null; source: "token" | "oauth"; tokenHash: string }): Device {
    const id = randomHex(8);
    const scopes = SCOPES.filter((s) => d.scopes.includes(s));
    this.sql.exec(
      `INSERT INTO devices (id, name, scopes, wikis, token_hash, source, created) VALUES (?, ?, ?, ?, ?, ?, ?)`,
      id, d.name, JSON.stringify(scopes), d.wikis === null ? null : JSON.stringify([...new Set(d.wikis)]), d.tokenHash, d.source, nowIso(),
    );
    return this.get(id)!;
  }

  get(id: string): Device | null {
    const r = this.sql.exec(`SELECT * FROM devices WHERE id = ?`, id).toArray()[0];
    return r ? fromRow(r) : null;
  }

  list(): Device[] {
    return this.sql.exec(`SELECT * FROM devices ORDER BY created, id`).toArray().map(fromRow);
  }

  /** Mark revoked (idempotent). Returns false for an unknown id. */
  revoke(id: string): boolean {
    const d = this.get(id);
    if (!d) return false;
    if (!d.revoked) this.sql.exec(`UPDATE devices SET revoked = ?, token_hash = '' WHERE id = ?`, nowIso(), id);
    return true;
  }

  /** The live device for a request, or null when unknown or revoked. Refreshes `last_seen` at most once a minute. */
  active(id: string): Device | null {
    const d = this.get(id);
    if (!d || d.revoked) return null;
    const now = Date.now();
    if (!d.last_seen || now - Date.parse(d.last_seen) >= TOUCH_EVERY_MS) {
      const ts = nowIso();
      this.sql.exec(`UPDATE devices SET last_seen = ? WHERE id = ?`, ts, id);
      d.last_seen = ts;
    }
    return d;
  }

  /** Check a presented token secret (already hashed by the caller) against the stored hash. */
  verify(id: string, secretHash: string): Device | null {
    const r = this.sql.exec(`SELECT token_hash FROM devices WHERE id = ? AND source = 'token'`, id).toArray()[0];
    const stored = (r?.["token_hash"] as string | undefined) ?? "";
    // Always compare, even when the device is unknown, so timing does not reveal which ids exist.
    const same = timingSafeEqualStr(stored || "0".repeat(64), secretHash);
    if (!r || !stored || !same) return null;
    return this.active(id);
  }

  // --- failed owner-key attempts --------------------------------------------------------------

  /** Seconds to wait when this IP has used up its failures, else 0. */
  blockedFor(ip: string): number {
    const now = Date.now();
    this.sql.exec(`DELETE FROM attempts WHERE ts < ?`, now - FAIL_WINDOW_MS);
    const rows = this.sql.exec(`SELECT ts FROM attempts WHERE ip = ? ORDER BY ts`, ip).toArray();
    if (rows.length < FAIL_LIMIT) return 0;
    const oldest = rows[rows.length - FAIL_LIMIT]!["ts"] as number;
    return Math.max(1, Math.ceil((oldest + FAIL_WINDOW_MS - now) / 1000));
  }

  recordFailure(ip: string): void {
    this.sql.exec(`INSERT INTO attempts (ip, ts) VALUES (?, ?)`, ip, Date.now());
  }
}

// --- checks the store applies to every operation ---------------------------------------------

export function needScope(d: Device, scope: Scope): void {
  if (!d.scopes.includes(scope)) throw new ApiError("forbidden", `this device lacks the ${scope} scope`);
}

export function wikiAllowed(d: Device, wiki: string): boolean {
  return d.wikis === null || d.wikis.includes(wiki);
}

export function needWiki(d: Device, wiki: string): void {
  if (!wikiAllowed(d, wiki)) throw new ApiError("forbidden", `this device may not use wiki ${wiki}`);
}
