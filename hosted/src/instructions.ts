// Session instructions for clients with no hooks (chat apps, phones). Composed per request
// within a character budget: what shapa is, the operator's own session-instructions and style
// notes, global rules, live features, then the write-back format for the client kind. The
// write-back block is reserved first so a small budget never cuts it.

import { type Device, needScope, wikiAllowed } from "./devices.js";
import { fail } from "./errors.js";
import { focusView } from "./focus.js";
import { ledgerTree } from "./ledger.js";
import { LIMITS, NORTHSTAR_MAX, oneLine, type Data } from "./records.js";
import { redact } from "./redact.js";
import { Repo } from "./repo.js";

export type ClientKind = "mcp" | "rest";
export const TEMPLATE_VERSION = 2;

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
Focus: \`focus\` {wiki?} shows the northstar and the three fires; \`focus_set\` {wiki, northstar?, fires?, promote?} sets them. A project keeps ONE document: a second \`save\` of type document is refused naming the first (add merge: true to append a section).
Skills: \`skills\` lists them, \`skill\` {name, wiki?, version?} reads one.
Look before you write: \`search\` {query}, then \`get\` {id}. Fix a record with \`update\` instead of saving a copy.`;

const WRITE_REST = `HOW TO WRITE BACK (HTTP, header "Authorization: Bearer <token>")
POST /v1/wikis/{wiki}/records
  {"type":"memory","summary":"<one line, max 160>","body":"<optional detail>","tags":["<optional>"]}
type: memory (an event the operator said matters or a strategy shift), rule (a standing rule), issue (a mistake the operator corrected and the fix), idea, document (use "title" instead of "summary"), note (add "path":"research/name.md"), feature | job | task (use "title"; job and task take "parent").
Pick the wiki: "global" for what holds across repos, else the repo's wiki key (GET /v1/wikis).
Identical text is stored once. Never put secrets in; they are masked anyway.
Work: GET /v1/wikis/{wiki}/records?kind=F lists features. POST /v1/wikis/{wiki}/records/{id}/claim, /release, /close {"reason":"agent"}. Fix a record with PATCH /v1/wikis/{wiki}/records/{id}.
Focus: GET /v1/wikis/{wiki}/focus shows the northstar and the three fires; PUT it {"northstar":"...","fires":[{"feature":"F1"},{"text":"..."}]} to set them. A project keeps ONE document: a second one is a 409 naming the first ("merge": true appends a section).
Skills: GET /v1/wikis/{wiki}/skills, GET /v1/wikis/{wiki}/skills/{name}.
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

/** Record text that goes into the instructions: masked, and one line, so a record can never add a
 *  line or a heading of its own to the operator session text. */
const line = (text: unknown, limit: number): string => oneLine(redact(String(text ?? "")), limit);

function section(title: string, body: string): string {
  return body ? `${title}\n${body}` : "";
}

const TREE_JOBS_SHOWN = 10;
const TREE_TASKS_SHOWN = 4;

/** The northstar lines and the fires with their tree, for the wikis in scope: the focus a session starts from. */
function focusSections(repo: Repo, wikis: string[]): { north: string[]; fires: string[]; hints: string[] } {
  const north: string[] = [];
  const fires: string[] = [];
  const hints: string[] = [];
  for (const w of wikis) {
    const v = focusView(repo, w);
    if (v.northstar) north.push(`[${w}] ${line(v.northstar, NORTHSTAR_MAX)}`);
    const shown: string[] = [];
    v.fires.forEach((t, i) => {
      if (!t) return;
      const claimed = t.status === "claimed" ? " (claimed)" : "";
      shown.push(`${i + 1}. ${t.feature ? `${t.feature} ` : ""}${line(t.title, 160)}${claimed}`);
      for (const j of t.jobs.slice(0, TREE_JOBS_SHOWN)) {
        shown.push(`   ${j.id} ${line(j.title, 120)}`);
        for (const task of j.tasks.slice(0, TREE_TASKS_SHOWN)) shown.push(`      ${task.id} ${line(task.title, 100)}`);
        if (j.tasks.length > TREE_TASKS_SHOWN) shown.push(`      …${j.tasks.length - TREE_TASKS_SHOWN} more tasks`);
      }
      if (t.jobs.length > TREE_JOBS_SHOWN) shown.push(`   …${t.jobs.length - TREE_JOBS_SHOWN} more jobs`);
      const names = [t.files.map((x) => line(x, 80)).join(", "), t.boxes.length ? `arch ${t.boxes.map((x) => line(x, 60)).join(", ")}` : ""].filter(Boolean).join("; ");
      if (names) shown.push(`   names: ${names}`);
    });
    if (shown.length) fires.push(`[${w}]\n${shown.join("\n")}`);
    else if (v.candidates.length && repo.wiki(w)?.kind === "repo") {
      hints.push(`${w}: no fires set; candidates ${v.candidates.slice(0, 3).map((c) => `${c.id} ${line(c.title, 70)}`).join("; ")}`);
    }
  }
  return { north, fires, hints };
}

export function composeInstructions(
  repo: Repo, d: Device, opts: { client: ClientKind; wiki?: string; budget: number; full?: boolean },
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

  // The focus first: the northstar, then the fires and only their tree (global first, then the wikis in scope).
  const scope = [...visible].sort((a, b) => Number(b.key === "global") - Number(a.key === "global")).map((w) => w.key);
  const focus = focusSections(repo, scope);
  add(section("NORTHSTAR", fit(focus.north, Math.max(0, Math.floor(total * 0.2)))), 0.2);
  if (focus.fires.length) add(section("FIRES (ranked; each with its open jobs and tasks)", focus.fires.join("\n")), 0.4);
  if (!focus.north.length && !focus.fires.length) {
    add("NORTHSTAR AND FIRES\nNone are set. Set them with PUT /v1/wikis/{wiki}/focus (the `focus_set` tool)." + (focus.hints.length ? `\n${focus.hints.join("\n")}` : ""), 0.2);
  } else if (focus.hints.length) {
    add(section("FIRES NOT SET", focus.hints.join("\n")), 0.1);
  }

  if (hasGlobal) {
    add(section("OPERATOR'S SESSION INSTRUCTIONS", noteBody(repo, "session-instructions")), 0.4);
    add(section("OPERATOR'S STYLE", noteBody(repo, "response-style")), 0.15);
  }

  // Everything else (the rules, the other live features) only when asked for: `full`.
  if (opts.full) {
    const rules: string[] = [];
    // Rules come from the global wiki, plus the wiki the request names: not from every wiki.
    for (const w of visible) {
      if (w.key !== "global" && w.key !== opts.wiki) continue;
      for (const r of repo.live(w.key, ["R"])) {
        rules.push(`${w.key === "global" ? "" : `${w.key}/`}${r.id} ${line((r.data as Data)["summary"], 160)}`);
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
        const by = line(f.claimed_by, 64);
        features.push(`${w.key}/${f.id} ${line(f.title, LIMITS.title)}${by ? ` [claimed by ${by}]` : ""}${open ? ` (${open} open ${open === 1 ? "job" : "jobs"})` : ""}`);
      }
    }
    if (features.length) add(section("LIVE FEATURES", fit(features, Math.max(0, room - 16))));
  } else {
    add("MORE: rules and the other live features are left out; ask for them with full=1 (session_start {full: true}), or `search` and `ledger_list`.", 0.1);
  }

  const text = [INTRO, ...parts, write].join("\n\n");
  return { text: text.length <= opts.budget ? text : clip(text, opts.budget), version: `${TEMPLATE_VERSION}.${repo.rev()}` };
}
