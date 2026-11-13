// The wire model: zod schemas are the single source of every request, response and stored
// shape. REST validates with them, the OpenAPI document is generated from them, the MCP tools
// reuse their fields, and the store normalizes every incoming record through `normalizeRecord`.

import { z } from "@hono/zod-openapi";
import { redact } from "./redact.js";

// --- limits (the contract's numbers, enforced everywhere) -----------------------------

export const LIMITS = {
  recordBytes: 1_000_000, // one record <= 1 MB JSON
  opsPerSync: 200,
  changesPage: 500,
  wantIds: 500,
  syncBodyBytes: 8 * 1024 * 1024, // a sync request; clients batch by size as well as op count
  jsonBodyBytes: 1_100_000, // any other JSON body: one record plus envelope
  summary: 160,
  title: 200,
  tag: 80,
  tags: 100,
  links: 200,
  searchK: 50,
  listLimit: 200,
  instructionsBudget: { min: 2000, default: 6000, max: 40_000 },
} as const;

export const KINDS = ["F", "J", "T", "M", "R", "I", "D", "N"] as const;
export type Kind = (typeof KINDS)[number];
export const LEDGER_KINDS = ["F", "J", "T"] as const;
export const ROW_KINDS = ["M", "R", "I"] as const;
export const COUNTER_KINDS = ["F", "J", "T", "M", "R", "I", "D"] as const;

export const TYPE_NAMES = {
  F: "feature",
  J: "job",
  T: "task",
  M: "memory",
  R: "rule",
  I: "issue",
  D: "document",
  N: "note",
} as const satisfies Record<Kind, string>;

export const LINK_RELS = ["about", "supersedes", "conflicts", "related"] as const;
export const STATUSES = ["open", "claimed", "closed"] as const;
export const CLOSE_REASONS = ["merge", "commit", "agent", "expired", "parent", "operator"] as const;
export const SCOPES = ["read", "write", "delete"] as const;
export type Scope = (typeof SCOPES)[number];

/** A job's parent is a feature, a task's is a job; both may stand alone. */
export const PARENT_KIND: Partial<Record<Kind, Kind>> = { J: "F", T: "J" };

export const ID_RE = /^([FJTMRID])([1-9][0-9]{0,8})$/;
/** Highest number an id may carry (nine digits), so every allocated id stays valid on the wire. */
export const MAX_COUNTER = 999_999_999;
export const WIKI_KEY_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;
export const NOTE_ID_RE = /^(arch\/|research\/)?[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$/;
const ALIAS_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/;
const LINK_DST_RE = /^[A-Za-z0-9][A-Za-z0-9._/:-]{0,119}$/;
/** The identity of a record: random, set where it is created, never changed. Notes use "" (their path). */
export const UID_RE = /^[A-Za-z0-9_-]{16,64}$/;
const TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const MIME_RE = /^[A-Za-z0-9][\w.+-]{0,60}\/[A-Za-z0-9][\w.+-]{0,60}$/;
/** Root notes that are shipped by shapa itself and never sync (either case, for case-folding disks). */
const RESERVED_ROOT_NOTES = new Set(["agents.md", "placement.md"]);

export function isNoteId(id: string): boolean {
  if (!NOTE_ID_RE.test(id)) return false;
  if (!id.includes("/") && RESERVED_ROOT_NOTES.has(id.toLowerCase())) return false;
  return true;
}

export function isTimestamp(value: string): boolean {
  return TS_RE.test(value) && !Number.isNaN(Date.parse(value));
}

export function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Kind letter of a ledger or row id (`M12`), `D` for documents; undefined otherwise. */
export function kindOfId(id: string): Kind | undefined {
  const m = ID_RE.exec(id);
  return m ? (m[1] as Kind) : undefined;
}

// --- text helpers ------------------------------------------------------------------------

/** Code point order, like Python's `sorted()` on str. */
export function cmp(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  const n = Math.min(x.length, y.length);
  for (let i = 0; i < n; i++) {
    const d = x[i]!.codePointAt(0)! - y[i]!.codePointAt(0)!;
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

export function oneLine(text: unknown, limit: number): string {
  const s = String(text ?? "").split(/\s+/).filter(Boolean).join(" ");
  const chars = Array.from(s);
  return chars.length <= limit ? s : chars.slice(0, limit - 1).join("").trimEnd() + "…";
}

export function cleanTag(tag: unknown): string {
  return Array.from(String(tag ?? "").split(/\s+/).filter(Boolean).join(" "))
    .slice(0, LIMITS.tag)
    .join("");
}

export function normTags(tags: unknown): string[] {
  const set = new Set<string>();
  if (Array.isArray(tags)) for (const t of tags) { const c = cleanTag(t); if (c) set.add(c); }
  return [...set].sort(cmp).slice(0, LIMITS.tags);
}

export type Link = [string, string];

export function normLinks(links: unknown): Link[] {
  const seen = new Set<string>();
  const out: Link[] = [];
  if (!Array.isArray(links)) return out;
  for (const l of links) {
    if (!Array.isArray(l) || l.length !== 2) continue;
    const [dst, rel] = l as [unknown, unknown];
    if (typeof dst !== "string" || typeof rel !== "string") continue;
    if (!LINK_DST_RE.test(dst) || !(LINK_RELS as readonly string[]).includes(rel)) continue;
    const key = `${dst}\u0000${rel}`;
    if (!seen.has(key)) { seen.add(key); out.push([dst, rel]); }
  }
  return out.sort((a, b) => cmp(a[0], b[0]) || cmp(a[1], b[1])).slice(0, LIMITS.links);
}

/** An alias is a short slug that is not itself an id; anything else is dropped. */
export function cleanAlias(alias: unknown): string {
  if (typeof alias !== "string") return "";
  const a = alias.trim();
  return ALIAS_RE.test(a) && !ID_RE.test(a) && !isNoteIdShaped(a) ? a : "";
}
const isNoteIdShaped = (a: string) => NOTE_ID_RE.test(a);

// --- per-kind data -------------------------------------------------------------------------

const ts = z.string().regex(TS_RE);

export const LedgerData = z.object({
  title: z.string(),
  body: z.string().default(""),
  parent: z.string().nullable().default(null),
  verify: z.string().nullable().default(null),
  status: z.enum(STATUSES).default("open"),
  closed_reason: z.enum(CLOSE_REASONS).nullable().default(null),
  git_ref: z.string().max(200).nullable().default(null),
  alias: z.string().nullable().default(null),
  claimed_by: z.string().max(64).nullable().default(null),
  closed: ts.nullable().default(null),
}).openapi("LedgerData");

export const RowData = z.object({
  summary: z.string().describe("One line, up to 160 characters."),
  body: z.string().default(""),
  alias: z.string().nullable().default(null),
  source: z.string().max(200).default(""),
  session: z.string().max(200).default(""),
}).openapi("RowData");

export const DocData = z.object({
  title: z.string().describe("One line, up to 200 characters."),
  body: z.string().default(""),
  alias: z.string().nullable().default(null),
  mime: z.string().default("text/markdown"),
  source: z.string().max(200).default(""),
}).openapi("DocData");

export const NoteData = z.object({
  body: z.string().describe("The whole file."),
  alias: z.string().nullable().optional().describe("Server-derived: frontmatter id, else the stem."),
  summary: z.string().optional().describe("Server-derived from frontmatter or the first line."),
}).openapi("NoteData");

export type Data = Record<string, unknown>;

/** Top-level scalar keys of a note's `---` frontmatter block (enough for `id` and `summary`). */
export function frontmatter(body: string): Record<string, string> {
  const m = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(body);
  const out: Record<string, string> = {};
  if (!m) return out;
  for (const line of m[1]!.split(/\r?\n/)) {
    const kv = /^([A-Za-z_][\w-]*):\s*(.*)$/.exec(line);
    if (!kv) continue;
    let v = kv[2]!.trim();
    if (/^(["']).*\1$/.test(v) && v.length >= 2) v = v.slice(1, -1);
    out[kv[1]!] = v;
  }
  return out;
}

function noteDerived(id: string, body: string): { alias: string; summary: string } {
  const fm = frontmatter(body);
  const stem = (id.split("/").pop() ?? id).replace(/\.md$/, "");
  const alias = cleanAlias(fm["id"]) || cleanAlias(stem);
  const stripped = body.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "");
  const firstLine = stripped.split(/\r?\n/).map((l) => l.replace(/^#+\s*/, "").trim()).find(Boolean) ?? "";
  const summary = oneLine(redact(fm["summary"] || firstLine || stem), LIMITS.summary);
  return { alias, summary };
}

export class Invalid extends Error {}

function zparse<T extends z.ZodType>(schema: T, raw: unknown, what: string): z.infer<T> {
  const r = schema.safeParse(raw);
  if (!r.success) {
    const issue = r.error.issues[0];
    throw new Invalid(`${what}: ${issue?.path.join(".") || "value"} ${issue?.message ?? "is invalid"}`);
  }
  return r.data;
}

/** A normalized, redacted record body ready to store. `alias` is the requested alias; the
 * store drops it when another live record already holds it. */
export interface Normalized {
  id: string;
  uid: string;
  kind: Kind;
  created: string;
  updated: string;
  deleted: boolean;
  alias: string;
  data: Data;
  tags: string[];
  links: Link[];
}

function normalizeData(kind: Kind, id: string, raw: unknown): { data: Data; alias: string } {
  const obj = typeof raw === "object" && raw !== null && !Array.isArray(raw) ? raw : {};
  if (kind === "N") {
    const { body } = zparse(NoteData.pick({ body: true }), obj, "data");
    const text = redact(body);
    const d = noteDerived(id, text);
    return { data: { body: text, alias: d.alias || null, summary: d.summary }, alias: d.alias };
  }
  if (kind === "F" || kind === "J" || kind === "T") {
    const d = zparse(LedgerData, obj, "data");
    const title = oneLine(redact(d.title), LIMITS.title);
    if (!title) throw new Invalid("data: title is required");
    if (d.parent !== null) {
      const want = PARENT_KIND[kind];
      if (!want || kindOfId(d.parent) !== want) {
        throw new Invalid(`data: parent of a ${TYPE_NAMES[kind]} must be a ${want ? TYPE_NAMES[want] : "nothing"} id`);
      }
    }
    const alias = cleanAlias(d.alias);
    return {
      alias,
      data: {
        title,
        body: redact(d.body),
        parent: d.parent,
        verify: d.verify === null ? null : redact(d.verify),
        status: d.status,
        closed_reason: d.closed_reason,
        git_ref: d.git_ref,
        alias: alias || null,
        claimed_by: d.claimed_by,
        closed: d.closed,
      },
    };
  }
  if (kind === "D") {
    const d = zparse(DocData, obj, "data");
    const title = oneLine(redact(d.title), LIMITS.title);
    if (!title) throw new Invalid("data: title is required");
    const alias = cleanAlias(d.alias);
    const mime = MIME_RE.test(d.mime) ? d.mime : "text/markdown";
    return { alias, data: { title, body: redact(d.body), alias: alias || null, mime, source: redact(d.source) } };
  }
  const d = zparse(RowData, obj, "data");
  const summary = oneLine(redact(d.summary), LIMITS.summary);
  if (!summary) throw new Invalid("data: summary is required");
  const alias = cleanAlias(d.alias);
  return {
    alias,
    data: { summary, body: redact(d.body.trim()), alias: alias || null, source: redact(d.source), session: redact(d.session) },
  };
}

/** The record as a client sends it in a sync `put`; `rev` and `device` are accepted and ignored. */
export const RecordIn = z.object({
  id: z.string().min(1).max(120),
  uid: z.string().max(64),
  kind: z.enum(KINDS),
  created: z.string().max(40),
  updated: z.string().max(40),
  rev: z.unknown().optional(),
  deleted: z.boolean().default(false),
  device: z.unknown().optional(),
  data: z.record(z.string(), z.unknown()).default({}),
  tags: z.array(z.unknown()).default([]),
  links: z.array(z.unknown()).default([]),
});

/** Validate and normalize an incoming record. Throws `Invalid` with a short reason. */
export function normalizeRecord(raw: unknown): Normalized {
  let size: number;
  try {
    size = new TextEncoder().encode(JSON.stringify(raw ?? null)).length;
  } catch {
    throw new Invalid("record is not plain JSON (too deeply nested?)");
  }
  if (size > LIMITS.recordBytes) throw new Invalid("record is larger than 1 MB");
  const r = zparse(RecordIn, raw, "record");
  const kind = r.kind;
  if (kind === "N") {
    if (!isNoteId(r.id)) throw new Invalid("record: note id must be a relative .md path under arch/ or research/ or the root");
    if (r.created !== "") throw new Invalid("record: a note has no created time");
    if (r.uid !== "") throw new Invalid("record: a note has no uid (its path is its identity)");
  } else {
    if (!UID_RE.test(r.uid)) throw new Invalid("record: uid must be 16-64 characters of A-Z a-z 0-9 _ -");
    if (kindOfId(r.id) !== kind) throw new Invalid(`record: id ${r.id} does not belong to kind ${kind}`);
    if (!isTimestamp(r.created)) throw new Invalid("record: created must be YYYY-MM-DDTHH:MM:SSZ");
  }
  if (!isTimestamp(r.updated)) throw new Invalid("record: updated must be YYYY-MM-DDTHH:MM:SSZ");
  if (r.deleted) {
    return { id: r.id, uid: r.uid, kind, created: r.created, updated: r.updated, deleted: true, alias: "", data: {}, tags: [], links: [] };
  }
  const { data, alias } = normalizeData(kind, r.id, r.data);
  return {
    id: r.id, uid: r.uid, kind, created: r.created, updated: r.updated, deleted: false, alias, data,
    tags: normTags(r.tags), links: normLinks(r.links),
  };
}

/** Normalize the data of a record the server itself builds (REST and MCP writes). */
export function buildData(kind: Kind, id: string, raw: Data): { data: Data; alias: string } {
  try {
    return normalizeData(kind, id, raw);
  } catch (e) {
    if (e instanceof Invalid) throw e;
    throw new Invalid("invalid data");
  }
}

/** Stable text for comparing two records' client-controlled content. */
export function contentKey(n: Pick<Normalized, "data" | "tags" | "links" | "updated" | "deleted">): string {
  return JSON.stringify([n.deleted, n.updated, sortKeys(n.data), n.tags, n.links]);
}

function sortKeys(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === "object") {
    return Object.fromEntries(Object.entries(v as Data).sort(([a], [b]) => cmp(a, b)).map(([k, x]) => [k, sortKeys(x)]));
  }
  return v;
}

// --- wire types ----------------------------------------------------------------------------

export const RecordOut = z.object({
  id: z.string(),
  uid: z.string(),
  kind: z.enum(KINDS),
  created: z.string(),
  updated: z.string(),
  rev: z.number().int(),
  deleted: z.boolean(),
  device: z.string(),
  data: z.union([LedgerData, RowData, DocData, NoteData, z.object({})]),
  tags: z.array(z.string()),
  links: z.array(z.tuple([z.string(), z.enum(LINK_RELS)])),
}).openapi("Record");
export type RecordWire = z.infer<typeof RecordOut>;

export const RecordWithWiki = RecordOut.extend({ wiki: z.string() }).openapi("RecordWithWiki");

export const ErrorBody = z.object({
  error: z.object({ code: z.string(), message: z.string() }),
}).openapi("Error");

export const WikiKey = z.string().regex(WIKI_KEY_RE);
export const WikiOut = z.object({
  key: z.string(), name: z.string(), kind: z.enum(["global", "repo"]), origin: z.string(),
  created: z.string(), counts: z.record(z.string(), z.number().int()),
}).openapi("Wiki");
export const WikiPut = z.object({
  name: z.string().min(1).max(120),
  kind: z.enum(["global", "repo"]),
  origin: z.string().max(300).regex(/^[A-Za-z0-9._:/-]*$/, "origin is host/owner/repo, no credentials").default(""),
}).openapi("WikiPut");

// Sync ---------------------------------------------------------------------------------------

const BaseRev = z.number().int().min(0).nullable();
export const PutOp = z.object({ op: z.literal("put"), base_rev: BaseRev, record: z.unknown() }).openapi("PutOp");
export const DelOp = z.object({
  op: z.literal("del"), id: z.string().min(1).max(120), uid: z.string().max(64),
  deleted_at: z.string().max(40), base_rev: BaseRev,
}).openapi("DelOp");
export const SyncOp = z.discriminatedUnion("op", [PutOp, DelOp]);
export type SyncOpIn = z.infer<typeof SyncOp>;

export const SyncRequest = z.object({
  cursor: z.number().int().min(0).default(0),
  ops: z.array(SyncOp).max(LIMITS.opsPerSync).default([]),
  want: z.array(z.string().min(1).max(120)).max(LIMITS.wantIds).default([]),
  counters: z.record(z.string(), z.number().int().min(0).max(MAX_COUNTER)).default({}),
}).openapi("SyncRequest");

export const SYNC_STATUSES = ["accepted", "renamed", "noop", "rejected"] as const;
export const SyncResult = z.object({
  id: z.string(), status: z.enum(SYNC_STATUSES), rev: z.number().int(),
  new_id: z.string().optional(), error: z.string().optional().describe("Why a record was rejected, when it was invalid."),
}).openapi("SyncResult");
export const SyncResponse = z.object({
  cursor: z.number().int(), results: z.array(SyncResult), changes: z.array(RecordOut),
  counters: z.record(z.string(), z.number().int()), more: z.boolean(),
}).openapi("SyncResponse");
export type SyncResultOut = z.infer<typeof SyncResult>;
export type SyncResponseOut = z.infer<typeof SyncResponse>;

// REST record writes --------------------------------------------------------------------------

export const CREATE_TYPES = ["feature", "job", "task", "memory", "idea", "rule", "issue", "document", "note"] as const;
export type CreateType = (typeof CREATE_TYPES)[number];

export const CreateBody = z.object({
  type: z.enum(CREATE_TYPES),
  summary: z.string().max(5000).optional(),
  title: z.string().max(5000).optional(),
  body: z.string().max(LIMITS.recordBytes).optional(),
  tags: z.array(z.string().max(500)).max(500).optional(),
  alias: z.string().max(200).optional(),
  parent: z.string().max(120).optional(),
  verify: z.string().max(5000).optional(),
  path: z.string().max(200).optional().describe("Note path, for type note."),
  mime: z.string().max(100).optional(),
}).openapi("CreateRecord");

export const PatchBody = z.object({
  summary: z.string().max(5000).optional(),
  title: z.string().max(5000).optional(),
  body: z.string().max(LIMITS.recordBytes).optional(),
  tags: z.array(z.string().max(500)).max(500).optional(),
  alias: z.string().max(200).nullable().optional(),
  parent: z.string().max(120).nullable().optional(),
  verify: z.string().max(5000).nullable().optional(),
  git_ref: z.string().max(200).nullable().optional(),
  mime: z.string().max(100).optional(),
  base_rev: z.number().int().min(0).optional(),
}).openapi("PatchRecord");
export type PatchIn = z.infer<typeof PatchBody>;
export type CreateIn = z.infer<typeof CreateBody>;

export const CloseBody = z.object({
  reason: z.enum(["agent", "operator"]),
  git_ref: z.string().max(200).optional(),
}).openapi("CloseRecord");

export const SearchHit = z.object({
  wiki: z.string(), id: z.string(), type: z.string(), summary: z.string(), updated: z.string(),
}).openapi("SearchHit");
export const SearchOut = z.object({ results: z.array(SearchHit), mode: z.literal("bm25") }).openapi("SearchResults");

export const DeviceOut = z.object({
  id: z.string(), name: z.string(), scopes: z.array(z.enum(SCOPES)), wikis: z.array(z.string()).nullable(),
  source: z.enum(["token", "oauth"]), created: z.string(), last_seen: z.string(), revoked: z.string().nullable(),
}).openapi("Device");
