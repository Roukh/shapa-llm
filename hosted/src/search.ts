// BM25 search over live records (SQLite FTS5), by tag, and the cross-wiki `get`. User text is never
// parsed as FTS syntax: it is cut into letter/digit tokens and each token is quoted. `tags` (all
// must match) walk the relation index; with no words the search is by tag alone. A lower stage
// ranks first on a tie. Both read through the caller's view: a baseline device finds only what its
// reach allows.

import { type Device, needScope, needWiki } from "./devices.js";
import { LIMITS, TYPE_NAMES, normTags, type Kind, type RecordWire } from "./records.js";
import { Repo, toWire } from "./repo.js";

export function ftsQuery(text: string): string {
  const tokens = (text.match(/[\p{L}\p{N}]+/gu) ?? []).slice(0, 24).map((t) => Array.from(t).slice(0, 64).join(""));
  return tokens.map((t) => `"${t}"`).join(" OR ");
}

const NAME_TO_KIND = Object.fromEntries(Object.entries(TYPE_NAMES).map(([k, v]) => [v, k])) as Record<string, Kind>;
export const SEARCH_TAGS_MAX = 8;
/** A record's stage (its stage:N tag; 10 without one, so an unstaged record ranks after a staged one on a tie). */
const STAGE_SQL = `COALESCE((SELECT MIN(CAST(substr(j.value, 7) AS INTEGER)) FROM json_each(r.tags) j WHERE j.value GLOB 'stage:[0-9]'), 10)`;

export interface Hit {
  wiki: string;
  id: string;
  type: string;
  summary: string;
  updated: string;
}

export function search(
  repo: Repo, d: Device, q: { query: string; k: number; wiki?: string; types?: string[]; tags?: string[] },
): { results: Hit[]; mode: "bm25" } {
  needScope(d, "read");
  if (q.wiki !== undefined) needWiki(d, q.wiki);
  const match = ftsQuery(q.query);
  const tags = normTags(q.tags ?? []).slice(0, SEARCH_TAGS_MAX);
  const none = { results: [] as Hit[], mode: "bm25" as const };
  if (!match && tags.length === 0) return none;
  const k = Math.max(1, Math.min(q.k, LIMITS.searchK));

  const where = [`r.deleted = 0`];
  const args: SqlStorageValue[] = [];
  let from: string;
  let order: string;
  let rest = tags;
  if (match) {
    from = `fts JOIN records r ON r.seq = fts.rowid`;
    where.unshift(`fts MATCH ?`);
    args.push(match);
    // Scores within a tenth of each other are a tie (a stage tag itself nudges the score), and a tie goes to the lower stage.
    order = `ROUND(bm25(fts, 8.0, 1.0, 3.0, 5.0), 1), ${STAGE_SQL}, r.rev DESC`;
  } else {
    // By tag alone: walk the first tag's rows in the index, the rest are lookups.
    from = `record_tags t0 JOIN records r ON r.wiki = t0.wiki AND r.id = t0.id`;
    where.unshift(`t0.tag = ?`);
    args.push(tags[0]!);
    if (repo.baseline) where.push(`t0.vis = 1`);
    rest = tags.slice(1);
    order = `${STAGE_SQL}, r.rev DESC`;
  }
  if (repo.baseline) where.push(`r.base_vis = 1`);
  for (const t of rest) {
    where.push(`EXISTS (SELECT 1 FROM record_tags t WHERE t.wiki = r.wiki AND t.id = r.id AND t.tag = ?)`);
    args.push(t);
  }
  if (q.wiki !== undefined) {
    where.push(`r.wiki = ?`);
    args.push(q.wiki);
  } else if (d.wikis !== null) {
    if (d.wikis.length === 0) return none;
    where.push(`r.wiki IN (${d.wikis.map(() => "?").join(",")})`);
    args.push(...d.wikis);
  }
  if (q.types?.length) {
    const kinds = new Set<Kind>();
    let ideas = false;
    let decisions = false;
    for (const t of q.types) {
      if (t === "idea") ideas = true;
      else if (t === "decision") decisions = true;
      else if (t === "human_task" || t === "agent_task") kinds.add("T");
      else if (NAME_TO_KIND[t]) kinds.add(NAME_TO_KIND[t]!);
    }
    const ors: string[] = [];
    if (kinds.size) {
      ors.push(`r.kind IN (${[...kinds].map(() => "?").join(",")})`);
      args.push(...kinds);
    }
    if (ideas) ors.push(`(r.kind = 'M' AND EXISTS (SELECT 1 FROM json_each(r.tags) WHERE value = 'idea'))`);
    if (decisions) ors.push(`(r.kind = 'M' AND EXISTS (SELECT 1 FROM json_each(r.tags) WHERE value = 'decision'))`);
    if (!ors.length) return none;
    where.push(`(${ors.join(" OR ")})`);
  }
  args.push(k);
  const rows = repo.sql
    // The summary is read in SQL: a hit's body (up to 1 MB) never leaves the database.
    .exec(
      `SELECT r.wiki, r.id, r.kind, r.updated, COALESCE(json_extract(r.data, '$.summary'), json_extract(r.data, '$.title'), '') AS summary
         FROM ${from} WHERE ${where.join(" AND ")} ORDER BY ${order} LIMIT ?`,
      ...args,
    )
    .toArray();
  return {
    mode: "bm25",
    results: rows.map((r) => ({
      wiki: r["wiki"] as string,
      id: r["id"] as string,
      type: TYPE_NAMES[r["kind"] as Kind],
      summary: String(r["summary"] ?? ""),
      updated: r["updated"] as string,
    })),
  };
}

/** Every live record with this id or alias, across the wikis the device may use. */
export function getAcross(repo: Repo, d: Device, ref: string, wiki?: string): { matches: (RecordWire & { wiki: string })[] } {
  needScope(d, "read");
  if (wiki !== undefined) needWiki(d, wiki);
  return { matches: repo.across(ref, d.wikis, wiki).map((s) => ({ ...toWire(s), wiki: s.wiki })) };
}
