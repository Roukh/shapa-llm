// Durable Object SQLite schema. One object (name "main") is the single writer, so a global
// `rev` counter plus per-wiki id counters need no merge engine. Additive migrations only.

import { rebuildRelations, quarantineLegacyStage } from "./relations.js";

export const SCHEMA_VERSION = 1;
/** Meta key: set once the relation index (record_tags, record_links, tag_counts) and the reach column are built. */
export const RELATIONS_KEY = "relations_v1";

const DDL: string[] = [
  `CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS wikis (
     key TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
     origin TEXT NOT NULL DEFAULT '', created TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS counters (
     wiki TEXT NOT NULL, kind TEXT NOT NULL, next INTEGER NOT NULL,
     PRIMARY KEY (wiki, kind)) WITHOUT ROWID`,
  `CREATE TABLE IF NOT EXISTS records (
     seq INTEGER PRIMARY KEY AUTOINCREMENT,
     wiki TEXT NOT NULL, id TEXT NOT NULL, uid TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
     created TEXT NOT NULL, updated TEXT NOT NULL,
     rev INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
     device TEXT NOT NULL DEFAULT '', alias TEXT NOT NULL DEFAULT '',
     data TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', links TEXT NOT NULL DEFAULT '[]',
     UNIQUE (wiki, id))`,
  `CREATE INDEX IF NOT EXISTS records_rev ON records (wiki, rev)`,
  `CREATE INDEX IF NOT EXISTS records_kind ON records (wiki, kind, deleted)`,
  // A uid is the identity of a record and is stored under exactly one id per wiki (tombstones keep theirs).
  `CREATE UNIQUE INDEX IF NOT EXISTS records_uid ON records (wiki, uid) WHERE uid != ''`,
  `CREATE UNIQUE INDEX IF NOT EXISTS records_alias ON records (wiki, alias) WHERE alias != '' AND deleted = 0`,
  `CREATE INDEX IF NOT EXISTS records_alias_any ON records (alias) WHERE alias != ''`,
  // `get` by id across wikis: without it the lookup scans every record.
  `CREATE INDEX IF NOT EXISTS records_id_any ON records (id)`,
  // Full-text index over live records only; rowid = records.seq. Columns: title/summary, body, tags, ids.
  `CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(summary, body, tags, ident, tokenize = 'unicode61')`,
  `CREATE TABLE IF NOT EXISTS devices (
     id TEXT PRIMARY KEY, name TEXT NOT NULL, scopes TEXT NOT NULL, wikis TEXT,
     token_hash TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
     created TEXT NOT NULL, last_seen TEXT NOT NULL DEFAULT '', revoked TEXT)`,
  `CREATE TABLE IF NOT EXISTS attempts (ip TEXT NOT NULL, ts INTEGER NOT NULL)`,
  `CREATE INDEX IF NOT EXISTS attempts_ip ON attempts (ip, ts)`,
  // The relation index: tags and links are the relations. Kept in step by Repo.save (every write path)
  // and backfilled once. `kind` is the record's bucket (tags.ts), `open` an open task, `vis` a record a
  // baseline device may see, `rank` the rank:N tag (1000 = none), `seq` creation order, `rev` last change.
  `CREATE TABLE IF NOT EXISTS record_tags (
     wiki TEXT NOT NULL, tag TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
     open INTEGER NOT NULL DEFAULT 0, vis INTEGER NOT NULL DEFAULT 1, rank INTEGER NOT NULL DEFAULT 1000,
     seq INTEGER NOT NULL, rev INTEGER NOT NULL,
     PRIMARY KEY (wiki, tag, id)) WITHOUT ROWID`,
  `CREATE INDEX IF NOT EXISTS record_tags_tag ON record_tags (tag, wiki)`,
  `CREATE INDEX IF NOT EXISTS record_tags_rev ON record_tags (wiki, tag, kind, open, rev)`,
  `CREATE INDEX IF NOT EXISTS record_tags_seq ON record_tags (tag, kind, open, rank, seq)`,
  // A tag listing pages by revision: this order lets it stop after one page instead of sorting every record of the tag.
  `CREATE INDEX IF NOT EXISTS record_tags_tagrev ON record_tags (wiki, tag, rev)`,
  `CREATE TABLE IF NOT EXISTS record_links (
     wiki TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL,
     PRIMARY KEY (wiki, src, dst, rel)) WITHOUT ROWID`,
  `CREATE INDEX IF NOT EXISTS record_links_dst ON record_links (wiki, dst)`,
  // Live records per (wiki, tag, bucket, vis): the tag index and the project and task counts read this, not the records.
  `CREATE TABLE IF NOT EXISTS tag_counts (
     wiki TEXT NOT NULL, tag TEXT NOT NULL, bucket TEXT NOT NULL, vis INTEGER NOT NULL, n INTEGER NOT NULL,
     PRIMARY KEY (wiki, tag, bucket, vis)) WITHOUT ROWID`,
];

/** Additive columns: SQLite has no ADD COLUMN IF NOT EXISTS, so a repeat is the expected error. */
const COLUMNS: string[] = [
  `ALTER TABLE records ADD COLUMN base_vis INTEGER NOT NULL DEFAULT 1`,
  `ALTER TABLE devices ADD COLUMN reach TEXT NOT NULL DEFAULT ''`,
];

export function migrate(sql: SqlStorage): void {
  for (const stmt of DDL) sql.exec(stmt);
  for (const stmt of COLUMNS) {
    try {
      sql.exec(stmt);
    } catch (e) {
      if (!/duplicate column/i.test(String((e as Error)?.message ?? e))) throw e;
    }
  }
  sql.exec(`INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)`, String(SCHEMA_VERSION));
  sql.exec(`INSERT OR IGNORE INTO meta (key, value) VALUES ('rev', '0')`);
  backfillOnce(sql);
}

/** The one-time step for rows stored before the relation index: a stage:0 tag stored before stages existed was
 *  never the operator's, so it becomes stage-proposed:0 first; then the index and the reach column are built. */
export function backfillOnce(sql: SqlStorage): void {
  const done = sql.exec(`SELECT 1 FROM meta WHERE key = ?`, RELATIONS_KEY).toArray().length > 0;
  if (done) return;
  quarantineLegacyStage(sql);
  rebuildRelations(sql);
  sql.exec(`INSERT OR IGNORE INTO meta (key, value) VALUES (?, '1')`, RELATIONS_KEY);
}
