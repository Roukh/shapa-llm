// One rule per object (operator, 2026-11-20): a rule written through REST or MCP is ONE imperative sentence, so each can be
// superseded or merged alone. The same heuristic as the local engine's `db.several_rules` (tests/test_parity.py holds the word
// list equal, and hosted/test/atomic-cases.json is run by both sides): a list, or several sentences that each open with a
// command word. It runs on a create and an edit; a sync put is never refused for it (stored rows are grandfathered).

import { fail } from "./errors.js";

const COMMAND_WORD_LIST: readonly string[] = [
  "always", "never", "do", "dont", "don't", "avoid", "use", "prefer", "keep", "make", "run", "write", "add", "remove", "ensure", "check",
  "stop", "put", "set", "open", "close", "commit", "push", "install", "test", "read", "ask", "tell", "call", "create", "delete", "update",
  "move", "name", "pass", "wrap", "treat", "only", "must", "should", "stick", "start", "finish", "merge", "review", "verify", "validate",
  "document", "log", "prefix", "limit", "restrict", "skip", "fix", "rename", "split", "combine", "reuse", "replace", "store", "save", "load",
  "cache", "clean", "reject", "refuse", "require", "allow", "forbid", "disable", "enable", "configure", "deploy", "build", "ship", "send",
  "wait", "confirm", "report", "mention", "list", "show", "hide", "drop", "pin", "lock", "mark", "tag", "link", "reply", "answer", "explain",
  "return", "throw", "catch", "handle", "retry", "fail", "prompt", "format", "lint", "audit", "scan", "delegate", "pick", "choose", "ignore",
  "respect", "honor", "follow", "obey", "default",
];
const COMMAND_WORDS: ReadonlySet<string> = new Set(COMMAND_WORD_LIST);

const LIST_LINE = /^\s*(?:\d{1,2}[.)]|[-*•])\s+\S/u;
const NUM_LINE = /^\s*\d{1,2}[.)]\s+\S/u;
const NUM_INLINE = /(?:^|[\s(])\(?\d{1,2}[.)]\s+[A-Za-z]/gu;
const SENTENCE_SPLIT = /(?<=[.!?;])\s+(?=\S)|\n+/u;
const LIST_PREFIX = /^\s*(?:\d{1,2}[.)]|[-*•])\s+/u;
const FIRST_WORD = /^[^\p{L}]*(\p{L}[\p{L}\p{M}\p{N}_'’-]*)/u;
// Python's str.splitlines: the line boundaries it honours.
const LINE_BREAK = new RegExp(`\\r\\n|[\\n\\r\\v\\f\\x1c-\\x1e\\x85${String.fromCharCode(0x2028, 0x2029)}]`);

const lines = (s: string): string[] => {
  const out = s.split(LINE_BREAK);
  if (out.length && out[out.length - 1] === "") out.pop();
  return out;
};
const count = (re: RegExp, s: string): number => (s.match(re) ?? []).length;

function commands(text: string): number {
  let n = 0;
  for (const raw of text.trim().split(SENTENCE_SPLIT)) {
    const part = raw.replace(LIST_PREFIX, "");
    const m = FIRST_WORD.exec(part);
    if (m && COMMAND_WORDS.has(m[1]!.toLowerCase().replace(/’/g, "'"))) n += 1;
  }
  return n;
}

/** Why the summary (and, more loosely, the body) holds more than one rule, or null when it is one. */
export function severalRules(summary: string, body = ""): string | null {
  const s = String(summary ?? "");
  const sl = lines(s);
  if (sl.filter((l) => NUM_LINE.test(l)).length >= 2 || count(NUM_INLINE, s) >= 2) return "a numbered list";
  if (sl.filter((l) => LIST_LINE.test(l)).length >= 2) return "a bulleted list";
  if (commands(s) >= 2) return "several imperative sentences";
  const b = String(body ?? "");
  const bl = lines(b);
  if (bl.filter((l) => NUM_LINE.test(l)).length >= 2 || count(NUM_INLINE, b) >= 2) return "a numbered list in the body";
  if (bl.filter((l) => LIST_LINE.test(l) && commands(l) >= 1).length >= 2) return "several imperative bullets in the body";
  return null;
}

/** Refuse (400) a rule that holds several. */
export function checkAtomicRule(summary: unknown, body: unknown, what = "a rule"): void {
  const why = severalRules(typeof summary === "string" ? summary : "", typeof body === "string" ? body : "");
  if (why) {
    fail("invalid", `${what} is ONE rule, one imperative sentence; this holds ${why}: split it, one rule per object (one save each)`);
  }
}
