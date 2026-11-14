// The lean gate: a project (or subproject) keeps one document. A focus record and a skill
// version are documents too but are not project documents, so they never count. The same rule
// runs in shapa/db.py (`add_doc`), and both sides refuse with the existing document's id.

import { fail } from "./errors.js";
import { focusRecord } from "./focus.js";
import { DOC_BODY_MAX, FOCUS_TAG, PROJECT_PREFIX, SKILL_TAG, cleanProject, projectOf, type Data, type Normalized } from "./records.js";
import type { Repo, Stored } from "./repo.js";
import { NAME_RE } from "./skills.js";

/** The id of the live project document of `key` in `wiki`, ignoring the record with `exceptUid`. */
export function projectHolder(repo: Repo, wiki: string, key: string, exceptUid = ""): { id: string; title: string } | null {
  for (const d of repo.docsLight(wiki)) {
    if (d.uid !== exceptUid && projectOf(d.tags) === key) return { id: d.id, title: d.title };
  }
  return null;
}

export function refusal(holder: { id: string; title: string }, key: string): string {
  const what = key ? `project ${JSON.stringify(key)}` : "this wiki's project";
  return `${what} already has document ${holder.id} (${holder.title}): update it (PATCH) or create with merge: true to append a section; a project keeps one document`;
}

/** The tags a new document gets: `project` (or an explicit `project:` tag) becomes the one project tag. */
export function documentTags(tags: string[], project: string | undefined): string[] {
  const key = cleanProject(project);
  const rest = key ? tags.filter((t) => !t.startsWith(PROJECT_PREFIX)) : tags;
  return key ? [...rest, PROJECT_PREFIX + key] : rest;
}

/** Refuse a new document whose project already has one (409 naming it), or return that one to merge into. */
export function gateNewDocument(repo: Repo, wiki: string, tags: string[], merge: boolean): Stored | null {
  const key = projectOf(tags);
  if (key === null) return null;
  if (key !== cleanProject(key)) fail("invalid", `invalid project tag ${PROJECT_PREFIX}${key}`);
  const held = projectHolder(repo, wiki, key);
  if (!held) return null;
  if (!merge) fail("conflict", refusal(held, key), { existing: held.id });
  return repo.get(wiki, held.id);
}

/** `existing` plus `title`/`body` as a section, within the record limit. */
export function mergedBody(existing: Data, title: string, body: string): string {
  const text = `${String(existing["body"] ?? "")}\n\n## ${title}\n\n${body}`.trim();
  if (new TextEncoder().encode(text).length > DOC_BODY_MAX) fail("too_large", `merging would make the document larger than ${DOC_BODY_MAX} bytes`);
  return text;
}

/** A sync put of a document: a different uid may not take a project another document holds, and
 *  the reserved tags (focus, skill) must carry what they promise, so they are no way around the gate. */
export function syncGate(repo: Repo, wiki: string, n: Normalized, target: Stored | null): string | null {
  if (n.kind !== "D" || n.deleted) return null;
  if (n.tags.includes(SKILL_TAG)) {
    const t = (p: string) => n.tags.find((x) => x.startsWith(p))?.slice(p.length) ?? "";
    if (!NAME_RE.test(t("skill:")) || !/^\d{1,6}$/.test(t("v:")) || !["active", "pending", "previous"].includes(t("status:"))) {
      return "a skill version needs the tags skill:NAME, v:N and status:active|pending|previous";
    }
  }
  if (n.tags.includes(FOCUS_TAG)) {
    let ok = false;
    try {
      const v: unknown = JSON.parse(String(n.data["body"] ?? ""));
      ok = typeof v === "object" && v !== null && !Array.isArray(v);
    } catch { /* not JSON */ }
    if (!ok) return "a focus record's body is a JSON object";
    const held = focusRecord(repo, wiki);
    if (held && held.uid !== n.uid) return `this wiki already has its focus record ${held.id}`;
  }
  const key = projectOf(n.tags);
  if (key === null) return null;
  if (target && !target.deleted && projectOf(target.tags) === key) return null; // unchanged project
  const held = projectHolder(repo, wiki, key, n.uid);
  return held ? refusal(held, key) : null;
}
