// Skills as stored, versioned records, mirroring shapa/skills.py. A skill version is a document
// tagged `skill`, `skill:NAME`, `v:N` and `status:active|pending|previous`; the body is a SKILL.md
// (frontmatter with the name and a one-line description, then the text). The Worker reads them,
// and files a *pending* new version when a memory, rule or issue saved through REST or MCP names
// a skill; it never activates one (activating is a tag change made through sync or PATCH).

import { type Device, needScope, needWiki, randomHex } from "./devices.js";
import { fail } from "./errors.js";
import { DOC_BODY_MAX, SKILL_TAG, buildData, normTags, nowIso, oneLine, type Normalized } from "./records.js";
import { Repo, type Stored } from "./repo.js";

export const NAME_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
export const DESCRIPTION_MAX = 1024;
const LESSON_BODY_MAX = 1500;
const FRONT_RE = /^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/;
const STATUSES = ["active", "pending", "previous"] as const;
export type SkillStatus = (typeof STATUSES)[number];

export interface SkillVersion {
  name: string;
  version: number;
  status: SkillStatus;
  description: string;
  body: string;
  id: string;
  created: string;
}

export function splitSkill(text: string): { fields: Record<string, string>; body: string } {
  const m = FRONT_RE.exec(text);
  if (!m) return { fields: {}, body: text.trim() };
  const fields: Record<string, string> = {};
  for (const line of m[1]!.split(/\r?\n/)) {
    const i = line.indexOf(":");
    if (i > 0 && /^[A-Za-z_][\w-]*$/.test(line.slice(0, i).trim())) fields[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return { fields, body: text.slice(m[0].length).trim() };
}

const compose = (name: string, description: string, body: string): string => `---\nname: ${name}\ndescription: ${description}\n---\n${body}`;

export function toVersion(s: Stored): SkillVersion | null {
  if (s.deleted || s.kind !== "D" || !s.tags.includes(SKILL_TAG)) return null;
  const tag = (prefix: string) => [...s.tags].sort().find((t) => t.startsWith(prefix))?.slice(prefix.length) ?? "";
  const name = tag("skill:");
  const version = tag("v:");
  const status = tag("status:") || "pending";
  if (!NAME_RE.test(name) || !/^\d{1,6}$/.test(version) || !(STATUSES as readonly string[]).includes(status)) return null;
  const { fields, body } = splitSkill(String(s.data["body"] ?? ""));
  return { name, version: Number(version), status: status as SkillStatus, description: oneLine(fields["description"] ?? "", DESCRIPTION_MAX), body, id: s.id, created: s.created };
}

export function versionsOf(repo: Repo, wiki: string, name?: string): SkillVersion[] {
  const out: SkillVersion[] = [];
  for (const s of repo.live(wiki, ["D"])) {
    const v = toVersion(s);
    if (v && (name === undefined || v.name === name)) out.push(v);
  }
  return out.sort((a, b) => a.version - b.version || (a.created < b.created ? -1 : 1));
}

/** The active version (the highest if a race left two), else the newest. */
function current(vs: SkillVersion[]): SkillVersion | undefined {
  const active = vs.filter((v) => v.status === "active");
  const pool = active.length ? active : vs;
  return pool[pool.length - 1];
}

export interface SkillSummary { name: string; description: string; active: number | null; pending: number[]; versions: number }

export function summaries(repo: Repo, wiki: string): SkillSummary[] {
  const by = new Map<string, SkillVersion[]>();
  for (const v of versionsOf(repo, wiki)) by.set(v.name, [...(by.get(v.name) ?? []), v]);
  return [...by.entries()].sort(([a], [b]) => (a < b ? -1 : 1)).map(([name, vs]) => {
    const active = vs.filter((v) => v.status === "active");
    return {
      name, description: current(vs)?.description ?? "", active: active.length ? active[active.length - 1]!.version : null,
      pending: vs.filter((v) => v.status === "pending").map((v) => v.version), versions: vs.length,
    };
  });
}

export function listSkills(repo: Repo, d: Device, wiki: string): SkillSummary[] {
  needScope(d, "read");
  needWiki(d, wiki);
  if (!repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}`);
  return summaries(repo, wiki);
}

export function getSkill(repo: Repo, d: Device, wiki: string, name: string, version?: number): SkillVersion {
  needScope(d, "read");
  needWiki(d, wiki);
  if (!repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}`);
  const vs = versionsOf(repo, wiki, name);
  const v = version === undefined ? current(vs) : vs.find((x) => x.version === version);
  if (!v) fail("not_found", `no skill ${name}${version === undefined ? "" : ` version ${version}`} in ${wiki}`);
  return v;
}

// --- lessons from rows ------------------------------------------------------------------------

function quote(text: string, limit: number): string {
  return text.trim().slice(0, limit).split(/\r?\n/).filter((l) => l.trim()).map((l) => `> ${l}`).join("\n");
}

/** Skills a row names with a `skill:NAME` tag (the REST and MCP writes carry no links). */
function named(tags: readonly string[]): string[] {
  const out: string[] = [];
  for (const t of tags) {
    if (!t.startsWith("skill:")) continue;
    const n = t.slice("skill:".length);
    if (NAME_RE.test(n) && !out.includes(n)) out.push(n);
  }
  return out;
}

/** File a pending version for each skill the row names (once per row): the skill's latest
 *  version plus the lesson, every lesson line quoted so it can open no heading or frontmatter. */
export function lessonFromRow(repo: Repo, wiki: string, row: Stored, device: string): number {
  if (row.kind !== "M" && row.kind !== "R" && row.kind !== "I") return 0;
  let filed = 0;
  for (const name of named(row.tags)) {
    const vs = versionsOf(repo, wiki, name);
    const base = vs[vs.length - 1];
    if (!base) continue;
    const marker = `[${row.id}]`;
    if (vs.some((v) => v.body.includes(marker))) continue;
    const kind = row.kind === "M" ? "memory" : row.kind === "R" ? "rule" : "issue";
    let lesson = `\n\n## Lesson ${marker} (${kind}, pending review)\n\n${quote(String(row.data["summary"] ?? ""), 200)}`;
    const body = String(row.data["body"] ?? "");
    if (body.trim()) lesson += `\n>\n${quote(body, LESSON_BODY_MAX)}`;
    const text = compose(name, base.description, base.body + lesson);
    if (new TextEncoder().encode(text).length > DOC_BODY_MAX) continue;
    const number = vs[vs.length - 1]!.version + 1;
    const id = repo.allocId(wiki, "D");
    const alias = `skill:${name}.v${number}`;
    const built = buildData("D", id, { title: `${name} v${number}`, body: text, alias: repo.aliasHolder(wiki, alias) ? "" : alias, mime: "text/markdown", source: `lesson:${row.id}` });
    const ts = nowIso();
    const n: Normalized = {
      id, uid: randomHex(16), kind: "D", created: ts, updated: ts, deleted: false, alias: built.alias, data: built.data,
      tags: normTags([SKILL_TAG, `skill:${name}`, `v:${number}`, "status:pending"]), links: [],
    };
    repo.save(wiki, n, device, null);
    filed += 1;
  }
  return filed;
}
