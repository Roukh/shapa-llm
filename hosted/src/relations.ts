// The relation index: `record_tags(wiki, tag, id)`, `record_links(wiki, src, dst, rel)` and the
// per-tag counts, kept in step by Repo.save (so REST, MCP, sync, tombstones and the engine's own
// writes all reach it) and rebuilt once for rows stored before it. Every read here takes `baseline`:
// a baseline device sees only the rows its reach allows (`vis = 1`), and sees nothing else, not even
// in a count. Reads cost the size of their answer, not of the store: they walk an index.

import { cmp, type Kind, type Link } from "./records.js";
import {
  BASELINE_TAG, BUCKET_NAMES, PROPOSED_TAG, RANK_NONE, baseVisible, bucketOf, rankOf, systemTagSql,
  type Bucket,
} from "./tags.js";

/** What the index needs to know of a record; `status` is the ledger status ("" for the rest). */
export interface IndexRow {
  wiki: string;
  id: string;
  seq: number;
  kind: Kind;
  rev: number;
  deleted: boolean;
  tags: string[];
  links: Link[];
  status: string;
}

const isOpenTask = (r: Pick<IndexRow, "kind" | "status">) => r.kind === "T" && r.status !== "closed";

/** The (tag, bucket, vis) triples a live record is counted under: its bucket, plus "O" while an open task. */
function counted(r: IndexRow | null): Map<string, [string, string, number]> {
  const out = new Map<string, [string, string, number]>();
  if (!r || r.deleted) return out;
  const vis = baseVisible(r.kind, r.tags) ? 1 : 0;
  const buckets: string[] = [bucketOf(r.kind, r.tags)];
  if (isOpenTask(r)) buckets.push("O");
  for (const tag of r.tags) for (const b of buckets) out.set(`${tag}\u0000${b}\u0000${vis}`, [tag, b, vis]);
  return out;
}

function bump(sql: SqlStorage, wiki: string, [tag, bucket, vis]: [string, string, number], by: 1 | -1): void {
  if (by === 1) {
    sql.exec(
      `INSERT INTO tag_counts (wiki, tag, bucket, vis, n) VALUES (?, ?, ?, ?, 1)
       ON CONFLICT (wiki, tag, bucket, vis) DO UPDATE SET n = n + 1`,
      wiki, tag, bucket, vis,
    );
    return;
  }
  sql.exec(`UPDATE tag_counts SET n = n - 1 WHERE wiki = ? AND tag = ? AND bucket = ? AND vis = ?`, wiki, tag, bucket, vis);
  sql.exec(`DELETE FROM tag_counts WHERE wiki = ? AND tag = ? AND bucket = ? AND vis = ? AND n <= 0`, wiki, tag, bucket, vis);
}

/** Bring the index from `old` (what was stored, null for a new record) to `next`. */
export function reindexRelations(sql: SqlStorage, old: IndexRow | null, next: IndexRow): void {
  const { wiki, id } = next;
  const oldTags = old && !old.deleted ? old.tags : [];
  const newTags = next.deleted ? [] : next.tags;
  const keep = new Set(newTags);
  for (const t of oldTags) {
    if (!keep.has(t)) sql.exec(`DELETE FROM record_tags WHERE wiki = ? AND tag = ? AND id = ?`, wiki, t, id);
  }
  if (!next.deleted) {
    const bucket = bucketOf(next.kind, next.tags);
    const open = isOpenTask(next) ? 1 : 0;
    const vis = baseVisible(next.kind, next.tags) ? 1 : 0;
    const rank = rankOf(next.tags) ?? RANK_NONE;
    for (const t of newTags) {
      sql.exec(
        `INSERT INTO record_tags (wiki, tag, id, kind, open, vis, rank, seq, rev) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT (wiki, tag, id) DO UPDATE SET kind = excluded.kind, open = excluded.open, vis = excluded.vis,
           rank = excluded.rank, seq = excluded.seq, rev = excluded.rev`,
        wiki, t, id, bucket, open, vis, rank, next.seq, next.rev,
      );
    }
  }
  const was = counted(old);
  const now = counted(next);
  for (const [k, v] of was) if (!now.has(k)) bump(sql, wiki, v, -1);
  for (const [k, v] of now) if (!was.has(k)) bump(sql, wiki, v, 1);

  const sameLinks = old && !old.deleted && !next.deleted && JSON.stringify(old.links) === JSON.stringify(next.links);
  if (!sameLinks) {
    sql.exec(`DELETE FROM record_links WHERE wiki = ? AND src = ?`, wiki, id);
    if (!next.deleted) {
      for (const [dst, rel] of next.links) sql.exec(`INSERT OR IGNORE INTO record_links (wiki, src, dst, rel) VALUES (?, ?, ?, ?)`, wiki, id, dst, rel);
    }
  }
}

// --- build and backfill -------------------------------------------------------------------------

/** Rebuild the whole index from the records (idempotent: it clears it first). Also sets `base_vis`. */
export function rebuildRelations(sql: SqlStorage): void {
  sql.exec(`DELETE FROM record_tags`);
  sql.exec(`DELETE FROM record_links`);
  sql.exec(`DELETE FROM tag_counts`);
  let after = 0;
  for (;;) {
    const rows = sql
      .exec(
        `SELECT seq, wiki, id, kind, rev, deleted, tags, links, json_extract(data, '$.status') AS status
           FROM records WHERE seq > ? ORDER BY seq LIMIT 500`, after,
      )
      .toArray();
    if (rows.length === 0) break;
    for (const r of rows) {
      const row: IndexRow = {
        wiki: r["wiki"] as string, id: r["id"] as string, seq: r["seq"] as number, kind: r["kind"] as Kind, rev: r["rev"] as number,
        deleted: r["deleted"] === 1, tags: JSON.parse(r["tags"] as string) as string[], links: JSON.parse(r["links"] as string) as Link[],
        status: String(r["status"] ?? ""),
      };
      sql.exec(`UPDATE records SET base_vis = ? WHERE seq = ?`, baseVisible(row.kind, row.tags) ? 1 : 0, row.seq);
      reindexRelations(sql, null, row);
      after = row.seq;
    }
  }
}

/** The one stage marker a tag list may hold after a relabel: `stage:0` was never the operator's when stored before
 *  stages existed, so it becomes `stage-proposed:0` in the global wiki and is dropped in a repo wiki (the baseline
 *  is the global wiki's). A record that already carries a `stage:1`-`stage:9` tag keeps just that one marker, and
 *  one that carries a proposal keeps it only in the global wiki: never `stage:N` and `stage-proposed:0` together. */
export function relabelStage(tags: readonly string[], inGlobal: boolean): string[] {
  const rest = tags.filter((t) => t !== BASELINE_TAG && t !== PROPOSED_TAG);
  const staged = rest.some((t) => /^stage:[1-9]$/.test(t));
  const hadZero = tags.includes(BASELINE_TAG) || tags.includes(PROPOSED_TAG);
  if (hadZero && inGlobal && !staged) rest.push(PROPOSED_TAG);
  return rest;
}

/** A stage:0 tag already stored when stages arrived was never the operator's: it is relabelled (`relabelStage`). */
export function quarantineLegacyStage(sql: SqlStorage): void {
  const rows = sql
    .exec(`SELECT seq, wiki, tags FROM records WHERE EXISTS (SELECT 1 FROM json_each(records.tags) WHERE value = ?)`, BASELINE_TAG)
    .toArray();
  for (const r of rows) {
    const kind = sql.exec(`SELECT kind FROM wikis WHERE key = ?`, r["wiki"] as string).toArray()[0]?.["kind"];
    const tags = relabelStage(JSON.parse(r["tags"] as string) as string[], kind === "global");
    const rev = Number(sql.exec(`UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'rev' RETURNING value`).one()["value"]);
    sql.exec(`UPDATE records SET tags = ?, rev = ? WHERE seq = ?`, JSON.stringify(tags.sort(cmp)), rev, r["seq"] as number);
    // the full-text index holds the tags too (a live record only): a stale stage:0 there would still match a tag search
    sql.exec(`UPDATE fts SET tags = ? WHERE rowid = ?`, tags.join(" "), r["seq"] as number);
  }
}

// --- reads --------------------------------------------------------------------------------------

const marks = (n: number) => Array(n).fill("?").join(",");
const visCol = (baseline: boolean, col = "vis") => (baseline ? ` AND ${col} = 1` : "");

export interface TagRow {
  tag: string;
  total: number;
  counts: Record<string, number>;
}

/** A device's wiki list in slices small enough for one query (a Durable Object binds at most 100 parameters). */
export function slices<T>(items: T[], size = 80): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

/** The tag index of some wikis: tags by record count (descending, then name), each with its counts by type. The
 *  work is the number of distinct tags, never the number of records. */
export function tagIndex(sql: SqlStorage, baseline: boolean, wikis: string[], o: { limit: number; system: boolean; prefix?: string }): TagRow[] {
  const by = new Map<string, { total: number; counts: Record<string, number> }>();
  for (const part of slices(wikis)) {
    const where = [`wiki IN (${marks(part.length)})`, `bucket != 'O'`];
    const args: SqlStorageValue[] = [...part];
    if (baseline) where.push(`vis = 1`);
    if (!o.system) where.push(`NOT ${systemTagSql()}`);
    if (o.prefix) { where.push(`substr(tag, 1, ?) = ?`); args.push(o.prefix.length, o.prefix); }
    for (const r of sql.exec(`SELECT tag, bucket, SUM(n) AS n FROM tag_counts WHERE ${where.join(" AND ")} GROUP BY tag, bucket`, ...args)) {
      const e = by.get(r["tag"] as string) ?? { total: 0, counts: {} };
      const name = bucketName(r["bucket"] as Bucket);
      e.counts[name] = (e.counts[name] ?? 0) + (r["n"] as number);
      e.total += r["n"] as number;
      by.set(r["tag"] as string, e);
    }
  }
  return [...by.entries()]
    .map(([tag, e]) => ({ tag, total: e.total, counts: e.counts }))
    .sort((a, b) => b.total - a.total || cmp(a.tag, b.tag))
    .slice(0, o.limit);
}

const bucketName = (b: Bucket): string => BUCKET_NAMES[b] ?? "document";

/** How many live records carry `tag` in a wiki, by bucket (O counts open tasks). */
export function tagBuckets(sql: SqlStorage, baseline: boolean, wiki: string, tag: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of sql.exec(`SELECT bucket, SUM(n) AS n FROM tag_counts WHERE wiki = ? AND tag = ?${visCol(baseline)} GROUP BY bucket`, wiki, tag)) {
    out[r["bucket"] as string] = r["n"] as number;
  }
  return out;
}

export interface ProjectKey {
  key: string;
  open: number;
}

/** The project keys of a wiki (any record carries `project:KEY`), with their open task counts. */
export function projectKeys(sql: SqlStorage, baseline: boolean, wiki: string): ProjectKey[] {
  const out = new Map<string, number>();
  for (const r of sql.exec(
    `SELECT tag, bucket, SUM(n) AS n FROM tag_counts WHERE wiki = ? AND tag >= 'project:' AND tag < 'project;'${visCol(baseline)} GROUP BY tag, bucket`,
    wiki,
  )) {
    const key = (r["tag"] as string).slice("project:".length);
    out.set(key, (out.get(key) ?? 0) + (r["bucket"] === "O" ? (r["n"] as number) : 0));
  }
  return [...out.entries()].map(([key, open]) => ({ key, open }));
}

export interface TaggedRef {
  id: string;
  rev: number;
}

/** Ids carrying a tag in a wiki and a bucket, newest change first, at most `limit` (open tasks first). */
export function taggedIds(sql: SqlStorage, baseline: boolean, wiki: string, tag: string, bucket: string, limit: number): string[] {
  return sql
    .exec(`SELECT id FROM record_tags WHERE wiki = ? AND tag = ? AND kind = ?${visCol(baseline)} ORDER BY open DESC, rev DESC LIMIT ?`, wiki, tag, bucket, limit)
    .toArray().map((r) => r["id"] as string);
}

/** Candidate neighbours of a record by shared tag: at most `cap` per tag (the index walks, never scans). */
export function tagNeighbours(sql: SqlStorage, baseline: boolean, wiki: string, tag: string, exceptId: string, cap: number): TaggedRef[] {
  return sql
    .exec(`SELECT id, rev FROM record_tags WHERE wiki = ? AND tag = ? AND id != ?${visCol(baseline)} LIMIT ?`, wiki, tag, exceptId, cap)
    .toArray().map((r) => ({ id: r["id"] as string, rev: r["rev"] as number }));
}

export function linkedIn(sql: SqlStorage, baseline: boolean, wiki: string, dsts: string[], cap: number): string[] {
  if (dsts.length === 0) return [];
  const vis = baseline ? ` AND EXISTS (SELECT 1 FROM records r WHERE r.wiki = l.wiki AND r.id = l.src AND r.base_vis = 1)` : "";
  return sql
    .exec(`SELECT DISTINCT l.src AS src FROM record_links l WHERE l.wiki = ? AND l.dst IN (${marks(dsts.length)})${vis} LIMIT ?`, wiki, ...dsts, cap)
    .toArray().map((r) => r["src"] as string);
}

// --- tasks --------------------------------------------------------------------------------------

export interface TaskQuery {
  wikis: string[];
  owner: "human" | "agent";
  status: "open" | "closed" | "all";
  project: string;
  limit: number;
}

const ownerTag = (o: "human" | "agent") => (o === "human" ? "human_task" : "agent_task");

/** Task refs in rank order (rank:N first, then oldest): an index walk by owner tag, never a scan of the records. */
export function taskRefs(sql: SqlStorage, baseline: boolean, q: TaskQuery): { refs: { wiki: string; id: string }[]; total: number } {
  const tag = ownerTag(q.owner);
  let total = 0;
  const found: { wiki: string; id: string; rank: number; seq: number }[] = [];
  for (const part of slices(q.wikis, 60)) {
    const where = [`t.tag = ?`, `t.kind = 'T'`, `t.wiki IN (${marks(part.length)})`];
    const args: SqlStorageValue[] = [tag, ...part];
    if (q.status === "open") where.push(`t.open = 1`);
    if (q.status === "closed") where.push(`t.open = 0`);
    if (baseline) where.push(`t.vis = 1`);
    if (q.project) { where.push(`EXISTS (SELECT 1 FROM record_tags p WHERE p.wiki = t.wiki AND p.id = t.id AND p.tag = ?)`); args.push(`project:${q.project}`); }
    for (const r of sql.exec(`SELECT t.wiki AS wiki, t.id AS id, t.rank AS rank, t.seq AS seq FROM record_tags t WHERE ${where.join(" AND ")} ORDER BY t.rank, t.seq LIMIT ?`, ...args, q.limit)) {
      found.push({ wiki: r["wiki"] as string, id: r["id"] as string, rank: r["rank"] as number, seq: r["seq"] as number });
    }
    if (q.project) {
      total += Number(sql.exec(`SELECT COUNT(*) AS n FROM record_tags t WHERE ${where.join(" AND ")}`, ...args).one()["n"]);
    } else {
      // The counts table answers without touching the tasks: open tasks are bucket O, every task bucket T.
      const sum = (bucket: string) => Number(
        sql.exec(`SELECT COALESCE(SUM(n), 0) AS n FROM tag_counts WHERE tag = ? AND bucket = ? AND wiki IN (${marks(part.length)})${visCol(baseline)}`, tag, bucket, ...part).one()["n"],
      );
      total += q.status === "open" ? sum("O") : q.status === "all" ? sum("T") : sum("T") - sum("O");
    }
  }
  found.sort((a, b) => a.rank - b.rank || a.seq - b.seq);
  return { refs: found.slice(0, q.limit).map(({ wiki, id }) => ({ wiki, id })), total };
}
