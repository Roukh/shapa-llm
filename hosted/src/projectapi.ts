// Projects as a surface: `project:KEY` is the relation, the project's one document (the lean gate)
// is its file, and every task, idea, decision, rule, memory and skill version that carries the tag
// is linked to it (an `about` link, kept in step on create, retag, sync put and when the document
// is created later). Create, append an item, read one project, list projects.

import { type Device, needScope, needWiki, randomHex } from "./devices.js";
import { fail } from "./errors.js";
import { gateNewDocument, projectHolder } from "./projects.js";
import { appendItem, isSection, parseSections, PROJECT_TEMPLATE, SECTIONS, type Section } from "./projectfile.js";
import { Invalid, LIMITS, PROJECT_PREFIX, buildData, cleanProject, cmp, nowIso, normLinks, projectOf, type Link, type Normalized } from "./records.js";
import { projectKeys, tagBuckets, taggedIds } from "./relations.js";
import { Repo, stampAfter, toNormalized, type Brief, type Stored } from "./repo.js";
import { baselineWrites } from "./reach.js";
import { headOf, splitSkill } from "./skills.js";
import { stageOf } from "./tags.js";

export const LIST_CAP = 50;
export const PROJECTS_CAP = 200;
export const PROJECTS_PER_WIKI = 1000;
const BACKLINK_CAP = 500;

/** Why a string is no project key, or null: exactly what `cleanProject` makes of it, and short enough for its tag. */
export function projectKeyProblem(key: unknown): string | null {
  if (typeof key !== "string" || !key) return "a project key is required";
  try {
    if (cleanProject(key) !== key) return `invalid project key ${JSON.stringify(key.slice(0, 90))}: lowercase letters, digits and . _ / - only`;
  } catch (e) {
    if (e instanceof Invalid) return `invalid project key ${JSON.stringify(key.slice(0, 90))}: lowercase letters, digits and . _ / - only`;
    throw e;
  }
  if (PROJECT_PREFIX.length + key.length > LIMITS.tag) return `a project key is at most ${LIMITS.tag - PROJECT_PREFIX.length} characters`;
  return null;
}

function validKey(key: string): string {
  const bad = projectKeyProblem(key);
  if (bad) fail("invalid", bad);
  return key;
}

function usable(repo: Repo, d: Device, wiki: string, scope: "read" | "write"): void {
  needScope(d, scope);
  needWiki(d, wiki);
  if (!repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}`);
}

// --- the about link ------------------------------------------------------------------------------

/** `links` with the `about` link to the project's document in step with `key`: the old project's link out, the new one in. */
export function projectLinks(repo: Repo, wiki: string, links: Link[], key: string, oldKey: string): Link[] {
  let out = links;
  if (oldKey && oldKey !== key) {
    const was = projectHolder(repo.raw, wiki, oldKey);
    if (was) out = out.filter(([dst, rel]) => !(dst === was.id && rel === "about"));
  }
  if (key) {
    const doc = projectHolder(repo.raw, wiki, key);
    if (doc && !out.some(([dst, rel]) => dst === doc.id && rel === "about")) out = [...out, [doc.id, "about"]];
  }
  return out === links ? links : normLinks(out);
}

/** The buckets that carry an `about` link to their project's document: tasks, memories (ideas and decisions
 *  included), rules, issues and skill versions. Never the document itself, a feature or a job. */
const LINKED_BUCKETS = ["T", "M", "E", "C", "R", "I", "S"];

/** A project document now exists (created, or a retag moved a document into the project): link the records that
 *  already carry the tag. At most BACKLINK_CAP, oldest first, one write each. A baseline device links only the
 *  records it can read and edit (tasks, ideas, decisions): a write onto any other record would put its name on a
 *  record it may not see and move that record's revision, so the rest are linked when a full device next writes
 *  them (project reads resolve membership by tag, so they stay complete either way). */
export function backlinkProject(repo: Repo, wiki: string, doc: Stored, device: string): number {
  const key = projectOf(doc.tags);
  if (!key) return 0;
  const ids = repo.sql
    .exec(
      `SELECT id FROM record_tags WHERE wiki = ? AND tag = ? AND kind IN (${LINKED_BUCKETS.map(() => "?").join(",")})${repo.vis("vis")} ORDER BY seq LIMIT ?`,
      wiki, PROJECT_PREFIX + key, ...LINKED_BUCKETS, BACKLINK_CAP,
    )
    .toArray().map((r) => r["id"] as string);
  let n = 0;
  for (const id of ids) {
    const rec = repo.raw.get(wiki, id);
    if (!rec || rec.deleted || rec.links.some(([dst, rel]) => dst === doc.id && rel === "about")) continue;
    if (repo.baseline && !baselineWrites(rec.kind, rec.tags)) continue;
    const next: Normalized = { ...toNormalized(rec), links: normLinks([...rec.links, [doc.id, "about"]]), updated: stampAfter(rec.updated) };
    repo.raw.save(wiki, next, device, rec);
    n += 1;
  }
  return n;
}

// --- create, append -------------------------------------------------------------------------------

export interface ProjectDoc {
  id: string;
  sections: Record<Section, string[]>;
}
export interface ProjectCard {
  key: string;
  wiki: string;
  title: string;
  document: ProjectDoc | null;
}

function cardOf(wiki: string, key: string, doc: Stored | null): ProjectCard {
  return {
    key, wiki, title: doc ? String(doc.data["title"] ?? key) : key,
    document: doc ? { id: doc.id, sections: parseSections(String(doc.data["body"] ?? "")) } : null,
  };
}

function documentOf(repo: Repo, wiki: string, key: string): Stored | null {
  const held = projectHolder(repo, wiki, key);
  return held ? repo.get(wiki, held.id) : null;
}

/** Create a project's file from the template (idempotent: an existing one is returned as it is). */
export function newProject(repo: Repo, d: Device, key: string, wiki: string, title?: string): { project: ProjectCard; created: boolean } {
  usable(repo, d, wiki, "write");
  validKey(key);
  const held = documentOf(repo, wiki, key);
  if (held) return { project: cardOf(wiki, key, held), created: false };
  // Every session start lists a wiki's projects: a store of thousands of them is spam, not a plan. A baseline device
  // counts only the projects it can see, so the cap never tells it about a hidden one.
  if (projectKeys(repo.sql, repo.baseline, wiki).length >= PROJECTS_PER_WIKI) fail("too_large", `a wiki holds at most ${PROJECTS_PER_WIKI} projects`);
  const id = repo.allocId(wiki, "D");
  let built;
  try {
    built = buildData("D", id, { title: title && title.trim() ? title : key, body: PROJECT_TEMPLATE, alias: null, mime: "text/markdown", source: "project" });
  } catch (e) {
    if (e instanceof Invalid) fail("invalid", e.message);
    throw e;
  }
  const tags = [PROJECT_PREFIX + key];
  gateNewDocument(repo, wiki, tags, false); // the lean gate runs for the project file too
  const ts = nowIso();
  const doc = repo.save(wiki, { id, uid: randomHex(16), kind: "D", created: ts, updated: ts, deleted: false, alias: "", data: built.data, tags, links: [] }, d.name, null);
  backlinkProject(repo, wiki, doc, d.name);
  return { project: cardOf(wiki, key, doc), created: true };
}

/** Append one numbered item to a section of a project's file. */
export function addItem(repo: Repo, d: Device, key: string, wiki: string, section: string, text: string): { project: ProjectCard; section: Section; n: number; added: boolean } {
  usable(repo, d, wiki, "write");
  validKey(key);
  if (!isSection(section)) fail("invalid", `section must be one of ${SECTIONS.join(", ")}`);
  const doc = documentOf(repo, wiki, key);
  if (!doc) fail("not_found", `no project ${key} in ${wiki}: create it first (PUT /v1/projects/${encodeURIComponent(key)})`);
  let r;
  try {
    r = appendItem(String(doc.data["body"] ?? ""), section, text);
  } catch (e) {
    if (e instanceof Invalid) fail(e.message.includes("bytes") ? "too_large" : "invalid", e.message);
    throw e;
  }
  if (!r.added) return { project: cardOf(wiki, key, doc), section, n: r.n, added: false };
  const saved = repo.save(wiki, { ...toNormalized(doc), data: { ...doc.data, body: r.body }, updated: stampAfter(doc.updated) }, d.name, doc);
  return { project: cardOf(wiki, key, saved), section, n: r.n, added: true };
}

// --- read one, list -------------------------------------------------------------------------------

export interface Summary { id: string; summary: string }
export interface ProjectOut extends ProjectCard {
  tasks: { id: string; title: string; status: string; owner: "human" | "agent" | "" }[];
  ideas: Summary[];
  decisions: Summary[];
  rules: (Summary & { stage: number | null })[];
  skills: { id: string; name: string; description: string; stage: number | null }[];
  memories: Summary[];
  features: { id: string; title: string }[];
  /** How many more of each list exist past its 50. */
  more: Record<"tasks" | "ideas" | "decisions" | "rules" | "skills" | "memories" | "features", number>;
}

const SKILL_HEAD = 2000;

/** Headlines of the records with these ids (never their bodies: a record may be 1 MB and a list holds fifty). */
function loadAll(repo: Repo, wiki: string, ids: string[], head = 0): Brief[] {
  return ids.map((id) => repo.brief(wiki, id, head)).filter((s): s is Brief => s !== null);
}

/** One project: its file and every record of the wiki that carries `project:KEY`, plus the open features of the repo wiki named KEY. */
export function showProject(repo: Repo, d: Device, key: string, wiki: string): ProjectOut {
  usable(repo, d, wiki, "read");
  validKey(key);
  const doc = documentOf(repo, wiki, key);
  const tag = PROJECT_PREFIX + key;
  const counts = tagBuckets(repo.sql, repo.baseline, wiki, tag);
  if (!doc && Object.keys(counts).filter((b) => b !== "O").length === 0) fail("not_found", `no project ${key} in ${wiki}`);
  const list = (bucket: string, head = 0) => loadAll(repo, wiki, taggedIds(repo.sql, repo.baseline, wiki, tag, bucket, LIST_CAP), head);
  const rest = (bucket: string, shown: number) => Math.max(0, (counts[bucket] ?? 0) - shown);
  const tasks = list("T");
  const ideas = list("E");
  const decisions = list("C");
  const rules = list("R");
  const skills = list("S", SKILL_HEAD);
  const memories = list("M");
  let features: { id: string; title: string }[] = [];
  let moreFeatures = 0;
  if (key !== wiki && repo.wiki(key)?.kind === "repo" && (d.wikis === null || d.wikis.includes(key))) {
    const open = repo.openFeatures(key, LIST_CAP + 1);
    features = open.slice(0, LIST_CAP).map((f) => ({ id: f.id, title: String(f.data["title"] ?? "") }));
    moreFeatures = Math.max(0, open.length - LIST_CAP);
  }
  const card = cardOf(wiki, key, doc);
  return {
    ...card,
    tasks: tasks.map((t) => ({
      id: t.id, title: t.text, status: t.status || "open",
      owner: t.tags.includes("human_task") ? "human" : t.tags.includes("agent_task") ? "agent" : "",
    })),
    ideas: ideas.map((s) => ({ id: s.id, summary: s.text })),
    decisions: decisions.map((s) => ({ id: s.id, summary: s.text })),
    rules: rules.map((s) => ({ id: s.id, summary: s.text, stage: stageOf(s.tags) })),
    skills: skills.flatMap((s) => {
      const h = headOf(s.tags);
      return h ? [{ id: s.id, name: h.name, description: String(splitSkill(s.head).fields["description"] ?? "").slice(0, 300), stage: stageOf(s.tags) }] : [];
    }),
    memories: memories.map((s) => ({ id: s.id, summary: s.text })),
    features,
    more: {
      tasks: rest("T", tasks.length), ideas: rest("E", ideas.length), decisions: rest("C", decisions.length), rules: rest("R", rules.length),
      skills: rest("S", skills.length), memories: rest("M", memories.length), features: moreFeatures,
    },
  };
}

export interface ProjectRow { key: string; title: string; open_tasks: number }

/** The projects of a wiki: key, title and open tasks, the busiest first; at most PROJECTS_CAP, with the total. */
export function listProjects(repo: Repo, d: Device, wiki: string, limit = PROJECTS_CAP): { projects: ProjectRow[]; total: number } {
  usable(repo, d, wiki, "read");
  const keys = projectKeys(repo.sql, repo.baseline, wiki).sort((a, b) => b.open - a.open || cmp(a.key, b.key));
  const rows = keys.slice(0, Math.min(limit, PROJECTS_CAP)).map(({ key, open }) => {
    const doc = projectHolder(repo, wiki, key);
    return { key, title: doc?.title || key, open_tasks: open };
  });
  return { projects: rows, total: keys.length };
}

