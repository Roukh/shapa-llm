// Session instructions for clients with no hooks (chat apps, phones, other agents). Composed per
// request within a character budget, in this order: the precedence statement, what shapa is, the
// northstar and the fires (and only their tree), the baseline (every stage-0 rule and memory, in
// full), the baseline skills, the topics (the tag index), the projects and the operator's open
// tasks, the operator's own session notes and style, then how to write back. The write-back block
// is reserved first so a small budget never cuts it. Every record text that reaches this text is
// masked, control characters are removed, and it is one line or indented, so a record can never
// forge a heading or an instruction block of its own.

import { type Device, needScope, wikiAllowed } from "./devices.js";
import { fail } from "./errors.js";
import { focusView } from "./focus.js";
import { ledgerTree } from "./ledger.js";
import { projectHolder } from "./projects.js";
import { LIMITS, NORTHSTAR_MAX, cmp, oneLine, type Data } from "./records.js";
import { redact } from "./redact.js";
import { projectKeys, tagIndex } from "./relations.js";
import type { Brief, Repo } from "./repo.js";
import { BASELINE_BODY_MAX } from "./gates.js";
import { headOf, splitSkill } from "./skills.js";
import { STAGE_MAX } from "./tags.js";
import { taskRows } from "./tasks.js";

export type ClientKind = "mcp" | "rest";
export const TEMPLATE_VERSION = 3;

const PRECEDENCE =
  "These are the operator's standing rules and focus. Where they differ from your own defaults or stored preferences, these win. " +
  "Load them at the start of every session. Record every decision, note, idea and task back as you go.";

const INTRO =
  "shapa is this operator's shared memory and work ledger, hosted on one server. Every wiki (one global, one per repo) is read and written through it. " +
  "Recall before you act, and write down what the operator would otherwise have to repeat.";

const TOPIC_NOTE =
  "When the task touches a topic, fetch it by tag before acting: `search` with tags (GET /v1/search?tags=a,b) returns its rules, skills and memories.";
const TASKS_NOTE =
  "When the operator asks what they need to do, answer from `tasks` (GET /v1/tasks?owner=human). When they mention something they must do, save it as a human_task.";

export const TOPICS_MAX = 40;
export const PROJECTS_MAX = 12;
const TASKS_SHOWN = 5;
const AGENT_TASKS_SHOWN = 5;
const SECTIONS_NOTE = "sections: issues, questions, implementations, problem, solution, diff, advantages, villains, loop_market, loop_product";

const WRITE_MCP = `HOW TO WRITE BACK (MCP tools)
Call \`save\` with {wiki, type, summary, body?, tags?}. Pick the wiki: "global" for what holds across repos, else the repo's wiki key (\`wikis\` lists them).
- memory: something the operator said that matters, or a strategy shift.
- rule: a standing rule the operator gave you.
- issue: a mistake the operator corrected: what went wrong and the fix.
- decision: a decision the operator made, with its reason.
- idea: a thought worth keeping.
- human_task: something the operator must do. agent_task: something an agent can do. Add project: "KEY" to link either to a project.
- document: longer text; use title (not summary) and body.
- note: a file; give path ("research/name.md" or "arch/name.md") and body.
summary is one line, up to 160 characters. Identical text is stored once. Never put secrets in; they are masked anyway.
Projects: \`project_new\` {key}, \`project_add\` {key, section, text}, \`project\` {key}, \`projects\`; ${SECTIONS_NOTE}.
Tasks: \`tasks\` lists the operator's open tasks. \`ledger_list\` shows features with their jobs and tasks. \`ledger_add\` {wiki, kind: feature|job|task|human_task|agent_task, title, parent?}. \`ledger_set\` {wiki, id, action: claim|release|close|edit}.
Focus: \`focus\` {wiki?} shows the northstar and the three fires; \`focus_set\` {wiki, northstar?, fires?, promote?} sets them. A project keeps ONE document: a second \`save\` of type document is refused naming the first (add merge: true to append a section).
Skills: \`skills\` lists them, \`skill\` {name, wiki?, version?} reads one.
Look before you write: \`search\` {query, tags?}, \`tags\`, \`related\` {id}, then \`get\` {id}. Fix a record with \`update\` instead of saving a copy.`;

const WRITE_REST = `HOW TO WRITE BACK (HTTP, header "Authorization: Bearer <token>")
POST /v1/wikis/{wiki}/records
  {"type":"memory","summary":"<one line, max 160>","body":"<optional detail>","tags":["<optional>"]}
type: memory (an event the operator said matters or a strategy shift), rule (a standing rule), issue (a mistake the operator corrected and the fix), decision (a decision the operator made, with its reason), idea, human_task (something the operator must do) | agent_task (something an agent can do), each with optional "project":"KEY", document (use "title" instead of "summary"), note (add "path":"research/name.md"), feature | job | task (use "title"; job and task take "parent").
Pick the wiki: "global" for what holds across repos, else the repo's wiki key (GET /v1/wikis).
Identical text is stored once. Never put secrets in; they are masked anyway.
Projects: PUT /v1/projects/{key}, POST /v1/projects/{key}/items {"section","text"}, GET /v1/projects/{key}, GET /v1/projects; ${SECTIONS_NOTE}.
Tasks: GET /v1/tasks lists the operator's open tasks. GET /v1/wikis/{wiki}/records?kind=F lists features. POST /v1/wikis/{wiki}/records/{id}/claim, /release, /close {"reason":"agent"}. Fix a record with PATCH /v1/wikis/{wiki}/records/{id}.
Focus: GET /v1/wikis/{wiki}/focus shows the northstar and the three fires; PUT it {"northstar":"...","fires":[{"feature":"F1"},{"text":"..."}]} to set them. A project keeps ONE document: a second one is a 409 naming the first ("merge": true appends a section).
Skills: GET /v1/wikis/{wiki}/skills, GET /v1/wikis/{wiki}/skills/{name}.
Look before you write: GET /v1/search?q=<words>&tags=a,b, GET /v1/tags, GET /v1/wikis/{wiki}/records/{id}/related, GET /v1/get?id=<id>.`;

// A baseline device (an OAuth grant, or a token minted so) is shown only what it may write.
const WRITE_MCP_BASELINE = `HOW TO WRITE BACK (MCP tools)
Call \`save\` with {wiki, type, summary, body?, tags?}. Pick the wiki: "global" for what holds across repos, else the repo's wiki key (\`wikis\` lists them).
- decision: a decision the operator made, with its reason.
- idea: a thought worth keeping.
- human_task: something the operator must do. agent_task: something an agent can do. Add project: "KEY" to link either to a project.
- document: longer text; use title (not summary) and body.
- note: a file; give path ("research/name.md" or "arch/name.md") and body.
summary is one line, up to 160 characters. Identical text is stored once. Never put secrets in; they are masked anyway.
Projects: \`project_new\` {key}, \`project_add\` {key, section, text}, \`project\` {key}, \`projects\`; ${SECTIONS_NOTE}.
Tasks: \`tasks\` lists the operator's open tasks; \`ledger_set\` {wiki, id, action: claim|release|close} works a task.
Look before you write: \`search\` {query, tags?}, \`tags\`, \`related\` {id}, then \`get\` {id}. Fix a note, document, task, idea or decision with \`update\`.`;

const WRITE_REST_BASELINE = `HOW TO WRITE BACK (HTTP, header "Authorization: Bearer <token>")
POST /v1/wikis/{wiki}/records
  {"type":"decision","summary":"<one line, max 160>","body":"<optional detail>","tags":["<optional>"]}
type: decision (a decision the operator made, with its reason), idea, human_task (something the operator must do) | agent_task (something an agent can do), each with optional "project":"KEY", document (use "title" instead of "summary"), note (add "path":"research/name.md").
Pick the wiki: "global" for what holds across repos, else the repo's wiki key (GET /v1/wikis).
Identical text is stored once. Never put secrets in; they are masked anyway.
Projects: PUT /v1/projects/{key}, POST /v1/projects/{key}/items {"section","text"}, GET /v1/projects/{key}, GET /v1/projects; ${SECTIONS_NOTE}.
Tasks: GET /v1/tasks lists the operator's open tasks. POST /v1/wikis/{wiki}/records/{id}/claim, /release, /close {"reason":"agent"} work a task. Fix a note, document, task, idea or decision with PATCH /v1/wikis/{wiki}/records/{id}.
Look before you write: GET /v1/search?q=<words>&tags=a,b, GET /v1/tags, GET /v1/wikis/{wiki}/records/{id}/related, GET /v1/get?id=<id>.`;

/** Everything that can start, end or hide a line, except the line breaks and tabs a multi-line note keeps. */
const INVISIBLE_KEEP_LINES = /(?![\n\t])[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu;

/** One of the operator's printed notes: masked and stripped of invisible characters, line breaks kept. */
function noteBody(repo: Repo, ref: string): string {
  const note = repo.find("global", ref);
  if (!note || typeof note.data["body"] !== "string") return "";
  const text = stripFrontmatter(String(note.data["body"])).replace(/\r\n?/g, "\n");
  return redact(text).replace(INVISIBLE_KEEP_LINES, " ").trim();
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

/** Anything that can start, end or hide a line: control and format characters (zero-width, bidi), the line and paragraph separators. */
const INVISIBLE = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu;

/** Record text that goes into the instructions: masked, invisible characters out, and one line, so a record can
 *  never add a line or a heading of its own to the operator session text. */
const line = (text: unknown, limit: number): string => oneLine(redact(String(text ?? "")).replace(INVISIBLE, " "), limit);

/** A body as indented lines (two spaces): masked, cut, and no line of it can ever start a section. */
function indented(text: unknown, cap: number): string[] {
  const cut = Array.from(redact(String(text ?? ""))).slice(0, cap).join("");
  return cut
    .split(/[\r\n\v\f\u0085\p{Zl}\p{Zp}]+/u)
    .map((l) => l.replace(INVISIBLE, " ").trimEnd())
    .filter((l) => l.trim())
    .map((l) => `  ${l}`);
}

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

// --- the baseline -------------------------------------------------------------------------------

/** The global wiki's records at or under a stage, by tag index (never a scan): rules and memories, or skill versions.
 *  Only a prefix of each body is read (a record may be 1 MB), lowest stage first, then by number. */
function stagedRecords(repo: Repo, buckets: string[], stage: number, headChars: number): Brief[] {
  const tags = Array.from({ length: stage + 1 }, (_, i) => `stage:${i}`);
  const rows = repo.sql
    .exec(
      `SELECT id FROM record_tags WHERE wiki = 'global' AND tag IN (${tags.map(() => "?").join(",")}) AND kind IN (${buckets.map(() => "?").join(",")})${repo.vis("vis")}
        ORDER BY tag, CAST(substr(id, 2) AS INTEGER) LIMIT 300`,
      ...tags, ...buckets,
    )
    .toArray();
  return rows.map((r) => repo.brief("global", r["id"] as string, headChars)).filter((s): s is Brief => s !== null);
}

/** Every rule and memory at or under the stage: id, summary, then the body indented. */
function baselineLines(recs: Brief[]): string[] {
  const lines: string[] = [];
  for (const r of recs) {
    lines.push(`${r.id} ${line(r.text, 160)}`);
    lines.push(...indented(r.head, BASELINE_BODY_MAX));
  }
  return lines;
}

const SKILL_HEAD = 4000;

/** The active skill versions at or under the stage (the highest active one of each name): name, description, body indented. */
function skillLines(recs: Brief[], bodyCap: number): string[] {
  const best = new Map<string, { version: number; rec: Brief }>();
  for (const s of recs) {
    const h = headOf(s.tags);
    if (h && h.status === "active" && (!best.has(h.name) || best.get(h.name)!.version < h.version)) best.set(h.name, { version: h.version, rec: s });
  }
  const lines: string[] = [];
  for (const [name, { rec }] of [...best.entries()].sort(([a], [b]) => cmp(a, b))) {
    const { fields, body } = splitSkill(rec.head);
    lines.push(`${name} - ${line(fields["description"] ?? "", 300)}`);
    lines.push(...indented(body, bodyCap));
  }
  return lines;
}

export function composeInstructions(
  repo: Repo, d: Device, opts: { client: ClientKind; wiki?: string; budget: number; full?: boolean; stage?: number },
): { text: string; version: string } {
  needScope(d, "read");
  const { min, max } = LIMITS.instructionsBudget;
  if (opts.budget < min || opts.budget > max) fail("invalid", `budget must be between ${min} and ${max} characters`);
  if (opts.stage !== undefined && (!Number.isInteger(opts.stage) || opts.stage < 0 || opts.stage > STAGE_MAX)) fail("invalid", `stage must be a whole number from 0 to ${STAGE_MAX}`);
  if (opts.wiki !== undefined) {
    if (!wikiAllowed(d, opts.wiki)) fail("forbidden", `this device may not use wiki ${opts.wiki}`);
    if (!repo.wiki(opts.wiki)) fail("not_found", `no wiki ${opts.wiki}`);
  }
  // A baseline device reads stage 0 and nothing above it, whatever it asks for. So does a device whose wiki list
  // leaves out `global`: only stage 0 of the global wiki is exempt from the list, the stages above it are not.
  const stage = repo.baseline || !wikiAllowed(d, "global") ? 0 : (opts.stage ?? 0);
  const baseline = repo.baseline;
  const write = opts.client === "mcp" ? (baseline ? WRITE_MCP_BASELINE : WRITE_MCP) : baseline ? WRITE_REST_BASELINE : WRITE_REST;
  const visible = repo.wikis().filter((w) => wikiAllowed(d, w.key) && (opts.wiki === undefined || w.key === opts.wiki || w.key === "global"));
  const hasGlobal = visible.some((w) => w.key === "global");
  const scopeKeys = visible.map((w) => w.key);

  const parts: string[] = [];
  const head = `${section("PRECEDENCE", PRECEDENCE)}\n\n${INTRO}`;
  let room = opts.budget - head.length - write.length - 8;
  // Each section gets a share of what is left after the fixed text, so one long note cannot
  // starve the rules and live features (they keep at least a fifth).
  const total = Math.max(0, room);
  const place = (text: string) => {
    parts.push(text);
    room -= text.length + 2;
  };
  const add = (text: string, share = 1) => {
    const cap = Math.min(room, Math.floor(total * share));
    if (!text || cap < 40) return;
    const clipped = clip(text, cap);
    if (clipped) place(clipped);
  };
  /** A section built from lines that must fit its share: it names how many it left out. */
  const addLines = (title: string, lines: string[], share: number, tail = "") => {
    const cap = Math.min(room, Math.floor(total * share));
    if (!lines.length || cap < 60) return;
    const body = fit(lines, Math.max(0, cap - title.length - 1 - (tail ? tail.length + 1 : 0)));
    if (body) place([title, body, tail].filter(Boolean).join("\n"));
  };

  // The focus first: the northstar, then the fires and only their tree (global first, then the wikis in scope).
  const scope = [...visible].sort((a, b) => Number(b.key === "global") - Number(a.key === "global")).map((w) => w.key);
  const focus = focusSections(repo, scope);
  add(section("NORTHSTAR", fit(focus.north, Math.max(0, Math.floor(total * 0.1)))), 0.1);
  if (focus.fires.length) add(section("FIRES (ranked; each with its open jobs and tasks)", focus.fires.join("\n")), 0.25);
  if (!focus.north.length && !focus.fires.length) {
    // Only a device that may set the focus is told how; a baseline device may not, so it is not pointed at the tool.
    const how = baseline ? "" : " Set them with PUT /v1/wikis/{wiki}/focus (the `focus_set` tool).";
    add(`NORTHSTAR AND FIRES\nNone are set.${how}` + (focus.hints.length ? `\n${focus.hints.join("\n")}` : ""), 0.1);
  } else if (focus.hints.length) {
    add(section("FIRES NOT SET", focus.hints.join("\n")), 0.05);
  }

  // The baseline: every rule and memory of the global wiki at or under the stage, in full, then the active skills.
  // The baseline is the operator's standing text for EVERY authenticated device, so it prints even when the
  // device's wiki list leaves out `global` (the rest of the global wiki stays out of such a device's reach).
  let baselineIds = new Set<string>();
  if (repo.wiki("global")) {
    const recs = stagedRecords(repo, ["R", "M", "E", "C"], stage, BASELINE_BODY_MAX + 100);
    baselineIds = new Set(recs.map((r) => r.id));
    addLines(`BASELINE (stage ${stage}${stage === 0 ? "" : " and below"})`, baselineLines(recs), 0.4);
    const skills = stagedRecords(repo, ["S"], stage, SKILL_HEAD);
    const perSkill = Math.min(SKILL_HEAD - 1100, Math.max(300, Math.floor((total * 0.2) / Math.max(1, skills.length))));
    addLines("BASELINE SKILLS (invoke them when the work calls for them)", skillLines(skills, perSkill), 0.2);
  }

  // The topics: the tag index, so a task that touches one fetches it by tag.
  const topics = tagIndex(repo.sql, baseline, scopeKeys, { limit: TOPICS_MAX, system: false });
  if (topics.length) {
    const lines = topics.map((t) => `  ${line(t.tag, 80)} (rules ${t.counts["rule"] ?? 0}, skills ${t.counts["skill"] ?? 0}, memories ${t.counts["memory"] ?? 0})`);
    addLines("TOPICS", lines, 0.12, TOPIC_NOTE);
  }

  // The projects of the global wiki, the operator's open tasks, and the agent tasks waiting.
  {
    const lines: string[] = [];
    if (hasGlobal) {
      const keys = projectKeys(repo.sql, baseline, "global").sort((a, b) => b.open - a.open || cmp(a.key, b.key));
      for (const p of keys.slice(0, PROJECTS_MAX)) {
        const title = projectHolder(repo, "global", p.key)?.title ?? "";
        lines.push(`  ${line(p.key, 80)}${title && title !== p.key ? ` - ${line(title, 80)}` : ""} (${p.open} open)`);
      }
      if (keys.length > PROJECTS_MAX) lines.push(`  …and ${keys.length - PROJECTS_MAX} more projects`);
    }
    const ref = (t: { wiki: string; id: string; title: string }) => `  ${t.wiki === "global" ? "" : `${t.wiki}/`}${t.id} ${line(t.title, 120)}`;
    const mine = taskRows(repo, scopeKeys, { owner: "human", status: "open", project: "", limit: TASKS_SHOWN });
    const agent = taskRows(repo, scopeKeys, { owner: "agent", status: "open", project: "", limit: AGENT_TASKS_SHOWN });
    const text = [
      ...lines,
      `The operator's open tasks: ${mine.total}`,
      ...mine.tasks.map(ref),
      TASKS_NOTE,
      ...(agent.tasks.length ? ["Agent tasks waiting (oldest first):", ...agent.tasks.map(ref)] : []),
    ].join("\n");
    add(`PROJECTS\n${text}`, 0.12);
  }

  if (hasGlobal) {
    add(section("OPERATOR'S SESSION INSTRUCTIONS", noteBody(repo, "session-instructions")), 0.25);
    add(section("OPERATOR'S STYLE", noteBody(repo, "response-style")), 0.1);
  }

  // Everything else (the rules, the other live features) only when asked for: `full`.
  if (opts.full) {
    const rules: string[] = [];
    // Rules come from the global wiki, plus the wiki the request names: not from every wiki.
    for (const w of visible) {
      if (w.key !== "global" && w.key !== opts.wiki) continue;
      for (const r of repo.live(w.key, ["R"])) {
        if (w.key === "global" && baselineIds.has(r.id)) continue; // already in the baseline, in full
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

  const text = [head, ...parts, write].join("\n\n");
  return { text: text.length <= opts.budget ? text : clip(text, opts.budget), version: `${TEMPLATE_VERSION}.${repo.rev()}` };
}
