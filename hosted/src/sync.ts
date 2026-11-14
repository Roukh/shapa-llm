// POST /v1/wikis/{wiki}/sync: apply a client's ops in order inside the caller's transaction,
// then return what the client is missing. The decision table is the contract's; the server
// never trusts the client for `rev` or `device`. A replayed request changes nothing.

import { type Device, needScope } from "./devices.js";
import { ApiError, fail } from "./errors.js";
import {
  COUNTER_KINDS, Invalid, LIMITS, clampFuture, contentKey, isTimestamp, normalizeRecord, normLinks,
  type Normalized, type SyncOpIn, type SyncResponseOut, type SyncResultOut,
} from "./records.js";
import { Repo, toWire, type Stored } from "./repo.js";
import { wikiFor } from "./ledger.js";
import { syncGate } from "./projects.js";
import { backlinkProject, projectLinks } from "./projectapi.js";
import { activationGate } from "./skills.js";
import { budgetProblem, gateStage, kindProblem, mayBaseline, tagProblem } from "./gates.js";
import { guardPrinted, storedNames } from "./reach.js";
import { projectKeyOf } from "./tags.js";
import { projectOf } from "./records.js";

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
  n = { ...n, updated: clampFuture(n.updated) };
  const wikiKind = repo.wiki(wiki)?.kind ?? "repo";
  if (!n.deleted) {
    // The tag and kind rules every writer meets (stages, one project, one task owner, the global wiki's tasks).
    const rawTags = Array.isArray((raw as { tags?: unknown } | null)?.tags) ? ((raw as { tags: unknown[] }).tags) : [];
    const bad = tagProblem({ kind: n.kind, tags: rawTags, wikiKind }) ?? kindProblem(wikiKind, n.kind, n.data["parent"]);
    if (bad) return { id: n.id, status: "rejected", rev: 0, error: bad };
  }
  if (n.deleted) {
    // A put that carries the tombstone flag is a delete and needs the delete scope.
    return applyDel(repo, d, wiki, { op: "del", id: n.id, uid: n.uid, deleted_at: n.updated, base_rev: baseRev }, extras);
  }
  n = rewrite(n, renames);

  // The record is identified by its uid (a note, which has none, by its path). If the uid is
  // already stored, that record is the target, whatever id the client used for it.
  const held = repo.get(wiki, n.id);
  const byUid = repo.byUid(wiki, n.uid);
  const target = byUid ?? (held && held.uid === n.uid ? held : null);

  // The notes printed into every session text change only from a device token with the delete scope.
  try {
    guardPrinted(d, wiki, [n.id, n.alias, n.data["alias"], ...(target ? storedNames(target) : [])]);
  } catch (e) {
    if (e instanceof ApiError) return { id: n.id, status: "rejected", rev: target?.rev ?? 0, error: e.message };
    throw e;
  }

  // The focus and skill record shapes and the project key format. Documents already stored are
  // grandfathered: the one-document rule gates creation, never a put of a document a client holds.
  const refused = syncGate(repo, wiki, n, target, (raw as { tags?: unknown } | null)?.tags);
  if (refused) return { id: n.id, status: "rejected", rev: target?.rev ?? 0, error: refused };
  // Activating a skill version needs the delete scope; anyone else's new active version is stored pending.
  const act = target && !target.deleted && sameContent(target, n) ? null : activationGate(d, n, target);
  if (act?.error) return { id: n.id, status: "rejected", rev: target?.rev ?? 0, error: act.error };
  if (act?.put) n = act.put;

  // Stage 0 is the operator's: only a delete-scope device token's put may add, remove or edit it. Anyone else's
  // stage:0 is stored as stage-proposed:0, and an edit of a stored stage:0 record is rejected.
  let note: string | undefined;
  const live = target && !target.deleted ? target : null;
  if (!(live && sameContent(live, n))) {
    const privileged = mayBaseline(d, true);
    const gated = gateStage({ stored: live ? live.tags : null, next: n.tags, privileged });
    if ("error" in gated) return { id: n.id, status: "rejected", rev: target?.rev ?? 0, error: gated.error };
    if (gated.notice) { n = { ...n, tags: gated.tags }; note = gated.notice; }
    if (privileged) {
      const over = budgetProblem(repo, wiki, n);
      if (over) return { id: n.id, status: "rejected", rev: target?.rev ?? 0, error: over };
    }
  }
  // The about link to the project's document follows the project tag (a document made later links them in `commit`).
  if (n.kind === "T" || n.kind === "M" || n.kind === "R" || n.kind === "I" || (n.kind === "D" && n.tags.includes("skill"))) {
    const key = projectKeyOf(n.tags);
    const links = projectLinks(repo, wiki, n.links, key, live ? projectKeyOf(live.tags) : "");
    if (links !== n.links) n = { ...n, links };
  }
  const commit = (rec: Normalized, was: Stored | null): Stored => {
    const saved = repo.save(wiki, rec, d.name, was);
    const key = rec.kind === "D" ? projectOf(rec.tags) : null;
    if (key && (!was || was.deleted || projectOf(was.tags) !== key)) backlinkProject(repo, wiki, saved, d.name);
    return saved;
  };
  const noted = <T extends SyncResultOut>(r: T): T => (note ? { ...r, note } : r);

  if (!target) {
    if (!held) {
      const saved = commit(resolveAlias(repo, wiki, n), null);
      return noted({ id: n.id, status: "accepted", rev: saved.rev });
    }
    // The id is held by a record with another uid (tombstones count): a label collision. The
    // incoming record is stored under a fresh id and the client relabels; the stored one is untouched.
    let newId: string;
    try {
      newId = repo.allocId(wiki, n.kind);
    } catch (e) {
      if (e instanceof ApiError) return { id: n.id, status: "rejected", rev: held.rev, error: e.message };
      throw e;
    }
    const saved = commit(resolveAlias(repo, wiki, { ...n, id: newId }), null);
    renames.set(n.id, newId);
    return noted({ id: n.id, status: "renamed", rev: saved.rev, new_id: newId });
  }

  if (target.kind !== n.kind) {
    return { id: n.id, status: "rejected", rev: target.rev, error: "uid belongs to a record of another kind" };
  }
  const result = (status: "accepted" | "noop" | "rejected", rev: number): SyncResultOut =>
    noted(target.id === n.id ? { id: n.id, status, rev } : { id: n.id, status, rev, new_id: target.id });
  if (target.id !== n.id) renames.set(n.id, target.id);
  // Same uid: the stored record keeps its id and uid; only its content can change.
  const incoming = resolveAlias(repo, wiki, { ...n, id: target.id });
  if (n.kind === "N" && baseRev === null) {
    // A note the client believes is new never overwrites a stored one (a file's mtime is not an
    // edit time). Over a tombstone, or with nothing stored, it is simply accepted.
    if (target.deleted) return result("accepted", commit(incoming, target).rev);
    if (target.data["body"] === incoming.data["body"]) return result("noop", target.rev);
    extras.add(target.id);
    return result("rejected", target.rev);
  }
  if (sameContent(target, incoming)) return result("noop", target.rev);
  if (baseRev === target.rev || n.updated > target.updated) return result("accepted", commit(incoming, target).rev);
  extras.add(target.id);
  return result("rejected", target.rev);
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
  // The record is found by its uid, under whatever id it has: a client whose last response was
  // lost may delete a record it never learned the stored id of. A note (no uid) is found by path.
  const deletedAt = clampFuture(op.deleted_at);
  const cur = op.uid ? repo.byUid(wiki, op.uid) : repo.get(wiki, op.id);
  if (!cur || cur.deleted || (!op.uid && cur.uid !== "")) return { id: op.id, status: "noop", rev: cur?.rev ?? 0 };
  if (op.base_rev === cur.rev || cur.updated <= deletedAt) {
    const tomb: Normalized = { id: cur.id, uid: cur.uid, kind: cur.kind, created: cur.created, updated: deletedAt, deleted: true, alias: "", data: {}, tags: [], links: [] };
    return { id: op.id, status: "accepted", rev: repo.save(wiki, tomb, d.name, cur).rev };
  }
  extras.add(cur.id);
  return { id: op.id, status: "rejected", rev: cur.rev };
}

export function sync(repo: Repo, d: Device, wiki: string, req: SyncIn): SyncResponseOut {
  needScope(d, "write");
  if (d.reach === "baseline") fail("forbidden", "this device does not sync: it reads stage 0 and writes notes, tasks, ideas, decisions and files through the API or MCP");
  wikiFor(repo, d, wiki);
  if (req.ops.length > LIMITS.opsPerSync) fail("too_large", `at most ${LIMITS.opsPerSync} ops per sync request`);
  const current = repo.rev();
  // A cursor ahead of this server means the store was reset: start over (upserts are idempotent).
  const cursor = req.cursor > current ? 0 : req.cursor;

  // The client's counters first: a label collision then takes an id past everything the client
  // is about to push, instead of one of them, so one collision never shifts the labels after it.
  for (const [kind, next] of Object.entries(req.counters)) {
    if ((COUNTER_KINDS as readonly string[]).includes(kind) && next >= 1) repo.raiseCounter(wiki, kind, next);
  }

  const renames = new Map<string, string>();
  const extras = new Set<string>();
  const results: SyncResultOut[] = [];
  for (const op of req.ops) {
    results.push(op.op === "put" ? applyPut(repo, d, wiki, op.record, op.base_rev, renames, extras) : applyDel(repo, d, wiki, op, extras));
  }

  // A response holds about LIMITS.changesBytes of record JSON. The records the client asked for
  // by name come first (current records of rejected ops, then `want`), each only while it fits;
  // one that does not is left out and the client asks again (it keeps wanting what it lacks).
  // The page of changes then takes what is left of the budget, and always at least one record,
  // so a client can never be stuck: the response may pass the budget by one record (<= 1 MB).
  const byId = new Map<string, Stored>();
  let used = 0;
  for (const id of [...extras, ...req.want]) {
    if (byId.has(id)) continue;
    const size = repo.recordSize(wiki, id);
    if (size === null || (byId.size > 0 && used + size > LIMITS.changesBytes)) continue;
    const s = repo.get(wiki, id);
    if (!s) continue;
    byId.set(id, s);
    used += size;
  }
  const { rows: main, more } = repo.changesPage(wiki, cursor, LIMITS.changesPage, Math.max(0, LIMITS.changesBytes - used));
  for (const s of main) byId.set(s.id, s);
  const changes = [...byId.values()].sort((a, b) => a.rev - b.rev).map(toWire);
  const nextCursor = more ? main[main.length - 1]!.rev : repo.rev();
  return { cursor: nextCursor, results, changes, counters: repo.counters(wiki), more };
}
