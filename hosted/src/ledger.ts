// Record operations behind REST and MCP: create, edit, list, ledger moves, delete. One
// implementation of each; both front ends call these through the store. The ledger rules
// mirror shapa/db.py (parent kinds, no child under a closed parent, close cascade, claim and
// release, identical row text returns the existing row, ids never reused).

import { type Device, needScope, needWiki } from "./devices.js";
import { fail } from "./errors.js";
import {
  CLOSE_REASONS, Invalid, LEDGER_KINDS, PARENT_KIND, TYPE_NAMES, buildData, contentKey, isNoteId, nowIso, normTags,
  type CreateIn, type CreateType, type Data, type Kind, type Normalized, type PatchIn, type RecordWire,
} from "./records.js";
import { Repo, toWire, type Stored } from "./repo.js";

const TYPE_KIND: Record<CreateType, Kind> = {
  feature: "F", job: "J", task: "T", memory: "M", idea: "M", rule: "R", issue: "I", document: "D", note: "N",
};
const isLedger = (k: Kind) => (LEDGER_KINDS as readonly string[]).includes(k);

function wrap<T>(fn: () => T): T {
  try {
    return fn();
  } catch (e) {
    if (e instanceof Invalid) fail("invalid", e.message);
    throw e;
  }
}

function norm(s: Stored): Normalized {
  return { id: s.id, kind: s.kind, created: s.created, updated: s.updated, deleted: s.deleted, alias: s.alias, data: s.data, tags: s.tags, links: s.links };
}

/** `updated` for a REST/MCP write: now, never earlier than the record already says. */
function stamp(prev?: string): string {
  const now = nowIso();
  return prev && prev > now ? prev : now;
}

/** The wiki must exist and the device must be allowed to use it. */
export function wikiFor(repo: Repo, d: Device, wiki: string): void {
  needWiki(d, wiki);
  if (!repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}; register it with PUT /v1/wikis/${wiki}`);
}

function liveRecord(repo: Repo, wiki: string, ref: string): Stored {
  const rec = repo.find(wiki, ref);
  if (!rec) fail("not_found", `no record ${ref} in ${wiki}`);
  return rec;
}

function checkParent(repo: Repo, wiki: string, kind: Kind, parentRef: string): string {
  const want = PARENT_KIND[kind];
  if (!want) fail("invalid", `a ${TYPE_NAMES[kind]} has no parent`);
  const p = repo.find(wiki, parentRef);
  if (!p) fail("not_found", `no parent ${parentRef} in ${wiki}`);
  if (p.kind !== want) fail("invalid", `a ${TYPE_NAMES[kind]}'s parent must be a ${TYPE_NAMES[want]}, not ${p.id}`);
  if (p.data["status"] === "closed") fail("conflict", `${p.id} is closed`);
  return p.id;
}

// --- create ---------------------------------------------------------------------------------

export function createRecord(repo: Repo, d: Device, wiki: string, body: CreateIn): { record: RecordWire; created: boolean } {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  const kind = TYPE_KIND[body.type];
  const ts = nowIso();
  const tags = normTags([...(body.tags ?? []), ...(body.type === "idea" ? ["idea"] : [])]);
  const text = body.title ?? body.summary ?? "";
  if (body.parent !== undefined && !isLedger(kind)) fail("invalid", "only a job or task takes a parent");
  if (body.parent !== undefined && kind === "F") fail("invalid", "a feature has no parent");
  if (body.verify !== undefined && !isLedger(kind)) fail("invalid", "verify applies to ledger items");

  let id: string;
  let raw: Data;
  if (kind === "N") {
    if (!body.path || !isNoteId(body.path)) fail("invalid", "a note needs a path like arch/name.md, research/name.md or name.md");
    id = body.path;
    raw = { body: body.body ?? "" };
  } else if (isLedger(kind)) {
    id = `${kind}1`;
    const parent = body.parent === undefined ? null : checkParent(repo, wiki, kind, body.parent);
    raw = { title: text, body: body.body ?? "", parent, verify: body.verify ?? null, alias: body.alias ?? null };
  } else if (kind === "D") {
    id = "D1";
    raw = { title: text, body: body.body ?? "", alias: body.alias ?? null, ...(body.mime ? { mime: body.mime } : {}) };
  } else {
    id = `${kind}1`;
    raw = { summary: text, body: body.body ?? "", alias: body.alias ?? null };
  }
  const built = wrap(() => buildData(kind, id, raw));

  if (kind === "M" || kind === "R" || kind === "I") {
    const same = repo.identicalRow(wiki, kind, String(built.data["summary"]), String(built.data["body"]));
    if (same) {
      const merged = normTags([...same.tags, ...tags]);
      if (merged.length === same.tags.length) return { record: toWire(same), created: false };
      const n = { ...norm(same), tags: merged, updated: stamp(same.updated) };
      return { record: toWire(repo.save(wiki, n, d.name, same)), created: false };
    }
  }
  if (built.alias && kind !== "N" && repo.aliasHolder(wiki, built.alias)) {
    fail("conflict", `alias ${built.alias} already names ${repo.aliasHolder(wiki, built.alias)}`);
  }

  let existing: Stored | null = null;
  if (kind === "N") {
    existing = repo.get(wiki, id);
    if (existing && !existing.deleted) fail("conflict", `note ${id} exists; update it instead`);
  } else {
    id = repo.allocId(wiki, kind);
  }
  const alias = kind === "N" && repo.aliasHolder(wiki, built.alias) ? "" : built.alias;
  const n: Normalized = {
    id, kind, created: kind === "N" ? "" : ts, updated: existing ? stamp(existing.updated) : ts, deleted: false,
    alias, data: kind === "N" ? { ...built.data, alias: alias || null } : built.data, tags, links: [],
  };
  return { record: toWire(repo.save(wiki, n, d.name, existing)), created: true };
}

// --- read -----------------------------------------------------------------------------------

export function getRecord(repo: Repo, d: Device, wiki: string, ref: string): RecordWire {
  needScope(d, "read");
  wikiFor(repo, d, wiki);
  return toWire(liveRecord(repo, wiki, ref));
}

export function listRecords(
  repo: Repo, d: Device, wiki: string,
  f: { kind?: Kind; status?: string; tag?: string; after: number; limit: number },
): { records: RecordWire[]; next: number | null } {
  needScope(d, "read");
  wikiFor(repo, d, wiki);
  const rows = repo.list(wiki, f);
  return { records: rows.map(toWire), next: rows.length === f.limit ? rows[rows.length - 1]!.rev : null };
}

// --- edit -----------------------------------------------------------------------------------

export function patchRecord(repo: Repo, d: Device, wiki: string, ref: string, p: PatchIn): RecordWire {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  const rec = liveRecord(repo, wiki, ref);
  if (p.base_rev !== undefined && p.base_rev !== rec.rev) {
    fail("conflict", `${rec.id} is at rev ${rec.rev}, not ${p.base_rev}`, { current: toWire(rec) });
  }
  const k = rec.kind;
  const data: Data = { ...rec.data };
  const only = (field: string, ok: boolean) => {
    if (!ok) fail("invalid", `${field} does not apply to a ${TYPE_NAMES[k]}`);
  };
  const row = k === "M" || k === "R" || k === "I";
  if (p.summary !== undefined || p.title !== undefined) {
    only("title/summary", k !== "N");
    const text = row ? (p.summary ?? p.title) : (p.title ?? p.summary);
    data[row ? "summary" : "title"] = text;
  }
  if (p.body !== undefined) data["body"] = p.body;
  if (p.alias !== undefined) { only("alias", k !== "N"); data["alias"] = p.alias; }
  if (p.verify !== undefined) { only("verify", isLedger(k)); data["verify"] = p.verify; }
  if (p.git_ref !== undefined) { only("git_ref", isLedger(k)); data["git_ref"] = p.git_ref; }
  if (p.mime !== undefined) { only("mime", k === "D"); data["mime"] = p.mime; }
  if (p.parent !== undefined) {
    only("parent", k === "J" || k === "T");
    if (rec.data["status"] === "closed") fail("conflict", `${rec.id} is closed`);
    data["parent"] = p.parent === null ? null : checkParent(repo, wiki, k, p.parent);
  }
  const built = wrap(() => buildData(k, rec.id, data));
  if (k !== "N" && built.alias && built.alias !== rec.alias) {
    const holder = repo.aliasHolder(wiki, built.alias);
    if (holder && holder !== rec.id) fail("conflict", `alias ${built.alias} already names ${holder}`);
  }
  const alias = k === "N" && built.alias !== rec.alias && repo.aliasHolder(wiki, built.alias) ? "" : built.alias;
  const next: Normalized = {
    ...norm(rec), alias, data: k === "N" ? { ...built.data, alias: alias || null } : built.data,
    tags: p.tags === undefined ? rec.tags : normTags(p.tags),
  };
  if (contentKey({ ...next, updated: rec.updated }) === contentKey(norm(rec)) && alias === rec.alias) return toWire(rec);
  next.updated = stamp(rec.updated);
  return toWire(repo.save(wiki, next, d.name, rec));
}

// --- ledger moves ---------------------------------------------------------------------------

function ledgerItem(repo: Repo, wiki: string, ref: string): Stored {
  const rec = liveRecord(repo, wiki, ref);
  if (!isLedger(rec.kind)) fail("invalid", `${rec.id} is a ${TYPE_NAMES[rec.kind]}, not a ledger item`);
  return rec;
}

function withData(rec: Stored, patch: Data): Normalized {
  return { ...norm(rec), data: { ...rec.data, ...patch }, updated: stamp(rec.updated) };
}

export function claim(repo: Repo, d: Device, wiki: string, ref: string): RecordWire {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  const rec = ledgerItem(repo, wiki, ref);
  if (rec.data["status"] === "closed") fail("conflict", `${rec.id} is closed`);
  return toWire(repo.save(wiki, withData(rec, { status: "claimed", claimed_by: d.name }), d.name, rec));
}

export function release(repo: Repo, d: Device, wiki: string, ref: string): RecordWire {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  const rec = ledgerItem(repo, wiki, ref);
  if (rec.data["status"] !== "claimed") return toWire(rec);
  return toWire(repo.save(wiki, withData(rec, { status: "open", claimed_by: null }), d.name, rec));
}

export function close(
  repo: Repo, d: Device, wiki: string, ref: string, reason: string, gitRef?: string,
): { record: RecordWire; closed: string[] } {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  if (!(CLOSE_REASONS as readonly string[]).includes(reason)) fail("invalid", `reason must be one of ${CLOSE_REASONS.join(", ")}`);
  const rec = ledgerItem(repo, wiki, ref);
  if (rec.data["status"] === "closed") return { record: toWire(rec), closed: [] };
  const ts = nowIso();
  const patch: Data = { status: "closed", closed_reason: reason, closed: ts };
  if (gitRef) patch["git_ref"] = gitRef;
  const done = repo.save(wiki, withData(rec, patch), d.name, rec);
  const closed = [rec.id];
  const queue = [rec.id];
  while (queue.length) {
    for (const child of repo.children(wiki, queue.shift()!)) {
      queue.push(child.id);
      if (child.data["status"] === "closed") continue;
      repo.save(wiki, withData(child, { status: "closed", closed_reason: "parent", closed: ts }), d.name, child);
      closed.push(child.id);
    }
  }
  return { record: toWire(done), closed };
}

// --- delete ---------------------------------------------------------------------------------

export function deleteRecord(repo: Repo, d: Device, wiki: string, ref: string): RecordWire {
  needScope(d, "delete");
  wikiFor(repo, d, wiki);
  const rec = liveRecord(repo, wiki, ref);
  const tomb: Normalized = { ...norm(rec), deleted: true, alias: "", data: {}, tags: [], links: [], updated: stamp(rec.updated) };
  return toWire(repo.save(wiki, tomb, d.name, rec));
}

// --- the ledger view (MCP ledger_list, session instructions) ---------------------------------

export interface LedgerNode {
  id: string;
  type: string;
  title: string;
  status: string;
  claimed_by: string | null;
  children: LedgerNode[];
}

export function ledgerTree(repo: Repo, wiki: string): LedgerNode[] {
  const items = repo.live(wiki, LEDGER_KINDS).filter((s) => s.data["status"] !== "closed");
  const node = (s: Stored): LedgerNode => ({
    id: s.id, type: TYPE_NAMES[s.kind], title: String(s.data["title"] ?? ""), status: String(s.data["status"] ?? "open"),
    claimed_by: (s.data["claimed_by"] as string | null) ?? null, children: [],
  });
  const byId = new Map(items.map((s) => [s.id, node(s)]));
  const roots: LedgerNode[] = [];
  for (const s of items) {
    const n = byId.get(s.id)!;
    const parent = s.data["parent"] ? byId.get(String(s.data["parent"])) : undefined;
    (parent ? parent.children : roots).push(n);
  }
  return roots;
}
