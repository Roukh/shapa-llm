// Tags are the relations (hosted.md, "Tags, stages and relations"): the system-tag list, the
// stage tags, the project/owner/decision/idea tags, the bucket a record is counted under, and the
// one rule for what a `baseline` device may see. One source of truth for each constant: the
// relation index (relations.ts), the session text, the stage gate and the reach filter all read
// from here.

import { PROJECT_PREFIX, RANK_PREFIX, cmp, type Kind } from "./records.js";

export const STAGE_MAX = 9;
export const STAGE_PREFIX = "stage:";
export const PROPOSED_PREFIX = "stage-proposed:";
export const BASELINE_TAG = "stage:0";
export const PROPOSED_TAG = "stage-proposed:0";
export const IDEA_TAG = "idea";
export const DECISION_TAG = "decision";
export const OWNER_TAGS = { human_task: "human", agent_task: "agent" } as const;
export type Owner = "human" | "agent" | "";

const STAGE_RE = /^stage:([0-9])$/;

/** The learning loop's own tags (`shapa learn` writes them on a principle and on a correction): the same list as the
 *  local engine's `db.LEARNING_TAGS` and `db.LEARNING_TAG_PREFIXES` (tests/test_parity.py keeps the two equal). */
export const LEARNING_TAGS: readonly string[] = ["principle", "churn", "violation", "repeat", "repeat-checked", "blocked", "learned", "meta-learning", "dismissed", "soft", "fire"];
export const LEARNING_PREFIXES: readonly string[] = ["violations:", "revisions:", "repeats:", "serves:", "repeat-of:", "last-seen:", "evidence:", "learned-at:", "generalized-into:"];

/** Tags that are the engine's, not topics: never listed in TOPICS or the tag index (unless asked). */
export const SYSTEM_TAGS: readonly string[] = ["skill", "focus", "human_task", "agent_task", IDEA_TAG, DECISION_TAG, ...LEARNING_TAGS];
export const SYSTEM_PREFIXES: readonly string[] = [STAGE_PREFIX, PROPOSED_PREFIX, "skill:", "v:", "status:", RANK_PREFIX, PROJECT_PREFIX, ...LEARNING_PREFIXES];

export function isSystemTag(tag: string): boolean {
  return SYSTEM_TAGS.includes(tag) || SYSTEM_PREFIXES.some((p) => tag.startsWith(p));
}

/** A tag as a lookalike-proof name: compatibility-folded (full-width, ligatures), lower case, with spaces, control,
 *  format (zero-width, bidi) and variation-selector characters removed. */
export const canonTag = (t: string): string => t.normalize("NFKC").toLowerCase().replace(/[\s\p{Cf}\p{Cc}\p{Zl}\p{Zp}\u{FE00}-\u{FE0F}\u{E0100}-\u{E01EF}]/gu, "");

/** A tag that becomes a system tag (a name of SYSTEM_TAGS, or one under a SYSTEM_PREFIXES prefix) once folded by `canonTag`
 *  but is not byte-equal to it: a lookalike, refused everywhere (one list, the same one `isSystemTag` reads). */
export function isLookalike(tag: string): boolean {
  const c = canonTag(tag);
  if (SYSTEM_TAGS.includes(c) && tag !== c) return true;
  return SYSTEM_PREFIXES.some((p) => c.startsWith(p) && !tag.startsWith(p));
}

/** The same test as `isSystemTag`, as a SQL condition over a tag column (the list above is the only source). */
export function systemTagSql(col = "tag"): string {
  const names = SYSTEM_TAGS.map((t) => `'${t}'`).join(",");
  const prefixes = SYSTEM_PREFIXES.map((p) => `substr(${col}, 1, ${p.length}) = '${p}'`).join(" OR ");
  return `(${col} IN (${names}) OR ${prefixes})`;
}

/** The stage of a record from its `stage:N` tag (the lowest if a stray second one exists), else null. */
export function stageOf(tags: readonly string[]): number | null {
  let best: number | null = null;
  for (const t of tags) {
    const m = STAGE_RE.exec(t);
    if (m) best = best === null ? Number(m[1]) : Math.min(best, Number(m[1]));
  }
  return best;
}

export const hasBaseline = (tags: readonly string[]): boolean => tags.includes(BASELINE_TAG);

/** `rank:N` (1-999) on a feature or task: lower first. */
export function rankOf(tags: readonly string[]): number | null {
  for (const t of tags) {
    if (t.startsWith(RANK_PREFIX) && /^[1-9]\d{0,2}$/.test(t.slice(RANK_PREFIX.length))) return Number(t.slice(RANK_PREFIX.length));
  }
  return null;
}
export const RANK_NONE = 1000;

export function ownerOf(tags: readonly string[]): Owner {
  const human = tags.includes("human_task");
  const agent = tags.includes("agent_task");
  return human && !agent ? "human" : agent && !human ? "agent" : "";
}

/** The project key of a record's `project:KEY` tag ("" without one): the first in code point order. */
export function projectKeyOf(tags: readonly string[]): string {
  const t = [...tags].sort(cmp).find((x) => x.startsWith(PROJECT_PREFIX));
  return t === undefined ? "" : t.slice(PROJECT_PREFIX.length);
}

// --- what a record is counted as ---------------------------------------------------------------

/** R rule, M memory, E idea, C decision, I issue, S skill version, D document, T task, J job, F feature, N note. */
export type Bucket = "R" | "M" | "E" | "C" | "I" | "S" | "D" | "T" | "J" | "F" | "N";

export function bucketOf(kind: Kind, tags: readonly string[]): Bucket {
  if (kind === "D") return tags.includes("skill") ? "S" : "D";
  if (kind === "M") return tags.includes(IDEA_TAG) ? "E" : tags.includes(DECISION_TAG) ? "C" : "M";
  return kind;
}

/** The name a bucket goes by in the tag index (ideas and decisions are memories there). */
export const BUCKET_NAMES: Record<Bucket, string> = {
  R: "rule", M: "memory", E: "memory", C: "memory", I: "issue", S: "skill", D: "document", T: "task", J: "job", F: "feature", N: "note",
};
export const COUNT_NAMES = ["rule", "memory", "issue", "skill", "document", "task", "job", "feature", "note"] as const;

/** Whether a `baseline` device (an OAuth grant, or a token minted baseline) may see a record at all: rules,
 *  memories, issues and skill versions only at stage 0; ideas and decisions; everything else. */
export function baseVisible(kind: Kind, tags: readonly string[]): boolean {
  switch (kind) {
    case "R": case "I": return hasBaseline(tags);
    case "M": return tags.includes(IDEA_TAG) || tags.includes(DECISION_TAG) || hasBaseline(tags);
    case "D": return tags.includes("skill") ? hasBaseline(tags) : true;
    default: return true;
  }
}

/** The staged kinds: a `baseline` device never writes one. */
export const isStagedKind = (kind: Kind, tags: readonly string[]): boolean => {
  const b = bucketOf(kind, tags);
  return b === "R" || b === "I" || b === "M" || b === "S";
};
