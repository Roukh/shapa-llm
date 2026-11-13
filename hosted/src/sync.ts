// POST /v1/wikis/{wiki}/sync: apply a client's ops in order inside the caller's transaction,
// then return what the client is missing. The decision table is the contract's; the server
// never trusts the client for `rev` or `device`. A replayed request changes nothing.

import { type Device, needScope } from "./devices.js";
import { ApiError, fail } from "./errors.js";
import {
  COUNTER_KINDS, Invalid, LIMITS, contentKey, isTimestamp, normalizeRecord, normLinks,
  type Normalized, type SyncOpIn, type SyncResponseOut, type SyncResultOut,
} from "./records.js";
import { Repo, toWire, type Stored } from "./repo.js";
import { wikiFor } from "./ledger.js";

interface SyncIn {
  cursor: number;
  ops: SyncOpIn[];
  want: string[];
  counters: Record<string, number>;
}

/** Later ops that name a renamed id as `parent` or link `dst` follow the new id. */
function rewrite(n: Normalized, renames: Map<string, string>): Normalized {
  if (renames.size === 0) return n;
  let out = n;
  const parent = n.data["parent"];
  if (typeof parent === "string" && renames.has(parent)) out = { ...out, data: { ...out.data, parent: renames.get(parent) } };
  if (n.links.some(([dst]) => renames.has(dst))) {
    out = { ...out, links: normLinks(n.links.map(([dst, rel]) => [renames.get(dst) ?? dst, rel])) };
  }
  return out;
}

/** An alias another live record holds is dropped, not an error: the record still syncs. */
function resolveAlias(repo: Repo, wiki: string, n: Normalized): Normalized {
  if (!n.alias) return n;
  const holder = repo.aliasHolder(wiki, n.alias);
  if (!holder || holder === n.id) return n;
  return { ...n, alias: "", data: { ...n.data, alias: null } };
}

function sameContent(s: Stored, n: Normalized): boolean {
  return contentKey(s) === contentKey(n);
}

function applyPut(
  repo: Repo, d: Device, wiki: string, raw: unknown, baseRev: number | null,
  renames: Map<string, string>, extras: Set<string>,
): SyncResultOut {
  let n: Normalized;
  try {
    n = normalizeRecord(raw);
  } catch (e) {
    if (!(e instanceof Invalid)) throw e;
    const id = typeof (raw as { id?: unknown } | null)?.id === "string" ? String((raw as { id: string }).id).slice(0, 120) : "";
    return { id, status: "rejected", rev: 0, error: e.message };
  }
  if (n.deleted) {
    // A put that carries the tombstone flag is a delete and needs the delete scope.
    return applyDel(repo, d, wiki, { op: "del", id: n.id, created: n.created, deleted_at: n.updated, base_rev: baseRev }, extras);
  }
  n = resolveAlias(repo, wiki, rewrite(n, renames));
  const cur = repo.get(wiki, n.id);

  if (!cur) {
    const saved = repo.save(wiki, n, d.name, null);
    return { id: n.id, status: "accepted", rev: saved.rev };
  }

  if (cur.created !== n.created) {
    // The id is taken by another record. A replay of an earlier rename finds its twin; otherwise
    // the record is stored under a fresh id and the client relabels.
    const twin = repo.byIdentity(wiki, n.kind, n.created).find((t) => t.id !== n.id && sameContent(t, { ...n, id: t.id }));
    if (twin) {
      renames.set(n.id, twin.id);
      return { id: n.id, status: "renamed", rev: twin.rev, new_id: twin.id };
    }
    let newId: string;
    try {
      newId = repo.allocId(wiki, n.kind);
    } catch (e) {
      if (e instanceof ApiError) return { id: n.id, status: "rejected", rev: cur.rev, error: e.message };
      throw e;
    }
    const saved = repo.save(wiki, resolveAlias(repo, wiki, { ...n, id: newId }), d.name, null);
    renames.set(n.id, newId);
    return { id: n.id, status: "renamed", rev: saved.rev, new_id: newId };
  }

  if (sameContent(cur, n)) return { id: n.id, status: "noop", rev: cur.rev };
  if (baseRev === cur.rev || n.updated > cur.updated) {
    const saved = repo.save(wiki, n, d.name, cur);
    return { id: n.id, status: "accepted", rev: saved.rev };
  }
  extras.add(n.id);
  return { id: n.id, status: "rejected", rev: cur.rev };
}

function applyDel(
  repo: Repo, d: Device, wiki: string,
  op: Extract<SyncOpIn, { op: "del" }>, extras: Set<string>,
): SyncResultOut {
  if (!d.scopes.includes("delete")) {
    extras.add(op.id);
    return { id: op.id, status: "rejected", rev: repo.get(wiki, op.id)?.rev ?? 0, error: "this device lacks the delete scope" };
  }
  if (!isTimestamp(op.deleted_at)) return { id: op.id, status: "rejected", rev: 0, error: "deleted_at must be YYYY-MM-DDTHH:MM:SSZ" };
  const cur = repo.get(wiki, op.id);
  if (!cur || cur.deleted || cur.created !== op.created) return { id: op.id, status: "noop", rev: cur?.rev ?? 0 };
  if (op.base_rev === cur.rev || cur.updated <= op.deleted_at) {
    const tomb: Normalized = { id: cur.id, kind: cur.kind, created: cur.created, updated: op.deleted_at, deleted: true, alias: "", data: {}, tags: [], links: [] };
    return { id: op.id, status: "accepted", rev: repo.save(wiki, tomb, d.name, cur).rev };
  }
  extras.add(op.id);
  return { id: op.id, status: "rejected", rev: cur.rev };
}

export function sync(repo: Repo, d: Device, wiki: string, req: SyncIn): SyncResponseOut {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  if (req.ops.length > LIMITS.opsPerSync) fail("too_large", `at most ${LIMITS.opsPerSync} ops per sync request`);
  const current = repo.rev();
  // A cursor ahead of this server means the store was reset: start over (upserts are idempotent).
  const cursor = req.cursor > current ? 0 : req.cursor;

  const renames = new Map<string, string>();
  const extras = new Set<string>();
  const results: SyncResultOut[] = [];
  for (const op of req.ops) {
    results.push(op.op === "put" ? applyPut(repo, d, wiki, op.record, op.base_rev, renames, extras) : applyDel(repo, d, wiki, op, extras));
  }

  for (const [kind, next] of Object.entries(req.counters)) {
    if ((COUNTER_KINDS as readonly string[]).includes(kind) && next >= 1) repo.raiseCounter(wiki, kind, next);
  }

  const page = repo.changes(wiki, cursor, LIMITS.changesPage + 1);
  const more = page.length > LIMITS.changesPage;
  const main = more ? page.slice(0, LIMITS.changesPage) : page;
  const byId = new Map<string, Stored>(main.map((s) => [s.id, s]));
  for (const id of [...extras, ...req.want]) {
    if (byId.has(id)) continue;
    const s = repo.get(wiki, id);
    if (s) byId.set(id, s);
  }
  const changes = [...byId.values()].sort((a, b) => a.rev - b.rev).map(toWire);
  const nextCursor = more ? main[main.length - 1]!.rev : repo.rev();
  return { cursor: nextCursor, results, changes, counters: repo.counters(wiki), more };
}
