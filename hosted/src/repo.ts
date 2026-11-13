// Data access over the Durable Object's SQLite: wikis, counters, records and the FTS5 index.
// No authorization and no business rules here; those live in ledger.ts, sync.ts and store.ts.

import { ApiError } from "./errors.js";
import { COUNTER_KINDS, MAX_COUNTER, TYPE_NAMES, nowIso, type Data, type Kind, type Link, type Normalized, type RecordWire } from "./records.js";

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

export interface WikiRow {
  key: string;
  name: string;
  kind: "global" | "repo";
  origin: string;
  created: string;
}

type Row = Record<string, SqlStorageValue>;

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
    data: JSON.parse(r["data"] as string) as Data,
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
  constructor(readonly sql: SqlStorage) {}

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
    while (this.get(wiki, `${kind}${n}`)) n += 1;
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
    for (const r of this.sql.exec(`SELECT kind, COUNT(*) AS n FROM records WHERE wiki = ? AND deleted = 0 GROUP BY kind`, key)) {
      out[TYPE_NAMES[r["kind"] as Kind]] = r["n"] as number;
    }
    return out;
  }

  // --- records --------------------------------------------------------------------------------

  get(wiki: string, id: string): Stored | null {
    const r = this.sql.exec(`SELECT * FROM records WHERE wiki = ? AND id = ?`, wiki, id).toArray()[0];
    return r ? fromRow(r) : null;
  }

  /** A live record by id, else by alias. */
  find(wiki: string, ref: string): Stored | null {
    const byId = this.get(wiki, ref);
    if (byId && !byId.deleted) return byId;
    const r = this.sql.exec(`SELECT * FROM records WHERE wiki = ? AND alias = ? AND deleted = 0`, wiki, ref).toArray()[0];
    return r ? fromRow(r) : null;
  }

  aliasHolder(wiki: string, alias: string): string | null {
    if (!alias) return null;
    const r = this.sql.exec(`SELECT id FROM records WHERE wiki = ? AND alias = ? AND deleted = 0`, wiki, alias).toArray()[0];
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
    const rev = this.bumpRev();
    const data = JSON.stringify(n.data);
    const tags = JSON.stringify(n.tags);
    const links = JSON.stringify(n.links);
    let seq: number;
    if (existing) {
      seq = existing.seq;
      this.sql.exec(
        `UPDATE records SET updated = ?, rev = ?, deleted = ?, device = ?, alias = ?, data = ?, tags = ?, links = ? WHERE seq = ?`,
        n.updated, rev, n.deleted ? 1 : 0, device, n.alias, data, tags, links, seq,
      );
    } else {
      seq = this.sql.exec(
        `INSERT INTO records (wiki, id, uid, kind, created, updated, rev, deleted, device, alias, data, tags, links)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING seq`,
        wiki, n.id, n.uid, n.kind, n.created, n.updated, rev, n.deleted ? 1 : 0, device, n.alias, data, tags, links,
      ).one()["seq"] as number;
    }
    const stored: Stored = {
      seq, wiki, id: n.id, uid: n.uid, kind: n.kind, created: n.created, updated: n.updated, rev,
      deleted: n.deleted, device, alias: n.alias, data: n.data, tags: n.tags, links: n.links,
    };
    this.reindex(stored);
    this.noteId(wiki, n.id);
    return stored;
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

  changes(wiki: string, cursor: number, limit: number): Stored[] {
    return this.sql
      .exec(`SELECT * FROM records WHERE wiki = ? AND rev > ? ORDER BY rev LIMIT ?`, wiki, cursor, limit)
      .toArray().map(fromRow);
  }

  list(wiki: string, f: { kind?: Kind; status?: string; tag?: string; after: number; limit: number }): Stored[] {
    const where = [`wiki = ?`, `deleted = 0`, `rev > ?`];
    const args: SqlStorageValue[] = [wiki, f.after];
    if (f.kind) { where.push(`kind = ?`); args.push(f.kind); }
    if (f.status) { where.push(`json_extract(data, '$.status') = ?`); args.push(f.status); }
    if (f.tag) { where.push(`EXISTS (SELECT 1 FROM json_each(records.tags) WHERE value = ?)`); args.push(f.tag); }
    args.push(f.limit);
    return this.sql
      .exec(`SELECT * FROM records WHERE ${where.join(" AND ")} ORDER BY rev LIMIT ?`, ...args)
      .toArray().map(fromRow);
  }

  live(wiki: string, kinds: readonly Kind[]): Stored[] {
    const marks = kinds.map(() => "?").join(",");
    return this.sql
      .exec(`SELECT * FROM records WHERE wiki = ? AND deleted = 0 AND kind IN (${marks}) ORDER BY kind, CAST(SUBSTR(id, 2) AS INTEGER), id`, wiki, ...kinds)
      .toArray().map(fromRow);
  }

  children(wiki: string, parent: string): Stored[] {
    return this.sql
      .exec(`SELECT * FROM records WHERE wiki = ? AND deleted = 0 AND kind IN ('J','T') AND json_extract(data, '$.parent') = ?`, wiki, parent)
      .toArray().map(fromRow);
  }

  /** Live rows of a kind whose summary and body are exactly these (the REST/MCP dedupe). */
  identicalRow(wiki: string, kind: Kind, summary: string, body: string): Stored | null {
    const r = this.sql.exec(
      `SELECT * FROM records WHERE wiki = ? AND kind = ? AND deleted = 0
         AND json_extract(data, '$.summary') = ? AND json_extract(data, '$.body') = ? LIMIT 1`,
      wiki, kind, summary, body,
    ).toArray()[0];
    return r ? fromRow(r) : null;
  }

  /** Cross-wiki lookup by id or alias (live records only). */
  across(ref: string, wikis: string[] | null, only?: string): Stored[] {
    const rows = this.sql
      .exec(`SELECT * FROM records WHERE deleted = 0 AND (id = ? OR alias = ?) ORDER BY wiki, id`, ref, ref)
      .toArray().map(fromRow);
    return rows.filter((s) => (only ? s.wiki === only : true) && (wikis === null || wikis.includes(s.wiki)));
  }
}
