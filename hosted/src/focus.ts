// The northstar and the three big fires of a wiki scope, mirroring shapa/focus.py.
//
// One focus record per wiki: a document (alias "focus", tag "focus", mime application/json)
// whose body is {"fires":[{"feature":"F22","text":""},null,null],"northstar":"...","v":1}. Three
// slots, ranked first to last; a slot is empty, points at a ledger feature, or is free text for a
// fire that spans repos. A feature's explicit rank is a `rank:N` tag on the feature (lower first).
// Closing a fire's feature frees its slot, the other fires move up, and the best open
// top-level feature that is not a fire (explicit rank, else the oldest) takes the last slot.

import { type Device, needScope, needWiki, randomHex } from "./devices.js";
import { fail } from "./errors.js";
import {
  FIRE_TEXT_MAX, FOCUS_ALIAS, FOCUS_TAG, MAX_FIRES, NORTHSTAR_MAX, RANK_PREFIX, buildData, kindOfId, nowIso, oneLine,
  type Normalized,
} from "./records.js";
import { redact } from "./redact.js";
import { Repo, type Stored } from "./repo.js";

export interface Fire { feature: string; text: string }
export interface FocusState { northstar: string; fires: (Fire | null)[] }
export interface TreeTask { id: string; title: string; status: string }
export interface TreeJob { id: string; title: string; status: string; tasks: TreeTask[] }
export interface FireTree { slot: number; feature: string; text: string; title: string; status: string; jobs: TreeJob[]; files: string[]; boxes: string[] }
export interface Candidate { id: string; title: string; rank: number | null }

const FILES_SHOWN = 8;
const FILE_RE = /(?<![\w/.:-])((?:[A-Za-z0-9_.-]+\/)+[A-Za-z0-9_.-]*\.[A-Za-z0-9]{1,6}|[A-Za-z0-9_-]+\.(?:py|ts|tsx|js|md|json|jsonc|toml|sh|yml|yaml|sql))(?![\w/-])/g;
const BOX_RE = /\[\[([A-Za-z0-9][A-Za-z0-9._-]{0,60})\]\]/g;

/** One redacted line of at most `limit` characters: a record can never add a line or heading of its own. */
export const lineOf = (text: unknown, limit: number): string => oneLine(redact(String(text ?? "")), limit);

const emptyFocus = (): FocusState => ({ northstar: "", fires: [null, null, null] });

export function parseFocus(body: unknown): FocusState {
  const out = emptyFocus();
  let data: unknown;
  try {
    data = JSON.parse(String(body ?? ""));
  } catch {
    return out;
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) return out;
  const d = data as Record<string, unknown>;
  if (typeof d["northstar"] === "string") out.northstar = lineOf(d["northstar"], NORTHSTAR_MAX);
  const raw = Array.isArray(d["fires"]) ? d["fires"].slice(0, MAX_FIRES) : [];
  raw.forEach((item, i) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return;
    const f = item as Record<string, unknown>;
    const feature = typeof f["feature"] === "string" && /^F[1-9][0-9]*$/.test(f["feature"]) ? f["feature"] : "";
    const text = typeof f["text"] === "string" ? lineOf(f["text"], FIRE_TEXT_MAX) : "";
    if (feature || text) out.fires[i] = { feature, text };
  });
  return out;
}

export function dumpFocus(f: FocusState): string {
  const fires = [...f.fires, null, null, null].slice(0, MAX_FIRES).map((x) => (x === null ? null : { feature: x.feature, text: x.text }));
  return JSON.stringify({ fires, northstar: f.northstar, v: 1 });
}

export function focusRecord(repo: Repo, wiki: string): Stored | null {
  const id = repo.aliasHolder(wiki, FOCUS_ALIAS);
  const rec = id ? repo.get(wiki, id) : null;
  return rec && !rec.deleted && rec.kind === "D" && rec.tags.includes(FOCUS_TAG) ? rec : null;
}

export function loadFocus(repo: Repo, wiki: string): FocusState {
  const rec = focusRecord(repo, wiki);
  return rec ? parseFocus(rec.data["body"]) : emptyFocus();
}

function stamp(prev?: string): string {
  const now = nowIso();
  return prev && prev > now ? prev : now;
}

export function saveFocus(repo: Repo, wiki: string, device: string, f: FocusState): void {
  const body = dumpFocus(f);
  const rec = focusRecord(repo, wiki);
  if (rec) {
    if (rec.data["body"] === body) return;
    const n: Normalized = {
      id: rec.id, uid: rec.uid, kind: "D", created: rec.created, updated: stamp(rec.updated), deleted: false, alias: rec.alias,
      data: { ...rec.data, body }, tags: rec.tags, links: rec.links,
    };
    repo.save(wiki, n, device, rec);
    return;
  }
  if (repo.aliasHolder(wiki, FOCUS_ALIAS)) fail("conflict", `another record already uses the alias ${FOCUS_ALIAS}`);
  const id = repo.allocId(wiki, "D");
  const built = buildData("D", id, { title: "Focus", body, alias: FOCUS_ALIAS, mime: "application/json", source: "focus" });
  const ts = nowIso();
  repo.save(wiki, { id, uid: randomHex(16), kind: "D", created: ts, updated: ts, deleted: false, alias: built.alias, data: built.data, tags: [FOCUS_TAG], links: [] }, device, null);
}

// --- features and candidates ----------------------------------------------------------------

export function rankOf(tags: readonly string[]): number | null {
  for (const t of tags) {
    if (t.startsWith(RANK_PREFIX) && /^\d{1,3}$/.test(t.slice(RANK_PREFIX.length))) return Number(t.slice(RANK_PREFIX.length));
  }
  return null;
}

function openFeature(repo: Repo, wiki: string, ref: string): Stored {
  const s = repo.find(wiki, ref);
  if (!s || s.kind !== "F") fail("invalid", `${JSON.stringify(ref)} is not a feature in this wiki`);
  if (s.data["status"] === "closed") fail("conflict", `${s.id} is closed`);
  return s;
}

/** Open top-level features that are not a fire, best first: an explicit rank (lowest first), then the oldest. */
export function candidates(repo: Repo, wiki: string, f: FocusState = loadFocus(repo, wiki)): Candidate[] {
  const taken = new Set(f.fires.filter((x): x is Fire => x !== null && x.feature !== "").map((x) => x.feature));
  const live = repo.live(wiki, ["F"]).filter((s) => s.data["status"] !== "closed" && !taken.has(s.id));
  const keyed = live.map((s) => ({ s, rank: rankOf(s.tags) }));
  keyed.sort((a, b) =>
    (a.rank === null ? 1 : 0) - (b.rank === null ? 1 : 0) || (a.rank ?? 0) - (b.rank ?? 0) ||
    (a.s.created < b.s.created ? -1 : a.s.created > b.s.created ? 1 : 0) || Number(a.s.id.slice(1)) - Number(b.s.id.slice(1)));
  return keyed.map(({ s, rank }) => ({ id: s.id, title: String(s.data["title"] ?? ""), rank }));
}

function mentions(texts: string[]): { files: string[]; boxes: string[] } {
  const files: string[] = [];
  const boxes: string[] = [];
  for (const t of texts) {
    for (const m of t.matchAll(FILE_RE)) if (!files.includes(m[1]!)) files.push(m[1]!);
    for (const m of t.matchAll(BOX_RE)) if (!boxes.includes(m[1]!)) boxes.push(m[1]!);
  }
  return { files: files.slice(0, FILES_SHOWN), boxes: boxes.slice(0, FILES_SHOWN) };
}

/** A fire's feature, its open jobs and tasks, and the files and arch boxes their text names. */
export function fireTree(repo: Repo, wiki: string, fire: Fire, slot: number): FireTree {
  if (!fire.feature) return { slot, feature: "", text: fire.text, title: fire.text, status: "", jobs: [], files: [], boxes: [] };
  const item = repo.get(wiki, fire.feature);
  if (!item || item.deleted || item.kind !== "F") {
    return { slot, feature: fire.feature, text: fire.text, title: fire.text || fire.feature, status: "missing", jobs: [], files: [], boxes: [] };
  }
  const texts = [String(item.data["title"] ?? ""), String(item.data["body"] ?? "")];
  const jobs: TreeJob[] = [];
  for (const job of repo.children(wiki, item.id)) {
    if (job.data["status"] === "closed") continue;
    const tasks = repo.children(wiki, job.id).filter((t) => t.data["status"] !== "closed");
    texts.push(String(job.data["title"] ?? ""), String(job.data["body"] ?? ""));
    for (const t of tasks) texts.push(String(t.data["title"] ?? ""), String(t.data["body"] ?? ""));
    jobs.push({
      id: job.id, title: String(job.data["title"] ?? ""), status: String(job.data["status"] ?? "open"),
      tasks: tasks.map((t) => ({ id: t.id, title: String(t.data["title"] ?? ""), status: String(t.data["status"] ?? "open") })),
    });
  }
  return { slot, feature: item.id, text: fire.text, title: String(item.data["title"] ?? ""), status: String(item.data["status"] ?? "open"), jobs, ...mentions(texts) };
}

// --- reading --------------------------------------------------------------------------------

function readable(repo: Repo, d: Device, wiki: string): void {
  needScope(d, "read");
  needWiki(d, wiki);
  if (!repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}`);
}

export interface FocusView {
  wiki: string;
  northstar: string;
  fires: (FireTree | null)[];
  candidates: Candidate[];
}

export function focusView(repo: Repo, wiki: string): FocusView {
  const f = loadFocus(repo, wiki);
  const fires = f.fires.map((x, i) => {
    if (x === null) return null;
    const t = fireTree(repo, wiki, x, i + 1);
    return x.feature && (t.status === "closed" || t.status === "missing") ? null : t; // a closed feature is not shown
  });
  return { wiki, northstar: f.northstar, fires, candidates: candidates(repo, wiki, f).slice(0, 20) };
}

export function getFocus(repo: Repo, d: Device, wiki: string): FocusView {
  readable(repo, d, wiki);
  return focusView(repo, wiki);
}

// --- writing --------------------------------------------------------------------------------

function writable(repo: Repo, d: Device, wiki: string): void {
  needScope(d, "write");
  needWiki(d, wiki);
  if (!repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}; register it with PUT /v1/wikis/${wiki}`);
}

function checkFire(repo: Repo, wiki: string, x: { feature?: string | undefined; text?: string | undefined }, others: (Fire | null)[]): Fire {
  if (x.feature) {
    if (repo.wiki(wiki)?.kind === "global") fail("invalid", "the global wiki has no ledger: a global fire is free text");
    const item = openFeature(repo, wiki, x.feature);
    if (others.some((o) => o && o.feature === item.id)) fail("conflict", `${item.id} is already a fire`);
    return { feature: item.id, text: "" };
  }
  const text = lineOf(x.text, FIRE_TEXT_MAX);
  if (!text) fail("invalid", "a fire needs a feature or text");
  return { feature: "", text };
}

export function setFocus(
  repo: Repo, d: Device, wiki: string,
  body: { northstar?: string | undefined; fires?: ({ feature?: string | undefined; text?: string | undefined } | null)[] | undefined },
): FocusView {
  writable(repo, d, wiki);
  if (body.northstar === undefined && body.fires === undefined) fail("invalid", "give a northstar, fires, or both");
  const f = loadFocus(repo, wiki);
  if (body.northstar !== undefined) {
    const text = lineOf(body.northstar, NORTHSTAR_MAX);
    if (!text) fail("invalid", "the northstar needs text");
    f.northstar = text;
  }
  if (body.fires !== undefined) {
    if (body.fires.length > MAX_FIRES) fail("invalid", "there are three fires");
    const next: (Fire | null)[] = [];
    for (const x of body.fires) next.push(x === null ? null : checkFire(repo, wiki, x, next));
    while (next.length < MAX_FIRES) next.push(null);
    f.fires = next;
  }
  saveFocus(repo, wiki, d.name, f);
  return focusView(repo, wiki);
}

export function promote(repo: Repo, d: Device, wiki: string, body: { feature?: string | undefined; slot?: number | undefined }): FocusView {
  writable(repo, d, wiki);
  const f = loadFocus(repo, wiki);
  let slot: number;
  if (body.slot !== undefined) {
    slot = body.slot;
  } else {
    const free = f.fires.findIndex((x) => x === null);
    if (free < 0) fail("conflict", "no free fire slot: pass slot to replace one, or clear one");
    slot = free + 1;
  }
  if (slot < 1 || slot > MAX_FIRES) fail("invalid", "there are three fires");
  let id: string;
  if (body.feature) {
    id = openFeature(repo, wiki, body.feature).id;
  } else {
    const next = candidates(repo, wiki, f)[0];
    if (!next) fail("not_found", "no open feature is waiting to be promoted");
    id = next.id;
  }
  if (f.fires.some((x, i) => x && x.feature === id && i !== slot - 1)) fail("conflict", `${id} is already a fire`);
  f.fires[slot - 1] = { feature: id, text: "" };
  saveFocus(repo, wiki, d.name, f);
  return focusView(repo, wiki);
}

/** The close hook: free the closed feature's slot (the rest move up) and promote the next candidate. */
export function onFeatureClosed(repo: Repo, wiki: string, featureId: string, device: string): void {
  if (kindOfId(featureId) !== "F") return;
  const f = loadFocus(repo, wiki);
  if (!f.fires.some((x) => x && x.feature === featureId)) return;
  const kept = f.fires.filter((x): x is Fire => x !== null && x.feature !== featureId);
  const freed = f.fires.filter((x) => x !== null).length - kept.length;
  const next: (Fire | null)[] = [...kept, ...Array<Fire | null>(MAX_FIRES - kept.length).fill(null)];
  const state: FocusState = { northstar: f.northstar, fires: next };
  for (let i = 0; i < freed; i++) {
    const c = candidates(repo, wiki, state)[0];
    const free = state.fires.findIndex((x) => x === null);
    if (!c || free < 0) break;
    state.fires[free] = { feature: c.id, text: "" };
  }
  saveFocus(repo, wiki, device, state);
}
