// Session instructions for clients with no hooks (chat apps, phones). Composed per request
// within a character budget: what shapa is, the operator's own session-instructions and style
// notes, global rules, live features, then the write-back format for the client kind. The
// write-back block is reserved first so a small budget never cuts it.

import { type Device, needScope, wikiAllowed } from "./devices.js";
import { fail } from "./errors.js";
import { ledgerTree } from "./ledger.js";
import { LIMITS, type Data } from "./records.js";
import { Repo } from "./repo.js";

export type ClientKind = "mcp" | "rest";
export const TEMPLATE_VERSION = 1;

const INTRO =
  "shapa is this operator's shared memory and work ledger, hosted on one server. Every wiki (one global, one per repo) is read and written through it. " +
  "Recall before you act, and write down what the operator would otherwise have to repeat.";

const WRITE_MCP = `HOW TO WRITE BACK (MCP tools)
Call \`save\` with {wiki, type, summary, body?, tags?}. Pick the wiki: "global" for what holds across repos, else the repo's wiki key (\`wikis\` lists them).
- memory: something the operator said that matters, or a strategy shift.
- rule: a standing rule the operator gave you.
- issue: a mistake the operator corrected: what went wrong and the fix.
- idea: a thought worth keeping.
- document: longer text; use title (not summary) and body.
- note: a file; give path ("research/name.md" or "arch/name.md") and body.
summary is one line, up to 160 characters. Identical text is stored once. Never put secrets in; they are masked anyway.
Work: \`ledger_list\` shows features with their jobs and tasks. \`ledger_add\` {wiki, kind: feature|job|task, title, parent?}. \`ledger_set\` {wiki, id, action: claim|release|close|edit}.
Look before you write: \`search\` {query}, then \`get\` {id}. Fix a record with \`update\` instead of saving a copy.`;

const WRITE_REST = `HOW TO WRITE BACK (HTTP, header "Authorization: Bearer <token>")
POST /v1/wikis/{wiki}/records
  {"type":"memory","summary":"<one line, max 160>","body":"<optional detail>","tags":["<optional>"]}
type: memory (an event the operator said matters or a strategy shift), rule (a standing rule), issue (a mistake the operator corrected and the fix), idea, document (use "title" instead of "summary"), note (add "path":"research/name.md"), feature | job | task (use "title"; job and task take "parent").
Pick the wiki: "global" for what holds across repos, else the repo's wiki key (GET /v1/wikis).
Identical text is stored once. Never put secrets in; they are masked anyway.
Work: GET /v1/wikis/{wiki}/records?kind=F lists features. POST /v1/wikis/{wiki}/records/{id}/claim, /release, /close {"reason":"agent"}. Fix a record with PATCH /v1/wikis/{wiki}/records/{id}.
Look before you write: GET /v1/search?q=<words>, GET /v1/get?id=<id>.`;

function noteBody(repo: Repo, ref: string): string {
  const note = repo.find("global", ref);
  return note && typeof note.data["body"] === "string" ? stripFrontmatter(String(note.data["body"])).trim() : "";
}

function stripFrontmatter(text: string): string {
  return text.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "");
}

/** Fit as many lines as the room allows, then say how many were left out. */
function fit(lines: string[], room: number): string {
  const out: string[] = [];
  let used = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    const left = lines.length - i;
    const reserve = left > 1 ? `…and ${left - 1} more`.length + 1 : 0;
    if (used + line.length + 1 + reserve > room) {
      const note = `…and ${left} more`;
      if (used + note.length + 1 <= room) out.push(note);
      break;
    }
    out.push(line);
    used += line.length + 1;
  }
  return out.join("\n");
}

function clip(text: string, room: number): string {
  if (text.length <= room) return text;
  if (room < 20) return "";
  return text.slice(0, room - 1).trimEnd() + "…";
}

function section(title: string, body: string): string {
  return body ? `${title}\n${body}` : "";
}

export function composeInstructions(
  repo: Repo, d: Device, opts: { client: ClientKind; wiki?: string; budget: number },
): { text: string; version: string } {
  needScope(d, "read");
  const { min, max } = LIMITS.instructionsBudget;
  if (opts.budget < min || opts.budget > max) fail("invalid", `budget must be between ${min} and ${max} characters`);
  if (opts.wiki !== undefined) {
    if (!wikiAllowed(d, opts.wiki)) fail("forbidden", `this device may not use wiki ${opts.wiki}`);
    if (!repo.wiki(opts.wiki)) fail("not_found", `no wiki ${opts.wiki}`);
  }
  const write = opts.client === "mcp" ? WRITE_MCP : WRITE_REST;
  const visible = repo.wikis().filter((w) => wikiAllowed(d, w.key) && (opts.wiki === undefined || w.key === opts.wiki || w.key === "global"));
  const hasGlobal = visible.some((w) => w.key === "global");

  const parts: string[] = [];
  let room = opts.budget - INTRO.length - write.length - 8;
  // Each section gets a share of what is left after the fixed text, so one long note cannot
  // starve the rules and live features (they keep at least a fifth).
  const total = Math.max(0, room);
  const add = (text: string, share = 1) => {
    const cap = Math.min(room, Math.floor(total * share));
    if (!text || cap < 40) return;
    const clipped = clip(text, cap);
    if (!clipped) return;
    parts.push(clipped);
    room -= clipped.length + 2;
  };

  if (hasGlobal) {
    add(section("OPERATOR'S SESSION INSTRUCTIONS", noteBody(repo, "session-instructions")), 0.4);
    add(section("OPERATOR'S STYLE", noteBody(repo, "response-style")), 0.15);
  }
  const rules: string[] = [];
  for (const w of visible) {
    for (const r of repo.live(w.key, ["R"])) {
      rules.push(`${w.key === "global" ? "" : `${w.key}/`}${r.id} ${String((r.data as Data)["summary"] ?? "")}`);
    }
  }
  if (rules.length) {
    const cap = Math.min(room, Math.floor(total * 0.25));
    add(section("RULES", fit(rules, Math.max(0, cap - 8))));
  }

  const features: string[] = [];
  for (const w of visible) {
    if (w.key === "global") continue;
    for (const f of ledgerTree(repo, w.key)) {
      if (f.type !== "feature") continue;
      const open = f.children.length;
      features.push(`${w.key}/${f.id} ${f.title}${f.claimed_by ? ` [claimed by ${f.claimed_by}]` : ""}${open ? ` (${open} open ${open === 1 ? "job" : "jobs"})` : ""}`);
    }
  }
  if (features.length) add(section("LIVE FEATURES", fit(features, Math.max(0, room - 16))));

  const text = [INTRO, ...parts, write].join("\n\n");
  return { text: text.length <= opts.budget ? text : clip(text, opts.budget), version: `${TEMPLATE_VERSION}.${repo.rev()}` };
}
