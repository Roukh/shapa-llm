// What a `baseline` device (every OAuth grant, and a device token minted so) may write. What it may
// READ is not decided here: it is the Repo's view (repo.ts `view`, tags.ts `baseVisible`), the one
// place every read goes through. Here are the write limits (hosted.md, "Reach"):
//
//   writes   ideas and decisions, notes, documents, tasks, project files and items, task claim,
//            release and close, and edits of the notes, documents, tasks, ideas and decisions it can read;
//   never    a rule, memory, issue or skill version (any stage), sync, the focus, a feature or job, a
//            delete, the two notes printed into every session text, a hidden or stage-0 record.

import type { Device } from "./devices.js";
import { ApiError } from "./errors.js";
import { PROJECT_PREFIX, type Kind } from "./records.js";
import type { Stored } from "./repo.js";
import { DECISION_TAG, IDEA_TAG, isStagedKind, isSystemTag } from "./tags.js";

export const BASELINE_REFUSAL = "this device writes notes, tasks, ideas, decisions and files";

export const isBaseline = (d: Device): boolean => d.reach === "baseline";

export function denyBaseline(d: Device, what = BASELINE_REFUSAL): void {
  if (isBaseline(d)) throw new ApiError("forbidden", what);
}

/** The notes printed into every session text: a baseline device never creates, edits or deletes them. */
export const PRINTED_NOTES = ["session-instructions", "response-style"] as const;

/** Whether a name is one of them: by id, alias or path, any case, with or without `.md`, in any folder. */
export function printedName(name: unknown): boolean {
  const last = String(name ?? "").split("/").pop() ?? "";
  const base = last.trim().toLowerCase().replace(/\.md$/, "");
  return (PRINTED_NOTES as readonly string[]).includes(base);
}

/** Whether a device may write the operator's printed notes: a device TOKEN with the delete scope and full reach (the
 *  operator's own machine). Any other writer, a read/write token included, never does. */
export const mayWritePrinted = (d: Device): boolean => d.source === "token" && d.reach === "full" && d.scopes.includes("delete");

/** A baseline device may create only these (REST type names). */
const CREATE_OK = new Set(["idea", "decision", "note", "document", "task", "human_task", "agent_task"]);

export function baselineMayCreate(d: Device, type: string): void {
  if (isBaseline(d) && !CREATE_OK.has(type)) throw new ApiError("forbidden", BASELINE_REFUSAL);
}

/** Whether a baseline device may write a record of this kind with these tags (the tags after the write): not a
 *  feature, job, rule, issue or skill version, a memory only as an idea or a decision, nothing staged, nothing that
 *  names a skill (a `skill:` tag would file a lesson into a skill it may not even read), never the focus. */
export function baselineWrites(kind: Kind, tags: readonly string[]): boolean {
  if (kind === "F" || kind === "J") return false;
  // A memory is writable only as an idea or a decision (the only memories it reads back).
  if (isStagedKind(kind, tags) && !(kind === "M" && (tags.includes(IDEA_TAG) || tags.includes(DECISION_TAG)))) return false;
  if (tags.some((t) => t.startsWith("stage:") || t.startsWith("stage-proposed:") || t === "skill" || t.startsWith("skill:"))) return false;
  if (kind === "D" && tags.includes("focus")) return false;
  return true;
}

/** Refuse the write of a record by a baseline device when it is not one it may write (`baselineWrites`). A hidden
 *  record never gets here: the view answers 404 first. */
export function baselineMayWrite(d: Device, kind: Kind, tags: readonly string[]): void {
  if (!isBaseline(d) || baselineWrites(kind, tags)) return;
  throw new ApiError("forbidden", kind === "D" && tags.includes("focus") ? "this device does not set the focus" : BASELINE_REFUSAL);
}

/** The order of the operator's lists (`rank:N`) is the operator's: a baseline device neither sets nor drops a rank tag. */
export const ranksChanged = (before: readonly string[], after: readonly string[]): boolean => {
  const ranks = (ts: readonly string[]) => ts.filter((t) => t.startsWith("rank:")).sort().join(",");
  return ranks(before) !== ranks(after);
};

export function baselineKeepsRank(d: Device, before: readonly string[], after: readonly string[]): void {
  if (isBaseline(d) && ranksChanged(before, after)) throw new ApiError("forbidden", "this device does not rank the operator's tasks");
}

/** The engine tags a baseline device may put on what it writes: the ones its own type names need (idea, decision, the
 *  task owner) and one `project:KEY`. Every other system tag and family (the learning loop's, `serves:`, `v:`,
 *  `status:` ...) is the engine's: an agent that could set them would steer the operator's learning and ranking. */
const BASELINE_ENGINE_TAGS: ReadonlySet<string> = new Set([IDEA_TAG, DECISION_TAG, "human_task", "agent_task"]);

/** Refuse a baseline device any engine tag it adds. A tag the record already held stays (an edit around the
 *  operator's `serves:` tag on a task is not an add); `held` is the record's tags before the write, [] for a create. */
export function baselineKeepsEngineTags(d: Device, held: readonly string[], next: readonly string[]): void {
  if (!isBaseline(d)) return;
  const bad = next.find((t) => !held.includes(t) && isSystemTag(t) && !BASELINE_ENGINE_TAGS.has(t) && !t.startsWith(PROJECT_PREFIX));
  if (bad !== undefined) throw new ApiError("forbidden", `this device does not set engine tags (${JSON.stringify(bad.slice(0, 40))}): only idea, decision, the task owner and one project`);
}

/** A record that is, or is named as, a printed note is written only by a device token with the delete scope. */
export function guardPrinted(d: Device, wiki: string, names: unknown[]): void {
  if (wiki !== "global" || mayWritePrinted(d)) return;
  if (names.some(printedName)) throw new ApiError("forbidden", "the notes printed into every session text change only from a device token with the delete scope");
}

export const storedNames = (s: Stored): string[] => [s.id, s.alias, String(s.data["alias"] ?? "")];
