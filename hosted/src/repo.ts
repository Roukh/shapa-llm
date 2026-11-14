// Data access over the Durable Object's SQLite: wikis, counters, records and the FTS5 index.
// No authorization and no business rules here; those live in ledger.ts, sync.ts and store.ts.

import { ApiError } from "./errors.js";
import { COUNTER_KINDS, MAX_COUNTER, TYPE_NAMES, nowIso, type Data, type Kind, type Link, type Normalized, type RecordWire } from "./records.js";
import { reindexRelations, type IndexRow } from "./relations.js";
import { baseVisible } from "./tags.js";

import type { Reach } from "./devices.js";

export interface Stored {
  seq: number;
  wiki: string;
  id: string;
  uid: string;
  kind: Kind;
  created: string;
  updated: string;
  rev: number;
  deleted: boolean;
  device: string;
  alias: string;
  data: Data;
  tags: string[];
  links: Link[];
}

/** A record without its body: what a list of many records reads. */
export interface Brief {
  id: string;
  kind: Kind;
  rev: number;
  created: string;
  updated: string;
  tags: string[];
  alias: string;
  /** The summary (rows) or title (documents, ledger items). */
  text: string;
  status: string;
  head: string;
}

export interface WikiRow {
  key: string;
  name: string;
  kind: "global" | "repo";
  origin: string;
  created: string;
}

type Row = Record<string, SqlStorageValue>;

/** Bytes a stored row adds to a response: its JSON columns plus a flat allowance for the envelope. */
const SIZE_SQL = `(length(CAST(data AS BLOB)) + length(CAST(tags AS BLOB)) + length(CAST(links AS BLOB)) + 512)`;

/** `verify` was once a ledger field. It is a shell command and must never be served, whatever a row holds. */
function dropVerify(data: Data): Data {
  delete data["verify"];
  return data;
}

function fromRow(r: Row): Stored {
  return {
    seq: r["seq"] as number,
    wiki: r["wiki"] as string,
    id: r["id"] as string,
    uid: r["uid"] as string,
    kind: r["kind"] as Kind,
    created: r["created"] as string,
    updated: r["updated"] as string,
    rev: r["rev"] as number,
    deleted: r["deleted"] === 1,
    device: r["device"] as string,
    alias: r["alias"] as string,
    data: dropVerify(JSON.parse(r["data"] as string) as Data),
    tags: JSON.parse(r["tags"] as string) as string[],
    links: JSON.parse(r["links"] as string) as Link[],
  };
}

export function toWire(s: Stored): RecordWire {
  return {
    id: s.id, uid: s.uid, kind: s.kind, created: s.created, updated: s.updated, rev: s.rev,
    deleted: s.deleted, device: s.device, data: s.data as RecordWire["data"],
    tags: s.tags, links: s.links as RecordWire["links"],
  };
}

export class Repo {
  /** The same store with no reach filter: for integrity checks only (id and alias collisions, lessons), never for an answer. */
  readonly raw: Repo;
  constructor(readonly sql: SqlStorage, readonly reach: Reach = "full", raw?: Repo) {
    this.raw = reach === "full" ? this : (raw ?? new Repo(sql, "full"));
  }

  /** The view a device reads through. Every read of a record goes through the view, so a baseline device cannot reach a hidden one. */
  view(reach: Reach): Repo {
    if (reach === this.reach) return this;
    return reach === "full" ? this.raw : new Repo(this.sql, "baseline", this.raw);
  }

  get baseline(): boolean {
    return this.reach === "baseline";
  }

  /** ` AND <col> = 1` for a baseline view, else nothing: the reach filter in SQL. */
  vis(col = "base_vis"): string {
    return this.baseline ? ` AND ${col} = 1` : "";
  }

  // --- revisions and counters ---------------------------------------------------------------

  rev(): number {
    return Number(this.sql.exec(`SELECT value FROM meta WHERE key = 'rev'`).one()["value"]);
  }

  bumpRev(): number {
    return Number(
      this.sql.exec(
        `UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'rev' RETURNING value`,
      ).one()["value"],
    );
  }

  counters(wiki: string): Record<string, number> {
    const out: Record<string, number> = Object.fromEntries(COUNTER_KINDS.map((k) => [k, 1]));
    for (const r of this.sql.exec(`SELECT kind, next FROM counters WHERE wiki = ?`, wiki)) {
      out[r["kind"] as string] = r["next"] as number;
    }
    return out;
  }

  raiseCounter(wiki: string, kind: string, next: number): void {
    this.sql.exec(
      `INSERT INTO counters (wiki, kind, next) VALUES (?, ?, ?)
       ON CONFLICT (wiki, kind) DO UPDATE SET next = MAX(next, excluded.next)`,
      wiki, kind, next,
    );
  }

  /** Make the counter pass an id the server just stored (`M12` makes `M` at least 13). */
  noteId(wiki: string, id: string): void {
    const m = /^([FJTMRID])([1-9][0-9]*)$/.exec(id);
    if (m) this.raiseCounter(wiki, m[1]!, Number(m[2]) + 1);
  }

  /** The next free id of a kind (tombstones count as taken), advancing the counter. */
  allocId(wiki: string, kind: Kind): string {
    let n = this.counters(wiki)[kind] ?? 1;
    while (this.raw.get(wiki, `${kind}${n}`)) n += 1;
    if (n > MAX_COUNTER) throw new ApiError("conflict", `no ${kind} ids left in ${wiki}`);
    this.raiseCounter(wiki, kind, n + 1);
    return `${kind}${n}`;
  }

  // --- wikis ----------------------------------------------------------------------------------

  wiki(key: string): WikiRow | null {
    const r = this.sql.exec(`SELECT * FROM wikis WHERE key = ?`, key).toArray()[0];
    return r ? (r as unknown as WikiRow) : null;
  }

  wikis(): WikiRow[] {
    return this.sql.exec(`SELECT * FROM wikis ORDER BY key`).toArray() as unknown as WikiRow[];
  }

  insertWiki(w: Omit<WikiRow, "created">): void {
    this.sql.exec(`INSERT INTO wikis (key, name, kind, origin, created) VALUES (?, ?, ?, ?, ?)`, w.key, w.name, w.kind, w.origin, nowIso());
  }

  updateWiki(key: string, name: string, kind: string, origin: string): void {
    this.sql.exec(`UPDATE wikis SET name = ?, kind = ?, origin = ? WHERE key = ?`, name, kind, origin, key);
  }

  /** Live record counts by type name. */
  wikiCounts(key: string): Record<string, number> {
    const out: Record<string, number> = {};
    for (const r of this.sql.exec(`SELECT kind, COUNT(*) AS n FROM records WHERE wiki = ? AND deleted = 0${this.vis()} GROUP BY kind`, key)) {
      out[TYPE_NAMES[r["kind"] as Kind]] = r["n"] as number;
    }
    return out;
  }

  // --- records --------------------------------------------------------------------------------

  get(wiki: string, id: string): Stored | null {
    const r = this.sql.exec(`SELECT * FROM records WHERE wiki = ? AND id = ?${this.vis()}`, wiki, id).toArray()[0];
    return r ? fromRow(r) : null;
  }

  /** A live record by id, else by alias. */
  find(wiki: string, ref: string): Stored | null {
    const byId = this.get(wiki, ref);
    if (byId && !byId.deleted) return byId;
    // `alias != ''` is what lets SQLite use the partial index `records_alias` (it cannot know a bound parameter is not '').
    const r = this.sql.exec(`SELECT * FROM records WHERE wiki = ? AND alias = ? AND alias != '' AND deleted = 0${this.vis()}`, wiki, ref).toArray()[0];
    return r ? fromRow(r) : null;
  }

  /** The live record holding an alias, hidden ones included: an alias is unique per wiki whatever the reach. */
  aliasHolder(wiki: string, alias: string): string | null {
    if (!alias) return null;
    const r = this.sql.exec(`SELECT id FROM records WHERE wiki = ? AND alias = ? AND alias != '' AND deleted = 0`, wiki, alias).toArray()[0];
    return r ? (r["id"] as string) : null;
  }

  /** The record with this uid, wherever it is stored (tombstones included). Notes have no uid. */
  byUid(wiki: string, uid: string): Stored | null {
    if (!uid) return null;
    const r = this.sql.exec(`SELECT * FROM records WHERE wiki = ? AND uid = ?`, wiki, uid).toArray()[0];
    return r ? fromRow(r) : null;
  }

  /** Insert or replace a record body and give it the next global rev. A uid never changes. */
  save(wiki: string, n: Normalized, device: string, existing: Stored | null): Stored {
    if (existing && existing.uid !== n.uid) throw new Error("a record uid is immutable");
    const before = existing ? this.indexRow(existing.seq) : null;
    const rev = this.bumpRev();
    const vis = baseVisible(n.kind, n.tags) ? 1 : 0;
    const data = JSON.stringify(n.data);
    const tags = JSON.stringify(n.tags);
    const links = JSON.stringify(n.links);
    let seq: number;
    if (existing) {
      seq = existing.seq;
      this.sql.exec(
        `UPDATE records SET updated = ?, rev = ?, deleted = ?, device = ?, alias = ?, data = ?, tags = ?, links = ?, base_vis = ? WHERE seq = ?`,
        n.updated, rev, n.deleted ? 1 : 0, device, n.alias, data, tags, links, vis, seq,
      );
    } else {
      seq = this.sql.exec(
        `INSERT INTO records (wiki, id, uid, kind, created, updated, rev, deleted, device, alias, data, tags, links, base_vis)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING seq`,
        wiki, n.id, n.uid, n.kind, n.created, n.updated, rev, n.deleted ? 1 : 0, device, n.alias, data, tags, links, vis,
      ).one()["seq"] as number;
    }
    const stored: Stored = {
      seq, wiki, id: n.id, uid: n.uid, kind: n.kind, created: n.created, updated: n.updated, rev,
      deleted: n.deleted, device, alias: n.alias, data: n.data, tags: n.tags, links: n.links,
    };
    this.reindex(stored);
    reindexRelations(this.sql, before, {
      wiki, id: n.id, seq, kind: n.kind, rev, deleted: n.deleted, tags: n.tags, links: n.links, status: String(n.data["status"] ?? ""),
    });
    this.noteId(wiki, n.id);
    return stored;
  }

  /** What the relation index holds for a stored row, read from the row itself (never from a caller's copy). */
  private indexRow(seq: number): IndexRow | null {
    const r = this.sql.exec(`SELECT wiki, id, kind, rev, deleted, tags, links, json_extract(data, '$.status') AS status FROM records WHERE seq = ?`, seq).toArray()[0];
    if (!r) return null;
    return {
      wiki: r["wiki"] as string, id: r["id"] as string, seq, kind: r["kind"] as Kind, rev: r["rev"] as number, deleted: r["deleted"] === 1,
      tags: JSON.parse(r["tags"] as string) as string[], links: JSON.parse(r["links"] as string) as Link[], status: String(r["status"] ?? ""),
    };
  }

  /** Keep the FTS5 index to live records. */
  private reindex(s: Stored): void {
    this.sql.exec(`DELETE FROM fts WHERE rowid = ?`, s.seq);
    if (s.deleted) return;
    const d = s.data;
    const summary = String(d["summary"] ?? d["title"] ?? "");
    this.sql.exec(
      `INSERT INTO fts (rowid, summary, body, tags, ident) VALUES (?, ?, ?, ?, ?)`,
      s.seq, summary, String(d["body"] ?? ""), s.tags.join(" "), `${s.id} ${s.alias}`.trim(),
    );
  }

  /** Records after `cursor`, oldest first: at most `maxRecords` and about `maxBytes` of JSON, but always
   *  at least one when any exists. Sizes are read first so only the chosen rows are ever loaded
   *  (a Durable Object has 128 MB of memory and a record may be 1 MB). */
  changesPage(wiki: string, cursor: number, maxRecords: number, maxBytes: number): { rows: Stored[]; more: boolean } {
    const sizes = this.sql
      .exec(`SELECT rev, ${SIZE_SQL} AS size FROM records WHERE wiki = ? AND rev > ? ORDER BY rev LIMIT ?`, wiki, cursor, maxRecords + 1)
      .toArray();
    let count = 0;
    let bytes = 0;
    for (const r of sizes) {
      if (count >= maxRecords || (count > 0 && bytes + (r["size"] as number) > maxBytes)) break;
      bytes += r["size"] as number;
      count += 1;
    }
    if (count === 0) return { rows: [], more: false };
    const last = sizes[count - 1]!["rev"] as number;
    const rows = this.sql
      .exec(`SELECT * FROM records WHERE wiki = ? AND rev > ? AND rev <= ? ORDER BY rev`, wiki, cursor, last)
      .toArray().map(fromRow);
    return { rows, more: count < sizes.length };
  }

  /** The size a record adds to a sync response, or null when it does not exist. */
  recordSize(wiki: string, id: string): number | null {
    const r = this.sql.exec(`SELECT ${SIZE_SQL} AS size FROM records WHERE wiki = ? AND id = ?`, wiki, id).toArray()[0];
    return r ? (r["size"] as number) : null;
  }

  list(wiki: string, f: { kind?: Kind; status?: string; tag?: string; after: number; limit: number }): Stored[] {
    // With a tag the listing starts from the relation index (wiki, tag, id): its cost is the tag's records, never
    // every record's tag list. The index row carries the record's `rev`, so the cursor is the same either way.
    const byTag = f.tag !== undefined && f.tag !== "";
    const p = byTag ? "r." : "";
    const where = byTag ? [`t.wiki = ?`, `t.tag = ?`, `t.rev > ?`, `r.deleted = 0`] : [`wiki = ?`, `deleted = 0`, `rev > ?`];
    const args: SqlStorageValue[] = byTag ? [wiki, f.tag!, f.after] : [wiki, f.after];
    if (this.baseline) where.push(`${p}base_vis = 1`);
    if (f.kind) { where.push(`${p}kind = ?`); args.push(f.kind); }
    if (f.status) { where.push(`json_extract(${p}data, '$.status') = ?`); args.push(f.status); }
    args.push(f.limit);
    const from = byTag ? `record_tags t JOIN records r ON r.wiki = t.wiki AND r.id = t.id` : `records`;
    return this.sql
      .exec(`SELECT ${p}* FROM ${from} WHERE ${where.join(" AND ")} ORDER BY ${byTag ? "t.rev" : "rev"} LIMIT ?`, ...args)
      .toArray().map(fromRow);
  }

  live(wiki: string, kinds: readonly Kind[]): Stored[] {
    const marks = kinds.map(() => "?").join(",");
    return this.sql
      .exec(`SELECT * FROM records WHERE wiki = ? AND deleted = 0${this.vis()} AND kind IN (${marks}) ORDER BY kind, CAST(SUBSTR(id, 2) AS INTEGER), id`, wiki, ...kinds)
      .toArray().map(fromRow);
  }

  children(wiki: string, parent: string): Stored[] {
    return this.sql
      .exec(`SELECT * FROM records WHERE wiki = ? AND deleted = 0${this.vis()} AND kind IN ('J','T') AND json_extract(data, '$.parent') = ?`, wiki, parent)
      .toArray().map(fromRow);
  }

  /** A live record's headline without its body (a body may be 1 MB): lists of many records read this, never `get`.
   *  `head` is the first `headChars` characters of the body. Honors the reach filter. */
  brief(wiki: string, id: string, headChars = 0): Brief | null {
    const r = this.sql.exec(
      `SELECT id, kind, rev, created, updated, tags, alias,
              COALESCE(json_extract(data, '$.summary'), json_extract(data, '$.title'), '') AS text,
              COALESCE(json_extract(data, '$.status'), '') AS status,
              CASE WHEN ? > 0 THEN substr(COALESCE(json_extract(data, '$.body'), ''), 1, ?) ELSE '' END AS head
         FROM records WHERE wiki = ? AND id = ? AND deleted = 0${this.vis()}`,
      headChars, headChars, wiki, id,
    ).toArray()[0];
    if (!r) return null;
    return {
      id: r["id"] as string, kind: r["kind"] as Kind, rev: r["rev"] as number, created: r["created"] as string, updated: r["updated"] as string,
      tags: JSON.parse(r["tags"] as string) as string[], alias: r["alias"] as string, text: String(r["text"] ?? ""), status: String(r["status"] ?? ""), head: String(r["head"] ?? ""),
    };
  }

  /** `brief` by id, else by alias. */
  briefRef(wiki: string, ref: string, headChars = 0): Brief | null {
    const byId = this.brief(wiki, ref, headChars);
    if (byId) return byId;
    const a = this.sql.exec(`SELECT id FROM records WHERE wiki = ? AND alias = ? AND alias != '' AND deleted = 0${this.vis()}`, wiki, ref).toArray()[0];
    return a ? this.brief(wiki, a["id"] as string, headChars) : null;
  }

  /** Open features of a wiki, oldest first, at most `limit`. */
  openFeatures(wiki: string, limit: number): Stored[] {
    return this.sql
      .exec(`SELECT * FROM records WHERE wiki = ? AND kind = 'F' AND deleted = 0${this.vis()} AND json_extract(data, '$.status') != 'closed' ORDER BY seq LIMIT ?`, wiki, limit)
      .toArray().map(fromRow);
  }

  /** An open standalone task of this owner tag with exactly this title and body (the REST/MCP dedupe of a task). */
  identicalOpenTask(wiki: string, ownerTag: string, title: string, body: string): Stored | null {
    const r = this.sql.exec(
      `SELECT r.* FROM record_tags t JOIN records r ON r.wiki = t.wiki AND r.id = t.id
        WHERE t.wiki = ? AND t.tag = ? AND t.kind = 'T' AND t.open = 1${this.vis("t.vis")}
          AND json_extract(r.data, '$.title') = ? AND json_extract(r.data, '$.body') = ? AND json_extract(r.data, '$.parent') IS NULL LIMIT 1`,
      wiki, ownerTag, title, body,
    ).toArray()[0];
    return r ? fromRow(r) : null;
  }

  /** Live rows of a kind whose summary and body are exactly these (the REST/MCP dedupe). */
  identicalRow(wiki: string, kind: Kind, summary: string, body: string): Stored | null {
    const r = this.sql.exec(
      `SELECT * FROM records WHERE wiki = ? AND kind = ? AND deleted = 0${this.vis()}
         AND json_extract(data, '$.summary') = ? AND json_extract(data, '$.body') = ? LIMIT 1`,
      wiki, kind, summary, body,
    ).toArray()[0];
    return r ? fromRow(r) : null;
  }

  /** Live documents without their bodies (a body may be 1 MB): id, uid, title, tags. */
  docsLight(wiki: string): { id: string; uid: string; title: string; tags: string[] }[] {
    return this.sql
      .exec(`SELECT id, uid, json_extract(data, '$.title') AS title, tags FROM records WHERE wiki = ? AND kind = 'D' AND deleted = 0${this.vis()} ORDER BY seq`, wiki)
      .toArray()
      .map((r) => ({ id: r["id"] as string, uid: r["uid"] as string, title: String(r["title"] ?? ""), tags: JSON.parse(r["tags"] as string) as string[] }));
  }

  /** Whether a lesson from row `rowId` is already filed as a version of skill `name` (no body is loaded). */
  lessonFiled(wiki: string, name: string, rowId: string): boolean {
    return this.sql.exec(
      `SELECT 1 FROM records WHERE wiki = ? AND kind = 'D' AND deleted = 0 AND json_extract(data, '$.source') = ?
         AND EXISTS (SELECT 1 FROM json_each(records.tags) WHERE value = ?) LIMIT 1`,
      wiki, `lesson:${rowId}`, `skill:${name}`,
    ).toArray().length > 0;
  }

  /** Cross-wiki lookup by id or alias (live records only). */
  across(ref: string, wikis: string[] | null, only?: string): Stored[] {
    // Two lookups, each on its own index (the id, the alias): one `OR` query can fall back to scanning every record.
    const byId = this.sql.exec(`SELECT * FROM records WHERE id = ? AND deleted = 0${this.vis()}`, ref).toArray();
    const byAlias = this.sql.exec(`SELECT * FROM records WHERE alias = ? AND alias != '' AND deleted = 0${this.vis()}`, ref).toArray();
    const seen = new Set<number>();
    const rows = [...byId, ...byAlias]
      .map(fromRow)
      .filter((s) => !seen.has(s.seq) && !!seen.add(s.seq))
      .sort((a, b) => (a.wiki < b.wiki ? -1 : a.wiki > b.wiki ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    return rows.filter((s) => (only ? s.wiki === only : true) && (wikis === null || wikis.includes(s.wiki)));
  }
}

/** A stored record as the normalized body a write starts from. */
export function toNormalized(s: Stored): Normalized {
  return { id: s.id, uid: s.uid, kind: s.kind, created: s.created, updated: s.updated, deleted: s.deleted, alias: s.alias, data: s.data, tags: s.tags, links: s.links };
}

/** `updated` for a server-side write: now, never earlier than the record already says. */
export function stampAfter(prev?: string): string {
  const now = nowIso();
  return prev && prev > now ? prev : now;
}
