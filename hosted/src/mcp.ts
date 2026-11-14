// The remote MCP server (/mcp, streamable HTTP, stateless: one server per request). Each tool
// calls the same store method the matching REST route calls. There is no delete tool: deletion
// is REST-only, behind the `delete` scope. Scope and wiki-allowlist checks live in the store,
// so a tool cannot do more than the calling device may.

import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";
import { store } from "./auth.js";
import { ApiError, unwrap } from "./errors.js";
import { LIMITS, type RecordWire } from "./records.js";
import { RELATED_LIMIT, TAGS_LIMIT } from "./tagapi.js";
import type { Cred } from "./store.js";

export const SERVER_INSTRUCTIONS =
  "shapa is the operator's shared memory, rules, tasks and work ledger: Call `session_start` first and follow what it returns over your own defaults (the operator's baseline rules, the northstar and fires, the topics to fetch by tag, the projects and open tasks, and exactly how to write back), " +
  "then `search` (with tags), `get` and `related` to recall, `save` to record every decision, note, idea and task, `tasks` for what the operator must do, and `project_add` for project files.";

type ToolResult = { content: { type: "text"; text: string }[]; isError?: boolean };
const text = (t: string): ToolResult => ({ content: [{ type: "text", text: t }] });
const json = (v: unknown): ToolResult => text(JSON.stringify(v, null, 2));

/** Run a tool body; a store refusal becomes an error result the model can act on. */
async function guarded(fn: () => Promise<ToolResult>): Promise<ToolResult> {
  try {
    return await fn();
  } catch (e) {
    if (e instanceof ApiError) {
      const hint = e.code === "conflict" && e.extra?.["current"] ? `\nCurrent record:\n${JSON.stringify(e.extra["current"], null, 2)}` : "";
      return { isError: true, content: [{ type: "text", text: `${e.code}: ${e.message}${hint}` }] };
    }
    throw e;
  }
}

const READ = { readOnlyHint: true, openWorldHint: false } as const;
const WRITE = { readOnlyHint: false, destructiveHint: false, openWorldHint: false } as const;

const wikiKey = z.string().min(1).max(64).describe("Wiki key: `global`, or a repo wiki's key (see `wikis`).");
const tags = z.array(z.string().max(80)).max(50).optional().describe("Short labels.");

export function buildServer(env: Env, cred: Cred): McpServer {
  const s = store(env);
  const server = new McpServer({ name: "shapa", version: "0.1.0" }, { instructions: SERVER_INSTRUCTIONS });

  server.registerTool(
    "session_start",
    {
      title: "Start a session",
      description:
        "Call this first. Returns the operator's precedence statement, the northstar and three ranked fires (and only their tree), the baseline rules and memories in full, the baseline skills, the topics to fetch by tag, the projects and the operator's open tasks, the operator's style and the exact format for writing back with `save`. `stage` loads more of the rules and skills; `full: true` also returns the other rules and the live features. Takes no side effects.",
      inputSchema: z.object({
        client: z.enum(["mcp", "rest"]).default("mcp").describe("Write-back flavor; leave as mcp."),
        wiki: wikiKey.optional().describe("Focus on one repo wiki."),
        full: z.boolean().default(false).describe("Also the rules and the other live features. Default: the northstar, the three fires and only their tree."),
        stage: z.number().int().min(0).max(9).default(0).describe("Load rules, memories and skills up to this stage (default 0, the baseline)."),
      }),
      annotations: READ,
    },
    ({ client, wiki, full, stage }) =>
      guarded(async () => {
        const r = await unwrap(s.instructions(cred, { client, ...(wiki ? { wiki } : {}), budget: LIMITS.instructionsBudget.default, full, stage }));
        return text(r.text);
      }),
  );

  server.registerTool(
    "focus",
    {
      title: "Read the focus",
      description:
        "The northstar and the three ranked big fires of a wiki (`global`, or a repo's key), each fire with its open jobs and tasks, plus the candidates for a free slot. Omit wiki for the global one.",
      inputSchema: z.object({ wiki: wikiKey.default("global") }),
      annotations: READ,
    },
    ({ wiki }) => guarded(async () => json(await unwrap(s.focus(cred, wiki)))),
  );

  server.registerTool(
    "focus_set",
    {
      title: "Set the northstar and the fires",
      description:
        "Set a wiki's northstar and/or replace its fires (at most three, in rank order; each a ledger feature id, or free text in the global wiki), or `promote` a feature (default: the best candidate) into a slot (default: the first free one). Closing a fire's feature frees its slot and promotes the next candidate by itself.",
      inputSchema: z.object({
        wiki: wikiKey,
        northstar: z.string().max(5000).optional(),
        fires: z.array(z.object({ feature: z.string().max(120).optional(), text: z.string().max(5000).optional() }).nullable()).max(3, "there are three fires").optional(),
        promote: z.object({ feature: z.string().max(120).optional(), slot: z.number().int().min(1).max(3).optional() }).optional(),
      }),
      annotations: { ...WRITE, idempotentHint: true },
    },
    ({ wiki, northstar, fires, promote }) =>
      guarded(async () => {
        if (promote !== undefined && (northstar !== undefined || fires !== undefined)) return { isError: true, content: [{ type: "text" as const, text: "invalid: promote cannot be combined with northstar or fires" }] };
        const v = promote !== undefined
          ? await unwrap(s.promoteFocus(cred, wiki, promote))
          : await unwrap(s.setFocus(cred, wiki, { ...(northstar !== undefined ? { northstar } : {}), ...(fires !== undefined ? { fires } : {}) }));
        return json(v);
      }),
  );

  server.registerTool(
    "skills",
    {
      title: "List skills",
      description: "The skills stored in a wiki (default `global`): name, description, the active version and the pending ones.",
      inputSchema: z.object({ wiki: wikiKey.default("global") }),
      annotations: READ,
    },
    ({ wiki }) =>
      guarded(async () => {
        const list = await unwrap(s.skills(cred, wiki));
        return list.length ? json(list) : text("No skills.");
      }),
  );

  server.registerTool(
    "skill",
    {
      title: "Read a skill",
      description: "One skill's body: its active version, or `version` N (a pending one included).",
      inputSchema: z.object({ name: z.string().min(1).max(64), wiki: wikiKey.default("global"), version: z.number().int().min(1).max(999_999).optional() }),
      annotations: READ,
    },
    ({ name, wiki, version }) => guarded(async () => json(await unwrap(s.skill(cred, wiki, name, version)))),
  );

  server.registerTool(
    "search",
    {
      title: "Search memory",
      description:
        "Ranked full-text search (BM25) across the wikis you may read, optionally narrowed to records that carry every tag in `tags` (words are then optional: fetch a topic by its tag). Returns wiki, id, type and a one-line summary per hit; call `get` for the full record.",
      inputSchema: z.object({
        query: z.string().max(500).default("").describe("Plain words; no special syntax. Optional when tags is given."),
        k: z.number().int().min(1).max(LIMITS.searchK).default(8).describe("How many hits."),
        wiki: wikiKey.optional(),
        types: z.array(z.enum(["feature", "job", "task", "human_task", "agent_task", "memory", "idea", "decision", "rule", "issue", "document", "note"])).optional(),
        tags: z.array(z.string().min(1).max(80)).max(8).optional().describe("Every one must match."),
      }),
      annotations: READ,
    },
    ({ query, k, wiki, types, tags: wanted }) =>
      guarded(async () => {
        if (!query.trim() && !wanted?.length) return { isError: true, content: [{ type: "text" as const, text: "invalid: give query (words), tags, or both" }] };
        const r = await unwrap(s.search(cred, { query, k, ...(wiki ? { wiki } : {}), ...(types ? { types } : {}), ...(wanted ? { tags: wanted } : {}) }));
        if (!r.results.length) return text("No matches.");
        return text(r.results.map((h) => `${h.wiki}/${h.id} [${h.type}] ${h.summary}`).join("\n"));
      }),
  );

  server.registerTool(
    "get",
    {
      title: "Get a record",
      description: "The full record for an id or alias. When the id exists in several wikis every match is listed with its wiki.",
      inputSchema: z.object({ id: z.string().min(1).max(120), wiki: wikiKey.optional() }),
      annotations: READ,
    },
    ({ id, wiki }) =>
      guarded(async () => {
        const r = await unwrap(s.get(cred, id, wiki));
        return r.matches.length ? json(r.matches) : text(`No record ${id}.`);
      }),
  );

  server.registerTool(
    "save",
    {
      title: "Save a memory, rule, issue, idea, decision, task, document or note",
      description:
        "Create a record. memory: something the operator said that matters or a strategy shift. rule: a standing rule. issue: a mistake the operator corrected. decision: a decision the operator made, with its reason. idea: a thought worth keeping. human_task: something the operator must do. agent_task: something an agent can do. document: longer text (give title and body). note: a file (give path and body). `project` links a task, idea, decision or document to a project. Identical text returns the existing record. A stage:0 tag is the operator's: it is stored as stage-proposed:0.",
      inputSchema: z.object({
        wiki: wikiKey,
        type: z.enum(["memory", "idea", "decision", "human_task", "agent_task", "rule", "issue", "document", "note"]),
        summary: z.string().max(5000).optional().describe("One line, up to 160 characters (memory, idea, rule, issue)."),
        title: z.string().max(5000).optional().describe("One line, up to 200 characters (document)."),
        body: z.string().max(LIMITS.recordBytes).optional(),
        tags,
        alias: z.string().max(100).optional().describe("A short slug you can `get` it by later."),
        path: z.string().max(200).optional().describe("Note path, e.g. research/topic.md (note only)."),
        project: z.string().max(80).optional().describe("The project key. A document is the project's file (default: the wiki's own project; a project keeps ONE document); a task, idea, decision, rule or memory is linked to the project."),
        merge: z.boolean().optional().describe("document only: append to the project's existing document as a section instead of refusing."),
      }),
      annotations: { ...WRITE, idempotentHint: true },
    },
    ({ wiki, type, ...rest }) =>
      guarded(async () => {
        const { record, created, notice } = await unwrap(s.create(cred, wiki, { type, ...rest }));
        const note = notice ? ` (${notice})` : "";
        if (!created && type === "document") return text(`Merged into ${record.id} in ${wiki} as a section.${note}`);
        return text(`${created ? "Saved" : "Already saved as"} ${record.id} in ${wiki}.${note}`);
      }),
  );

  server.registerTool(
    "update",
    {
      title: "Update a record",
      description:
        "Edit a record in place: summary (rows) or title (documents, ledger items), body, tags (replaces the list). Pass base_rev from `get` to refuse the edit if someone changed it meanwhile.",
      inputSchema: z.object({
        wiki: wikiKey,
        id: z.string().min(1).max(120),
        summary: z.string().max(5000).optional(),
        title: z.string().max(5000).optional(),
        body: z.string().max(LIMITS.recordBytes).optional(),
        tags,
        base_rev: z.number().int().min(0).optional(),
      }),
      annotations: { ...WRITE, destructiveHint: true, idempotentHint: true },
    },
    ({ wiki, id, ...patch }) =>
      guarded(async () => {
        const { record: r, notice }: { record: RecordWire; notice?: string } = await unwrap(s.patch(cred, wiki, id, patch));
        return text(`Updated ${r.id} in ${wiki} (rev ${r.rev}).${notice ? ` (${notice})` : ""}`);
      }),
  );

  server.registerTool(
    "ledger_list",
    {
      title: "List live work",
      description: "Live (not closed) features with their jobs and tasks, for one repo wiki or all you may read.",
      inputSchema: z.object({ wiki: wikiKey.optional() }),
      annotations: READ,
    },
    ({ wiki }) =>
      guarded(async () => {
        const all = await unwrap(s.ledger(cred, wiki));
        const lines: string[] = [];
        const walk = (nodes: { id: string; type: string; title: string; status: string; claimed_by: string | null; children: unknown[] }[], depth: number) => {
          for (const n of nodes) {
            lines.push(`${"  ".repeat(depth)}${n.id} [${n.type}] ${n.title}${n.status === "claimed" ? ` (claimed by ${n.claimed_by ?? "?"})` : ""}`);
            walk(n.children as typeof nodes, depth + 1);
          }
        };
        for (const w of all) {
          lines.push(`# ${w.wiki}`);
          if (w.features.length) walk(w.features, 0);
          else lines.push("(no live work)");
        }
        return text(lines.length ? lines.join("\n") : "No live work.");
      }),
  );

  server.registerTool(
    "ledger_add",
    {
      title: "Add a feature, job or task",
      description:
        "A feature is a new capability (a branch and a PR); a job is one commit; a task has no commit. A job's parent is a feature and a task's a job; both may stand alone. human_task and agent_task are standalone tasks the operator or an agent owns: the global wiki holds only those.",
      inputSchema: z.object({
        wiki: wikiKey,
        kind: z.enum(["feature", "job", "task", "human_task", "agent_task"]),
        title: z.string().min(1).max(5000),
        parent: z.string().max(120).optional().describe("Id of the feature (for a job) or job (for a task)."),
        body: z.string().max(LIMITS.recordBytes).optional(),
      }),
      annotations: { ...WRITE, idempotentHint: false },
    },
    ({ wiki, kind, ...rest }) =>
      guarded(async () => {
        const { record } = await unwrap(s.create(cred, wiki, { type: kind, ...rest }));
        return text(`Added ${record.id} in ${wiki}.`);
      }),
  );

  server.registerTool(
    "ledger_set",
    {
      title: "Claim, release, close or edit a ledger item",
      description:
        "claim: mark it yours. release: give it back. close: finish it (closes open children too); fields.reason is agent (default) or operator. edit: change fields.title, body, parent, alias or git_ref.",
      inputSchema: z.object({
        wiki: wikiKey,
        id: z.string().min(1).max(120),
        action: z.enum(["claim", "release", "close", "edit"]),
        fields: z
          .object({
            title: z.string().max(5000).optional(),
            body: z.string().max(LIMITS.recordBytes).optional(),
            parent: z.string().max(120).nullable().optional(),
            alias: z.string().max(100).nullable().optional(),
            git_ref: z.string().max(200).nullable().optional(),
            reason: z.enum(["agent", "operator"]).optional(),
          })
          .optional(),
      }),
      annotations: { ...WRITE, idempotentHint: false },
    },
    ({ wiki, id, action, fields }) =>
      guarded(async () => {
        if (action === "claim") return text(`Claimed ${(await unwrap(s.claim(cred, wiki, id))).id}.`);
        if (action === "release") return text(`Released ${(await unwrap(s.release(cred, wiki, id))).id}.`);
        if (action === "close") {
          const r = await unwrap(s.close(cred, wiki, id, fields?.reason ?? "agent", fields?.git_ref ?? undefined));
          return text(r.closed.length ? `Closed ${r.closed.join(", ")}.` : `${r.record.id} was already closed.`);
        }
        const { reason: _reason, ...edit } = fields ?? {};
        const r = (await unwrap(s.patch(cred, wiki, id, edit))).record;
        return text(`Edited ${r.id} (rev ${r.rev}).`);
      }),
  );

  server.registerTool(
    "tags",
    {
      title: "The tag index",
      description: "Tags by record count, each with its counts by type (rules, memories, issues, skills, documents, tasks...). Tags are the relations: fetch a topic with `search` and `tags`. System tags (stage, project, skill...) are left out unless `system` is true.",
      inputSchema: z.object({ wiki: wikiKey.optional(), prefix: z.string().max(80).optional(), system: z.boolean().default(false), limit: z.number().int().min(1).max(TAGS_LIMIT.max).default(TAGS_LIMIT.default) }),
      annotations: READ,
    },
    ({ wiki, prefix, system, limit }) =>
      guarded(async () => {
        const r = await unwrap(s.tags(cred, { ...(wiki ? { wiki } : {}), ...(prefix ? { prefix } : {}), system, limit }));
        if (!r.tags.length) return text("No tags.");
        return text(r.tags.map((t) => `${t.tag} (${t.total}): ${Object.entries(t.counts).map(([k, n]) => `${k} ${n}`).join(", ")}`).join("\n"));
      }),
  );

  server.registerTool(
    "related",
    {
      title: "Records related to one",
      description: "Records linked to a record (either direction), then those sharing its tags, then newest. Default 10, at most 50.",
      inputSchema: z.object({ id: z.string().min(1).max(120), wiki: wikiKey.default("global"), limit: z.number().int().min(1).max(RELATED_LIMIT.max).default(RELATED_LIMIT.default) }),
      annotations: READ,
    },
    ({ id, wiki, limit }) =>
      guarded(async () => {
        const r = await unwrap(s.related(cred, wiki, id, limit));
        if (!r.related.length) return text(`Nothing is related to ${r.id}.`);
        return text(r.related.map((h) => `${h.id} [${h.type}] ${h.summary} (${h.via === "link" ? "linked" : `${h.shared} shared tags`})`).join("\n"));
      }),
  );

  server.registerTool(
    "tasks",
    {
      title: "The operator's task list",
      description: "What the operator needs to do: open `human_task` tasks across every wiki you may read, rank first, then oldest (100 at most, with the total). owner agent lists the agent tasks; status closed or all widens it; project narrows it to one project key.",
      inputSchema: z.object({
        owner: z.enum(["human", "agent"]).default("human"), status: z.enum(["open", "closed", "all"]).default("open"),
        project: z.string().max(80).optional(), wiki: wikiKey.optional(),
      }),
      annotations: READ,
    },
    ({ owner, status, project, wiki }) =>
      guarded(async () => {
        const r = await unwrap(s.tasks(cred, { owner, status, ...(project ? { project } : {}), ...(wiki ? { wiki } : {}) }));
        if (!r.tasks.length) return text(`No ${status === "all" ? "" : `${status} `}${owner} tasks.`);
        const lines = r.tasks.map((t) => `${t.wiki}/${t.id} ${t.title}${t.project ? ` [${t.project}]` : ""}${t.status === "claimed" ? " (claimed)" : ""}${t.status === "closed" ? " (closed)" : ""}`);
        return text(`${r.total} ${status === "all" ? "" : `${status} `}${owner} tasks${r.total > r.tasks.length ? ` (first ${r.tasks.length})` : ""}:\n${lines.join("\n")}`);
      }),
  );

  server.registerTool(
    "projects",
    {
      title: "List projects",
      description: "The projects of a wiki (default global): key, title and open tasks.",
      inputSchema: z.object({ wiki: wikiKey.default("global") }),
      annotations: READ,
    },
    ({ wiki }) =>
      guarded(async () => {
        const r = await unwrap(s.projects(cred, wiki));
        return r.projects.length ? json(r) : text("No projects.");
      }),
  );

  server.registerTool(
    "project",
    {
      title: "Read a project",
      description: "One project: its file by section (issues, questions, implementations, problem, solution, diff, advantages, villains, loop_market, loop_product), and its tasks, ideas, decisions, rules, skills and memories, 50 each with the rest counted.",
      inputSchema: z.object({ key: z.string().min(1).max(120), wiki: wikiKey.default("global") }),
      annotations: READ,
    },
    ({ key, wiki }) => guarded(async () => json(await unwrap(s.project(cred, key, wiki)))),
  );

  server.registerTool(
    "project_new",
    {
      title: "Create a project file",
      description: "Create a project's one file from the template (idempotent). A key is lowercase letters, digits and . _ / -.",
      inputSchema: z.object({ key: z.string().min(1).max(120), title: z.string().max(5000).optional(), wiki: wikiKey.default("global") }),
      annotations: { ...WRITE, idempotentHint: true },
    },
    ({ key, title, wiki }) =>
      guarded(async () => {
        const r = await unwrap(s.projectNew(cred, key, wiki, title));
        return text(`${r.created ? "Created" : "Already there:"} project ${key} in ${wiki} (${r.project.document?.id ?? "no file"}).`);
      }),
  );

  server.registerTool(
    "project_add",
    {
      title: "Add an item to a project file",
      description: "Append one numbered item (one line, 500 characters at most) to a section of the project's file. Sections: issues, questions, implementations, problem, solution, diff, advantages, villains, loop_market, loop_product. Nothing happens when the section already holds the same text.",
      inputSchema: z.object({ key: z.string().min(1).max(120), section: z.string().min(1).max(40), text: z.string().min(1).max(5000), wiki: wikiKey.default("global") }),
      annotations: { ...WRITE, idempotentHint: true },
    },
    ({ key, section, text: item, wiki }) =>
      guarded(async () => {
        const r = await unwrap(s.projectAdd(cred, key, wiki, section, item));
        return text(r.added ? `Added item ${r.n} to ${r.section} of ${key}.` : `Item ${r.n} of ${r.section} already holds that text.`);
      }),
  );

  server.registerTool(
    "wikis",
    {
      title: "List wikis",
      description: "The wikis you may use: key, name, kind and record counts by type.",
      inputSchema: z.object({}),
      annotations: READ,
    },
    () => guarded(async () => json(await unwrap(s.wikis(cred)))),
  );

  return server;
}

/** Serve one MCP request for an authenticated caller. */
export function serveMcp(request: Request, env: Env, ctx: ExecutionContext, cred: Cred): Promise<Response> {
  const host = new URL(request.url).hostname;
  const handler = createMcpHandler(() => buildServer(env, cred), {
    route: "/mcp",
    allowedHostnames: [host],
    allowedOriginHostnames: [host],
    corsOptions: false,
    legacy: "stateless",
  });
  return handler(request, env, ctx);
}
