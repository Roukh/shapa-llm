// The personal-data gate as the REST and MCP write paths use it (pii.ts has the patterns): every writer here
// is an app, so every category is refused, and anything that goes to stage 0 is refused whoever writes it.
// A refusal is a 400 `invalid` that names the categories and never the value; the warnings of a write the gate
// lets through come back for the result. A sync put from the operator's own device has its own gate, `syncGate`.

import { fail } from "./errors.js";
import { Rejected, check, levelFor, type PiiLevel } from "./pii.js";

const asText = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

/** Refuse (400) a write that holds personal or sensitive data, else the warnings. `texts` are the fields of the write,
 *  `tags` its tag list (a value in a tag is a value). `privileged` is never true for REST or MCP. */
export function piiGate(texts: readonly unknown[], tags: readonly unknown[] = [], privileged = false): string[] {
  const level: PiiLevel = levelFor(tags, privileged);
  try {
    return check([...texts.map(asText), ...tags.map(asText)].filter((t): t is string => t !== null), level);
  } catch (e) {
    if (e instanceof Rejected) fail("invalid", e.message);
    throw e;
  }
}

/** The reason a sync put is refused for personal data, or null. A put from the operator's own device (a device token
 *  with the delete scope) is held to the severe categories only; every other device to all of them; any put that
 *  carries a stage tag to all of them. */
export function piiSyncGate(texts: readonly unknown[], tags: readonly unknown[], privileged: boolean): string | null {
  try {
    check([...texts.map(asText), ...tags.map(asText)].filter((t): t is string => t !== null), levelFor(tags, privileged));
    return null;
  } catch (e) {
    if (e instanceof Rejected) return e.message;
    throw e;
  }
}
