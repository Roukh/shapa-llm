// A repo node's place in the hierarchy, read from the node documents the operator's sync writes into the
// global wiki (the local engine's `node.py`): the groups above the repo, its type, `read_stage` (the lowest
// device stage that may read the repo: above 0 hides it from a `baseline` device) and `global_access` (the
// highest global stage the repo reads, null when unset). A node written before these fields reads with the
// defaults. The body is untrusted text: it is validated here and never printed unmasked.

import { NODE_ALIAS_PREFIX, NODE_TAG } from "./records.js";
import { STAGE_MAX } from "./tags.js";

export interface NodeInfo {
  key: string;
  path: string[];
  type: string;
  readStage: number;
  globalAccess: number | null;
}

const KEY_RE = /^[a-z0-9][a-z0-9._/-]{0,79}$/;
/** A group or type name (the local engine's `hierarchy.NAME_RE`). */
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/;
const MAX_NODES = 200;
const MAX_PATH = 16;

const stage = (v: unknown, fallback: number | null): number | null =>
  typeof v === "number" && Number.isInteger(v) && v >= 0 && v <= STAGE_MAX ? v : fallback;

/** A node document as `NodeInfo`, or null when it is not a trusted node: the alias, the reserved tag and the source
 *  agree, and the key in the body is the one the alias names (a document cannot speak for another repo). */
export function parseNode(alias: string, tags: readonly string[], data: Record<string, unknown>): NodeInfo | null {
  if (!alias.startsWith(NODE_ALIAS_PREFIX) || !tags.includes(NODE_TAG) || data["source"] !== "node" || typeof data["body"] !== "string") return null;
  let raw: unknown;
  try {
    raw = JSON.parse(data["body"]);
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  if (typeof r["key"] !== "string" || !KEY_RE.test(r["key"]) || alias.slice(NODE_ALIAS_PREFIX.length) !== r["key"]) return null;
  const path = Array.isArray(r["path"]) ? r["path"].slice(0, MAX_PATH).filter((p): p is string => typeof p === "string" && NAME_RE.test(p)) : [];
  const type = typeof r["type"] === "string" && NAME_RE.test(r["type"]) ? r["type"] : "";
  return { key: r["key"], path, type, readStage: stage(r["read_stage"], 0)!, globalAccess: stage(r["global_access"], null) };
}

/** Every trusted node in the global wiki (at most MAX_NODES). Reads the stored rows directly: never through a reach filter. */
export function readNodes(sql: SqlStorage): NodeInfo[] {
  const out: NodeInfo[] = [];
  const rows = sql
    .exec(
      // `alias != ''` is what lets SQLite use the partial alias index (the range alone does not name it).
      `SELECT alias, tags, data FROM records WHERE wiki = 'global' AND alias != '' AND alias >= ? AND alias < ? AND kind = 'D' AND deleted = 0 ORDER BY seq LIMIT ?`,
      NODE_ALIAS_PREFIX, "node;", MAX_NODES,
    )
    .toArray();
  for (const r of rows) {
    const n = parseNode(String(r["alias"]), JSON.parse(r["tags"] as string) as string[], JSON.parse(r["data"] as string) as Record<string, unknown>);
    if (n) out.push(n);
  }
  return out;
}

/** The wikis a device of `deviceStage` may not read: the repos whose `read_stage` is above it. */
export const hiddenKeys = (nodes: readonly NodeInfo[], deviceStage: number): string[] =>
  nodes.filter((n) => n.readStage > deviceStage && n.key !== "global").map((n) => n.key);

export const sameGroup = (a: readonly string[], b: readonly string[]): boolean => a.length === b.length && a.every((x, i) => x === b[i]);
