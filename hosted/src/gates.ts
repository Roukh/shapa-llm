// The rules about tags and kinds, in one place so REST, MCP and sync apply the same ones:
// what a tag list may hold (stages, one project, one task owner), what the global wiki accepts,
// who may put a record into the baseline (stage:0) and how much baseline there may be.
//
// Stage 0 is the operator's. Only a sync put from a device TOKEN holding the delete scope (the
// operator's own machine, `shapa stage set`) may add or remove it or edit the text of a record
// that carries it. Everyone else's `stage:0` is stored as `stage-proposed:0`, which no session
// text ever serves, and an edit of a stored stage:0 record is refused.

import type { Device } from "./devices.js";
import { Invalid, LIMITS, cleanProject, normTags, type Kind, type Normalized } from "./records.js";
import type { Repo, Stored } from "./repo.js";
import { splitSkill } from "./skills.js";
import {
  BASELINE_TAG, PROPOSED_PREFIX, PROPOSED_TAG, STAGE_MAX, STAGE_PREFIX, canonTag, hasBaseline, isLookalike, ownerOf, projectKeyOf,
} from "./tags.js";

/** The stage-0 set of the global wiki (a rule or memory: summary + body; a skill: its description) is capped here, in characters. */
export const BASELINE_BUDGET = 6000;
/** A baseline record prints at most this much of its body in the session text. */
export const BASELINE_BODY_MAX = 1200;

export const BASELINE_EDIT_REFUSAL = "a stage:0 (baseline) record changes only from a device token with the delete scope, by sync (the operator's machine: `shapa stage`)";

/** A tag as written, whitespace folded, before any length cut. */
const folded = (t: unknown): string => String(t ?? "").split(/\s+/).filter(Boolean).join(" ");
const canon = canonTag;

const STAGE_TAG_RE = /^stage:[0-9]$/;
const stagedKind = (kind: Kind, tags: readonly string[]) => kind === "R" || kind === "M" || (kind === "D" && tags.includes("skill"));

export interface TagCtx {
  kind: Kind;
  /** The tags as sent (no length cut); the record's own list decides the kind-specific rules. */
  tags: unknown[];
  /** `global` or `repo`: the global wiki's tasks name one owner. */
  wikiKind: "global" | "repo";
}

/** Why a tag list is no good for a record, or null. Applied wherever tags are accepted: REST create and
 *  PATCH, MCP save and update (they call the same), and a sync put. */
export function tagProblem(c: TagCtx): string | null {
  const tags = c.tags.map(folded).filter(Boolean);
  // A lookalike of any system tag (a name, or a prefix family) is refused, never stored: it would read as the
  // system tag to a person and as another tag to the engine.
  const alike = tags.find(isLookalike);
  if (alike !== undefined) return `invalid tag ${JSON.stringify(alike.slice(0, 40))}: it imitates a system tag (write it exactly, or choose another word)`;
  // Stage tags: exact, one marker per record, only on rules, memories and skill versions.
  const family = tags.filter((t) => canon(t).startsWith(STAGE_PREFIX) || canon(t).startsWith(PROPOSED_PREFIX));
  if (family.length) {
    for (const t of family) {
      if (!STAGE_TAG_RE.test(t) && t !== PROPOSED_TAG) return `invalid stage tag ${JSON.stringify(t.slice(0, 40))}: stage:0 to stage:${STAGE_MAX}, or stage-proposed:0`;
    }
    if (tags.filter((t) => STAGE_TAG_RE.test(t)).length > 1) return "a record carries at most one stage tag";
    if (tags.some((t) => STAGE_TAG_RE.test(t)) && tags.includes(PROPOSED_TAG)) return "a record carries one stage marker: stage:N or stage-proposed:0, not both";
    if (!stagedKind(c.kind, tags)) return "stage tags apply to rules, memories and skill versions only";
    // The baseline is the global wiki's: stage 0 (and its proposal) has no meaning in a repo wiki.
    if (c.wikiKind !== "global" && (tags.includes(BASELINE_TAG) || tags.includes(PROPOSED_TAG))) return "stage:0 and stage-proposed:0 exist only in the global wiki (a repo wiki uses stage:1 to stage:9)";
  }
  const projects = tags.filter((t) => t.startsWith("project:"));
  if (new Set(projects).size > 1) return "a record carries at most one project tag";
  for (const t of projects) {
    try {
      const key = t.slice("project:".length);
      if (t.length > LIMITS.tag) return `invalid project tag ${JSON.stringify(t.slice(0, 90))}: at most ${LIMITS.tag} characters`;
      if (!key || cleanProject(key) !== key) return `invalid project tag ${JSON.stringify(t.slice(0, 90))}: lowercase letters, digits and . _ / - only, one key as written`;
    } catch (e) {
      if (e instanceof Invalid) return `invalid project tag ${JSON.stringify(t.slice(0, 90))}: lowercase letters, digits and . _ / - only, one key as written`;
      throw e;
    }
  }
  const human = tags.includes("human_task");
  const agent = tags.includes("agent_task");
  if ((human || agent) && c.kind !== "T") return "the tags human_task and agent_task apply to tasks only";
  if (human && agent) return "a task is a human_task or an agent_task, not both";
  if (c.kind === "T" && c.wikiKind === "global" && !human && !agent) return "a task in the global wiki is a human_task or an agent_task";
  return null;
}

/** What the global wiki accepts: tasks only, standalone. Features and jobs are bound to a branch and a commit. */
export function kindProblem(wikiKind: "global" | "repo", kind: Kind, parent: unknown): string | null {
  if (wikiKind !== "global") return null;
  if (kind === "F" || kind === "J") return "the global wiki has no features or jobs (they are bound to a branch and a commit): it holds standalone tasks";
  if (kind === "T" && parent !== null && parent !== undefined && parent !== "") return "a task in the global wiki stands alone: it has no parent";
  return null;
}

// --- the stage gate --------------------------------------------------------------------------------

/** Whether a writer may put a record into the baseline: a sync put from a device token with the delete scope. */
export const mayBaseline = (d: Device, viaSync: boolean): boolean => viaSync && d.source === "token" && d.reach === "full" && d.scopes.includes("delete");

export interface GateIn {
  /** The tags of the stored live record this write changes, null for a new record. */
  stored: readonly string[] | null;
  next: readonly string[];
  privileged: boolean;
}
export type GateOut = { tags: string[]; notice?: string } | { error: string };

/** The stage-0 rule: refuse any change of a stored stage:0 record, and keep a new or retagged `stage:0` out of the baseline. */
export function gateStage(g: GateIn): GateOut {
  if (g.privileged) return { tags: [...g.next] };
  if (g.stored && hasBaseline(g.stored)) return { error: BASELINE_EDIT_REFUSAL };
  if (!hasBaseline(g.next)) return { tags: [...g.next] };
  const tags = normTags([...g.next.filter((t) => t !== BASELINE_TAG), PROPOSED_TAG]);
  return { tags, notice: "stage:0 is the operator's: stored as stage-proposed:0 for review (the operator sets stage 0 with `shapa stage set`)" };
}

// --- the baseline budget -----------------------------------------------------------------------------

const chars = (s: unknown): number => Array.from(String(s ?? "")).length;

/** What a stage-0 record counts against the budget: a rule or memory its summary + body, an ACTIVE skill version its
 *  description (a pending or previous version is never printed, so it costs nothing). */
export function baselineCost(kind: Kind, tags: readonly string[], data: Record<string, unknown>): number {
  if (kind === "D" && tags.includes("skill")) return tags.includes("status:active") ? chars(splitSkill(String(data["body"] ?? "")).fields["description"]) : 0;
  return chars(data["summary"]) + chars(data["body"]);
}

/** The refusal when putting `n` at stage 0 would take the global wiki's baseline past the budget, else null. */
export function budgetProblem(repo: Repo, wiki: string, n: Normalized): string | null {
  if (wiki !== "global" || !hasBaseline(n.tags) || !stagedKind(n.kind, n.tags)) return null;
  let held = 0;
  for (const s of baselineSet(repo.raw, wiki)) {
    if (s.uid === n.uid || s.id === n.id) continue;
    held += baselineCost(s.kind, s.tags, s.data);
  }
  const need = baselineCost(n.kind, n.tags, n.data);
  return held + need > BASELINE_BUDGET
    ? `the baseline holds ${held} of ${BASELINE_BUDGET} characters; this record needs ${need} more (shorten it, or take a record out of stage 0 first)`
    : null;
}

/** The live stage-0 records of a wiki (at most 2000: the budget keeps the set small). */
export function baselineSet(repo: Repo, wiki: string): Stored[] {
  const ids = repo.sql
    .exec(`SELECT id FROM record_tags WHERE wiki = ? AND tag = ? LIMIT 2000`, wiki, BASELINE_TAG)
    .toArray().map((r) => r["id"] as string);
  return ids.map((id) => repo.get(wiki, id)).filter((s): s is Stored => s !== null && !s.deleted);
}

export { ownerOf, projectKeyOf };
