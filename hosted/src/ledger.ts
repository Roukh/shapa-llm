// Record operations behind REST and MCP: create, edit, list, ledger moves, delete. One
// implementation of each; both front ends call these through the store. The ledger rules
// mirror shapa/db.py (parent kinds, no child under a closed parent, close cascade, claim and
// release, identical row text returns the existing row, ids never reused).
//
// Every record read here goes through the `repo` it is given, which is the caller's view (a
// baseline device's view hides what it may not read); `repo.raw` is used only to keep ids and
// aliases unique.

import { type Device, needScope, needWiki, randomHex } from "./devices.js";
import { fail } from "./errors.js";
import { onFeatureClosed } from "./focus.js";
import { BASELINE_EDIT_REFUSAL, gateStage, kindProblem, tagProblem } from "./gates.js";
import { backlinkProject, projectLinks } from "./projectapi.js";
import { documentTags, gateNewDocument, mergedBody, projectHolder, refusal } from "./projects.js";
import { baselineKeepsRank, baselineMayCreate, baselineMayWrite, baselineWrites, denyBaseline, guardPrinted, isBaseline, ranksChanged, storedNames } from "./reach.js";
import {
  CLOSE_REASONS, Invalid, LEDGER_KINDS, PARENT_KIND, PROJECT_PREFIX, RESERVED_DOC_TAGS, FOCUS_TAG, SKILL_TAG, TYPE_NAMES, buildData, contentKey, isNoteId, nowIso, normTags, projectOf,
  type CreateIn, type CreateType, type Data, type Kind, type Normalized, type PatchIn, type RecordWire,
} from "./records.js";
import { Repo, stampAfter, toNormalized, toWire, type Stored } from "./repo.js";
import { lessonFromRow } from "./skills.js";
import { OWNER_TAGS, hasBaseline, projectKeyOf } from "./tags.js";

const TYPE_KIND: Record<CreateType, Kind> = {
  feature: "F", job: "J", task: "T", human_task: "T", agent_task: "T", memory: "M", idea: "M", decision: "M", rule: "R", issue: "I", document: "D", note: "N",
};
/** The type names that are a tagged kind: `idea` and `decision` are memories, the two task names are tasks. */
const TYPE_TAG: Partial<Record<CreateType, string>> = { idea: "idea", decision: "decision", human_task: "human_task", agent_task: "agent_task" };
/** The kinds that may carry a project tag of their own (a document is the project's file; features and jobs have none). */
const PROJECT_KINDS: readonly Kind[] = ["D", "T", "M", "R", "I"];
const isLedger = (k: Kind) => (LEDGER_KINDS as readonly string[]).includes(k);

function wrap<T>(fn: () => T): T {
  try {
    return fn();
  } catch (e) {
    if (e instanceof Invalid) fail("invalid", e.message);
    throw e;
  }
}

const norm = toNormalized;
const stamp = stampAfter;

/** The wiki must exist and the device must be allowed to use it. */
export function wikiFor(repo: Repo, d: Device, wiki: string): void {
  needWiki(d, wiki);
  if (!repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}; register it with PUT /v1/wikis/${wiki}`);
}

function wikiKindOf(repo: Repo, wiki: string): "global" | "repo" {
  return repo.wiki(wiki)?.kind ?? "repo";
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

/** The alias a record ends up with: the one asked for when it is free. An alias a live record holds is a 409 naming
 *  the holder, except for a baseline device: the holder may be a record hidden from it, so it is told nothing, and
 *  the alias is dropped ("" is returned) as if it had not been asked for. */
function claimAlias(repo: Repo, d: Device, wiki: string, alias: string, except?: string): string {
  const holder = repo.raw.aliasHolder(wiki, alias);
  if (!holder || holder === except) return alias;
  if (isBaseline(d)) return "";
  fail("conflict", `alias ${alias} already names ${holder}`);
}

/** Saving text a record already holds. The holder's tags, project and device change only when this writer could
 *  have made that edit directly: the same gates as PATCH. A writer that may not gets the holder back unchanged (it
 *  could read it, or `identical*` would not have found it; a holder hidden from it is never found, so that writer
 *  gets a new separate record and nothing reveals the hidden one). */
function saveIntoHolder(repo: Repo, d: Device, wiki: string, same: Stored, tags: readonly string[]): Created {
  const unchanged: Created = { record: toWire(same), created: false };
  const merged = normTags([...same.tags, ...tags]);
  if (merged.length === same.tags.length && merged.every((t) => same.tags.includes(t))) return unchanged;
  if (hasBaseline(same.tags)) return unchanged; // a stored stage:0 record is edited only by the operator's sync
  if (isBaseline(d) && (!baselineWrites(same.kind, merged) || ranksChanged(same.tags, merged))) return unchanged;
  const bad = tagProblem({ kind: same.kind, tags: merged, wikiKind: wikiKindOf(repo, wiki) });
  if (bad) fail("invalid", bad);
  const gated = gateStage({ stored: same.tags, next: merged, privileged: false });
  if ("error" in gated) return unchanged;
  const links = PROJECT_KINDS.includes(same.kind) ? projectLinks(repo, wiki, same.links, projectKeyOf(gated.tags), projectKeyOf(same.tags)) : same.links;
  const saved = repo.save(wiki, { ...norm(same), tags: gated.tags, links, updated: stamp(same.updated) }, d.name, same);
  if (!isBaseline(d)) lessonFromRow(repo.raw, wiki, saved, d.name);
  return { record: toWire(saved), created: false, ...(gated.notice ? { notice: gated.notice } : {}) };
}

// --- create ---------------------------------------------------------------------------------

export interface Created {
  record: RecordWire;
  created: boolean;
  /** Set when the server stored something other than what was asked (a stage:0 tag kept out of the baseline). */
  notice?: string;
}

export function createRecord(repo: Repo, d: Device, wiki: string, body: CreateIn): Created {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  baselineMayCreate(d, body.type);
  const wikiKind = wikiKindOf(repo, wiki);
  const kind = TYPE_KIND[body.type];
  const ts = nowIso();
  if (body.project !== undefined && !PROJECT_KINDS.includes(kind)) fail("invalid", "project applies to a document, task, idea, decision, rule, issue or memory only");
  if (body.merge !== undefined && kind !== "D") fail("invalid", "merge applies to a document only");
  const typeTag = TYPE_TAG[body.type];
  const sent = [...(body.tags ?? []), ...(typeTag ? [typeTag] : [])];
  const projectKey = body.project === undefined ? undefined : wrap(() => documentTags([], body.project)[0]?.slice(PROJECT_PREFIX.length) ?? "");
  // The tags as the caller means them: a `project` argument replaces any project tag it also sent.
  const meant = projectKey ? [...sent.filter((t) => !String(t).trim().startsWith(PROJECT_PREFIX)), PROJECT_PREFIX + projectKey] : sent;
  const bad = tagProblem({ kind, tags: meant, wikiKind });
  if (bad) fail("invalid", bad);
  let tags = normTags(meant);
  if (kind === "D" && tags.some((t) => RESERVED_DOC_TAGS.includes(t))) fail("invalid", "the tags focus and skill are reserved for the focus record and skill versions");
  const wrongKind = kindProblem(wikiKind, kind, body.parent);
  if (wrongKind) fail("invalid", wrongKind);
  baselineMayWrite(d, kind, tags);
  baselineKeepsRank(d, [], tags);
  const text = body.title ?? body.summary ?? "";
  if (body.parent !== undefined && !isLedger(kind)) fail("invalid", "only a job or task takes a parent");
  if (body.parent !== undefined && kind === "F") fail("invalid", "a feature has no parent");
  const gated = gateStage({ stored: null, next: tags, privileged: false });
  if ("error" in gated) fail("forbidden", gated.error);
  tags = gated.tags;

  let id: string;
  let raw: Data;
  if (kind === "N") {
    if (!body.path || !isNoteId(body.path)) fail("invalid", "a note needs a path like arch/name.md, research/name.md or name.md");
    id = body.path;
    raw = { body: body.body ?? "" };
  } else if (isLedger(kind)) {
    id = `${kind}1`;
    const parent = body.parent === undefined ? null : checkParent(repo, wiki, kind, body.parent);
    raw = { title: text, body: body.body ?? "", parent, alias: body.alias ?? null };
  } else if (kind === "D") {
    id = "D1";
    raw = { title: text, body: body.body ?? "", alias: body.alias ?? null, ...(body.mime ? { mime: body.mime } : {}) };
  } else {
    id = `${kind}1`;
    raw = { summary: text, body: body.body ?? "", alias: body.alias ?? null };
  }
  const built = wrap(() => buildData(kind, id, raw));
  guardPrinted(d, wiki, [body.path, body.alias, built.alias]);

  if (kind === "D") {
    // The lean gate: a project keeps one document. Refuse the second (409 naming the first) or merge into it.
    const held = gateNewDocument(repo, wiki, tags, body.merge === true);
    if (held) {
      const merged = mergedBody(held.data, String(built.data["title"]), String(built.data["body"]));
      const n = { ...norm(held), data: { ...held.data, body: merged }, updated: stamp(held.updated) };
      return { record: toWire(repo.save(wiki, n, d.name, held)), created: false };
    }
  }
  if (kind === "M" || kind === "R" || kind === "I") {
    // Identical text is stored once. The holder is found through the caller's view, and merged into only as far as
    // the caller could edit it directly.
    const same = repo.identicalRow(wiki, kind, String(built.data["summary"]), String(built.data["body"]));
    if (same) return saveIntoHolder(repo, d, wiki, same, tags);
  }
  const owner = tags.find((t) => t in OWNER_TAGS);
  if (kind === "T" && owner && body.parent === undefined) {
    // Identical open text is stored once: the same task is the same task.
    const same = repo.identicalOpenTask(wiki, owner, String(built.data["title"]), String(built.data["body"]));
    if (same) return saveIntoHolder(repo, d, wiki, same, tags);
  }
  let alias = built.alias;
  let data = built.data;
  if (alias && kind !== "N") {
    alias = claimAlias(repo, d, wiki, alias);
    if (!alias) data = { ...data, alias: null };
  }

  let existing: Stored | null = null;
  if (kind === "N") {
    existing = repo.raw.get(wiki, id);
    if (existing && !existing.deleted) fail("conflict", `note ${id} exists; update it instead`);
  } else {
    id = repo.allocId(wiki, kind);
  }
  if (kind === "N" && repo.raw.aliasHolder(wiki, alias)) alias = "";
  const key = PROJECT_KINDS.includes(kind) && kind !== "D" ? projectKeyOf(tags) : "";
  const n: Normalized = {
    id, uid: kind === "N" ? "" : randomHex(16), kind, created: kind === "N" ? "" : ts, updated: existing ? stamp(existing.updated) : ts, deleted: false,
    alias, data: kind === "N" ? { ...data, alias: alias || null } : data, tags, links: key ? projectLinks(repo, wiki, [], key, "") : [],
  };
  const saved = repo.save(wiki, n, d.name, existing);
  // A lesson is filed into a skill only from the operator's own writers: never from a baseline device.
  if ((kind === "M" || kind === "R" || kind === "I") && !isBaseline(d)) lessonFromRow(repo.raw, wiki, saved, d.name);
  if (kind === "D" && projectOf(tags)) backlinkProject(repo, wiki, saved, d.name);
  return { record: toWire(saved), created: true, ...(gated.notice ? { notice: gated.notice } : {}) };
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

export interface Patched {
  record: RecordWire;
  notice?: string;
}

export function patchRecord(repo: Repo, d: Device, wiki: string, ref: string, p: PatchIn): Patched {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  const rec = liveRecord(repo, wiki, ref);
  const k = rec.kind;
  const wikiKind = wikiKindOf(repo, wiki);
  // What this device may touch at all: a baseline device edits notes, documents, tasks, ideas and
  // decisions (never a printed note, the focus, or a record of a staged kind). Nobody but the
  // operator's sync edits a stored stage:0 record.
  guardPrinted(d, wiki, storedNames(rec));
  baselineMayWrite(d, k, rec.tags);
  if (hasBaseline(rec.tags)) fail("forbidden", BASELINE_EDIT_REFUSAL);
  if (p.base_rev !== undefined && p.base_rev !== rec.rev) {
    fail("conflict", `${rec.id} is at rev ${rec.rev}, not ${p.base_rev}`, { current: toWire(rec) });
  }
  if (k === "D" && rec.tags.includes(SKILL_TAG)) {
    fail("invalid", `${rec.id} is a skill version: it is activated, edited or retired with \`shapa skill\` on the operator's machine, never through the API or MCP`);
  }
  let nextTags = rec.tags;
  let notice: string | undefined;
  if (p.tags !== undefined) {
    const bad = tagProblem({ kind: k, tags: p.tags, wikiKind });
    if (bad) fail("invalid", bad);
    nextTags = normTags(p.tags);
    baselineMayWrite(d, k, nextTags);
    baselineKeepsRank(d, rec.tags, nextTags);
    if (k === "D") {
      // The focus and skill tags make a record the engine's: no edit adds or removes one (a focus
      // record retagged `skill` would be an active skill version), and the focus record's tags are fixed.
      const reserved = (ts: readonly string[]) => ts.filter((t) => RESERVED_DOC_TAGS.includes(t)).sort().join(",");
      if (reserved(nextTags) !== reserved(rec.tags)) fail("invalid", "the focus and skill tags are reserved for the focus record and skill versions");
      if (rec.tags.includes(FOCUS_TAG) && nextTags.join("\n") !== [...rec.tags].sort().join("\n")) fail("invalid", "the focus record's tags are fixed");
    }
    const gated = gateStage({ stored: rec.tags, next: nextTags, privileged: false });
    if ("error" in gated) fail("forbidden", gated.error);
    nextTags = gated.tags;
    notice = gated.notice;
  }
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
  if (p.git_ref !== undefined) { only("git_ref", isLedger(k)); data["git_ref"] = p.git_ref; }
  if (p.mime !== undefined) { only("mime", k === "D"); data["mime"] = p.mime; }
  if (p.parent !== undefined) {
    only("parent", k === "J" || k === "T");
    if (rec.data["status"] === "closed") fail("conflict", `${rec.id} is closed`);
    if (p.parent !== null) { const bad = kindProblem(wikiKind, k, p.parent); if (bad) fail("invalid", bad); }
    data["parent"] = p.parent === null ? null : checkParent(repo, wiki, k, p.parent);
  }
  let built = wrap(() => buildData(k, rec.id, data));
  guardPrinted(d, wiki, [built.alias]);
  if (k !== "N" && built.alias && built.alias !== rec.alias) {
    // A baseline device whose alias is taken keeps the alias it had (a refusal would tell it the alias exists).
    if (!claimAlias(repo, d, wiki, built.alias, rec.id)) built = wrap(() => buildData(k, rec.id, { ...data, alias: rec.alias || null }));
  }
  const alias = k === "N" && built.alias !== rec.alias && repo.raw.aliasHolder(wiki, built.alias) ? "" : built.alias;
  const next: Normalized = {
    ...norm(rec), alias, data: k === "N" ? { ...built.data, alias: alias || null } : built.data,
    tags: nextTags,
  };
  if (contentKey({ ...next, updated: rec.updated }) === contentKey(norm(rec)) && alias === rec.alias) return { record: toWire(rec) };
  if (k === "D") {
    // The lean gate also covers a retag: a document may not move into a project another one holds,
    // and the focus and skill tags are the engine's, not a way around the gate.
    const was = projectOf(rec.tags);
    const now = projectOf(next.tags);
    if ((was === null) !== (now === null)) fail("invalid", "the focus and skill tags are reserved for the focus record and skill versions");
    if (now !== null && now !== was) {
      const held = projectHolder(repo, wiki, now, rec.uid);
      if (held) fail("conflict", refusal(held, now), { existing: held.id });
    }
  } else if (p.tags !== undefined && PROJECT_KINDS.includes(k)) {
    // A retag moves the `about` link with the project tag.
    next.links = projectLinks(repo, wiki, rec.links, projectKeyOf(next.tags), projectKeyOf(rec.tags));
  }
  next.updated = stamp(rec.updated);
  const saved = repo.save(wiki, next, d.name, rec);
  if ((k === "M" || k === "R" || k === "I") && !isBaseline(d)) lessonFromRow(repo.raw, wiki, saved, d.name);
  if (k === "D" && p.tags !== undefined && projectOf(saved.tags) && projectOf(saved.tags) !== projectOf(rec.tags)) backlinkProject(repo, wiki, saved, d.name);
  return { record: toWire(saved), ...(notice ? { notice } : {}) };
}

// --- ledger moves ---------------------------------------------------------------------------

function ledgerItem(repo: Repo, d: Device, wiki: string, ref: string): Stored {
  const rec = liveRecord(repo, wiki, ref);
  if (!isLedger(rec.kind)) fail("invalid", `${rec.id} is a ${TYPE_NAMES[rec.kind]}, not a ledger item`);
  // A baseline device works tasks; features and jobs are the operator's (closing one moves the focus).
  if (isBaseline(d) && rec.kind !== "T") denyBaseline(d);
  return rec;
}

function withData(rec: Stored, patch: Data): Normalized {
  return { ...norm(rec), data: { ...rec.data, ...patch }, updated: stamp(rec.updated) };
}

export function claim(repo: Repo, d: Device, wiki: string, ref: string): RecordWire {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  const rec = ledgerItem(repo, d, wiki, ref);
  if (rec.data["status"] === "closed") fail("conflict", `${rec.id} is closed`);
  return toWire(repo.save(wiki, withData(rec, { status: "claimed", claimed_by: d.name }), d.name, rec));
}

export function release(repo: Repo, d: Device, wiki: string, ref: string): RecordWire {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  const rec = ledgerItem(repo, d, wiki, ref);
  if (rec.data["status"] !== "claimed") return toWire(rec);
  return toWire(repo.save(wiki, withData(rec, { status: "open", claimed_by: null }), d.name, rec));
}

export function close(
  repo: Repo, d: Device, wiki: string, ref: string, reason: string, gitRef?: string,
): { record: RecordWire; closed: string[] } {
  needScope(d, "write");
  wikiFor(repo, d, wiki);
  if (!(CLOSE_REASONS as readonly string[]).includes(reason)) fail("invalid", `reason must be one of ${CLOSE_REASONS.join(", ")}`);
  const rec = ledgerItem(repo, d, wiki, ref);
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
  if (rec.kind === "F") onFeatureClosed(repo, wiki, rec.id, d.name);
  return { record: toWire(done), closed };
}

// --- delete ---------------------------------------------------------------------------------

export function deleteRecord(repo: Repo, d: Device, wiki: string, ref: string): RecordWire {
  needScope(d, "delete");
  denyBaseline(d, "this device never deletes");
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
