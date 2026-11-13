// BM25 search over live records (SQLite FTS5) and the cross-wiki `get`. User text is never
// parsed as FTS syntax: it is cut into letter/digit tokens and each token is quoted.

import { type Device, needScope, needWiki } from "./devices.js";
import { LIMITS, TYPE_NAMES, type Kind, type RecordWire } from "./records.js";
import { Repo, toWire } from "./repo.js";

export function ftsQuery(text: string): string {
  const tokens = (text.match(/[\p{L}\p{N}]+/gu) ?? []).slice(0, 24).map((t) => Array.from(t).slice(0, 64).join(""));
  return tokens.map((t) => `"${t}"`).join(" OR ");
}

const NAME_TO_KIND = Object.fromEntries(Object.entries(TYPE_NAMES).map(([k, v]) => [v, k])) as Record<string, Kind>;

export interface Hit {
  wiki: string;
  id: string;
  type: string;
  summary: string;
  updated: string;
}

export function search(
  repo: Repo, d: Device, q: { query: string; k: number; wiki?: string; types?: string[] },
): { results: Hit[]; mode: "bm25" } {
  needScope(d, "read");
  if (q.wiki !== undefined) needWiki(d, q.wiki);
  const match = ftsQuery(q.query);
  if (!match) return { results: [], mode: "bm25" };
  const k = Math.max(1, Math.min(q.k, LIMITS.searchK));

  const where = [`fts MATCH ?`, `r.deleted = 0`];
  const args: SqlStorageValue[] = [match];
  if (q.wiki !== undefined) {
    where.push(`r.wiki = ?`);
    args.push(q.wiki);
  } else if (d.wikis !== null) {
    if (d.wikis.length === 0) return { results: [], mode: "bm25" };
    where.push(`r.wiki IN (${d.wikis.map(() => "?").join(",")})`);
    args.push(...d.wikis);
  }
  if (q.types?.length) {
    const kinds = new Set<Kind>();
    let ideas = false;
    for (const t of q.types) {
      if (t === "idea") ideas = true;
      else if (NAME_TO_KIND[t]) kinds.add(NAME_TO_KIND[t]!);
    }
    const ors: string[] = [];
    if (kinds.size) {
      ors.push(`r.kind IN (${[...kinds].map(() => "?").join(",")})`);
      args.push(...kinds);
    }
    if (ideas) ors.push(`(r.kind = 'M' AND EXISTS (SELECT 1 FROM json_each(r.tags) WHERE value = 'idea'))`);
    if (!ors.length) return { results: [], mode: "bm25" };
    where.push(`(${ors.join(" OR ")})`);
  }
  args.push(k);
  const rows = repo.sql
    .exec(
      `SELECT r.wiki, r.id, r.kind, r.updated, r.data FROM fts JOIN records r ON r.seq = fts.rowid
       WHERE ${where.join(" AND ")} ORDER BY bm25(fts, 8.0, 1.0, 3.0, 5.0), r.rev DESC LIMIT ?`,
      ...args,
    )
    .toArray();
  return {
    mode: "bm25",
    results: rows.map((r) => {
      const data = JSON.parse(r["data"] as string) as Record<string, unknown>;
      return {
        wiki: r["wiki"] as string,
        id: r["id"] as string,
        type: TYPE_NAMES[r["kind"] as Kind],
        summary: String(data["summary"] ?? data["title"] ?? ""),
        updated: r["updated"] as string,
      };
    }),
  };
}

/** Every live record with this id or alias, across the wikis the device may use. */
export function getAcross(repo: Repo, d: Device, ref: string, wiki?: string): { matches: (RecordWire & { wiki: string })[] } {
  needScope(d, "read");
  if (wiki !== undefined) needWiki(d, wiki);
  return { matches: repo.across(ref, d.wikis, wiki).map((s) => ({ ...toWire(s), wiki: s.wiki })) };
}
