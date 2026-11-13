// Durable Object SQLite schema. One object (name "main") is the single writer, so a global
// `rev` counter plus per-wiki id counters need no merge engine. Additive migrations only.

export const SCHEMA_VERSION = 1;

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
  // Full-text index over live records only; rowid = records.seq. Columns: title/summary, body, tags, ids.
  `CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(summary, body, tags, ident, tokenize = 'unicode61')`,
  `CREATE TABLE IF NOT EXISTS devices (
     id TEXT PRIMARY KEY, name TEXT NOT NULL, scopes TEXT NOT NULL, wikis TEXT,
     token_hash TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
     created TEXT NOT NULL, last_seen TEXT NOT NULL DEFAULT '', revoked TEXT)`,
  `CREATE TABLE IF NOT EXISTS attempts (ip TEXT NOT NULL, ts INTEGER NOT NULL)`,
  `CREATE INDEX IF NOT EXISTS attempts_ip ON attempts (ip, ts)`,
];

export function migrate(sql: SqlStorage): void {
  for (const stmt of DDL) sql.exec(stmt);
  sql.exec(`INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)`, String(SCHEMA_VERSION));
  sql.exec(`INSERT OR IGNORE INTO meta (key, value) VALUES ('rev', '0')`);
}
