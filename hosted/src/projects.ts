// The lean gate: a project (or subproject) keeps one document. A focus record and a skill
// version are documents too but are not project documents, so they never count. The same rule
// runs in shapa/db.py (`add_doc`), and both sides refuse with the existing document's id.

import { fail } from "./errors.js";
import { focusRecord } from "./focus.js";
import { DOC_BODY_MAX, FOCUS_TAG, LIMITS, PROJECT_PREFIX, SKILL_TAG, cleanProject, projectOf, type Data, type Normalized } from "./records.js";
import type { Repo, Stored } from "./repo.js";
import { NAME_RE } from "./skills.js";

/** The id of the live project document of `key` in `wiki`, ignoring the record with `exceptUid`. */
export function projectHolder(repo: Repo, wiki: string, key: string, exceptUid = ""): { id: string; title: string } | null {
  if (key !== "") {
    // A named project is found by its tag in the relation index: the cost never grows with the wiki.
    const rows = repo.sql
      .exec(
        `SELECT r.id AS id, r.uid AS uid, json_extract(r.data, '$.title') AS title, r.tags AS tags FROM record_tags t
           JOIN records r ON r.wiki = t.wiki AND r.id = t.id
          WHERE t.wiki = ? AND t.tag = ? AND t.kind = 'D'${repo.vis("t.vis")} ORDER BY r.seq LIMIT 8`,
        wiki, PROJECT_PREFIX + key,
      ).toArray();
    for (const r of rows) {
      if (r["uid"] !== exceptUid && projectOf(JSON.parse(r["tags"] as string) as string[]) === key) return { id: r["id"] as string, title: String(r["title"] ?? "") };
    }
    return null;
  }
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
  const bad = projectTagProblem(tags);
  if (bad) fail("invalid", bad);
  const key = projectOf(tags);
  if (key === null) return null;
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

/** Why a `project:` tag is no project key, or null. A key is exactly what `cleanProject` makes of it:
 *  `project:Foo`, a zero-width or full-width lookalike and a key cut short by the tag limit would
 *  otherwise sit next to `project:foo` and slip past the one-document rule. `tags` is the raw list
 *  (whitespace is normalised as `cleanTag` does, but nothing is cut). */
export function projectTagProblem(tags: unknown): string | null {
  if (!Array.isArray(tags)) return null;
  for (const t of tags) {
    const tag = String(t ?? "").split(/\s+/).filter(Boolean).join(" ");
    if (!tag.startsWith(PROJECT_PREFIX)) continue;
    const key = tag.slice(PROJECT_PREFIX.length);
    if (!key || key !== key.toLowerCase() || !/^[a-z0-9][a-z0-9._/-]{0,79}$/.test(key) || PROJECT_PREFIX.length + key.length > LIMITS.tag) {
      return `invalid project tag ${JSON.stringify(tag.slice(0, 90))}: lowercase letters, digits and . _ / - only, one key as written`;
    }
  }
  return null;
}

/** A sync put of a document. Documents already stored are grandfathered: the lean gate stops the
 *  creation of a second document (create, merge, retag), not a put of one a client already holds,
 *  since the server cannot tell the two apart. The reserved tags (focus, skill) must carry what
 *  they promise, and a project tag must be a clean key. `rawTags` is the put's tag list as sent. */
export function syncGate(repo: Repo, wiki: string, n: Normalized, target: Stored | null, rawTags?: unknown): string | null {
  if (n.kind !== "D" || n.deleted) return null;
  const bad = projectTagProblem(rawTags ?? n.tags);
  if (bad) return bad;
  // A record is the focus record or a skill version from its first put to its last: a put may not add or
  // remove the reserved tags of a stored record (a focus record retagged `skill` would be an active
  // skill version), carry both, or change the focus record's tags.
  if (n.tags.includes(FOCUS_TAG) && n.tags.includes(SKILL_TAG)) return "a record is the focus record or a skill version, not both";
  if (target && !target.deleted && target.kind === "D") {
    const reserved = (ts: readonly string[]) => ts.filter((t) => t === FOCUS_TAG || t === SKILL_TAG).sort().join(",");
    if (reserved(n.tags) !== reserved(target.tags)) return "the focus and skill tags cannot be added to or removed from a stored record";
    if (target.tags.includes(FOCUS_TAG) && [...n.tags].sort().join("\n") !== [...target.tags].sort().join("\n")) return "the focus record's tags are fixed";
  }
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
  return null;
}
