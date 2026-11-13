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
import type { Cred } from "./store.js";

export const SERVER_INSTRUCTIONS =
  "shapa is the operator's shared memory and work ledger. Call `session_start` first: it returns the operator's standing instructions, rules, live work and exactly how to write back. " +
  "Then use `search` and `get` to recall, `save` to record what matters, and `ledger_list` / `ledger_add` / `ledger_set` for work.";

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
        "Call this first. Returns the operator's session instructions, style, rules, live features, and the exact format for writing back with `save`. Takes no side effects.",
      inputSchema: z.object({
        client: z.enum(["mcp", "rest"]).default("mcp").describe("Write-back flavor; leave as mcp."),
        wiki: wikiKey.optional().describe("Focus on one repo wiki."),
      }),
      annotations: READ,
    },
    ({ client, wiki }) =>
      guarded(async () => {
        const r = await unwrap(s.instructions(cred, { client, ...(wiki ? { wiki } : {}), budget: LIMITS.instructionsBudget.default }));
        return text(r.text);
      }),
  );

  server.registerTool(
    "search",
    {
      title: "Search memory",
      description:
        "Ranked full-text search (BM25) across the wikis you may read. Returns wiki, id, type and a one-line summary per hit; call `get` for the full record.",
      inputSchema: z.object({
        query: z.string().min(1).max(500).describe("Plain words; no special syntax."),
        k: z.number().int().min(1).max(LIMITS.searchK).default(8).describe("How many hits."),
        wiki: wikiKey.optional(),
        types: z.array(z.enum(["feature", "job", "task", "memory", "idea", "rule", "issue", "document", "note"])).optional(),
      }),
      annotations: READ,
    },
    ({ query, k, wiki, types }) =>
      guarded(async () => {
        const r = await unwrap(s.search(cred, { query, k, ...(wiki ? { wiki } : {}), ...(types ? { types } : {}) }));
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
      title: "Save a memory, rule, issue, idea, document or note",
      description:
        "Create a record. memory: something the operator said that matters or a strategy shift. rule: a standing rule. issue: a mistake the operator corrected. idea: a thought worth keeping. document: longer text (give title and body). note: a file (give path and body). Identical memory/rule/issue text returns the existing record.",
      inputSchema: z.object({
        wiki: wikiKey,
        type: z.enum(["memory", "idea", "rule", "issue", "document", "note"]),
        summary: z.string().max(5000).optional().describe("One line, up to 160 characters (memory, idea, rule, issue)."),
        title: z.string().max(5000).optional().describe("One line, up to 200 characters (document)."),
        body: z.string().max(LIMITS.recordBytes).optional(),
        tags,
        alias: z.string().max(100).optional().describe("A short slug you can `get` it by later."),
        path: z.string().max(200).optional().describe("Note path, e.g. research/topic.md (note only)."),
      }),
      annotations: { ...WRITE, idempotentHint: true },
    },
    ({ wiki, type, ...rest }) =>
      guarded(async () => {
        const { record, created } = await unwrap(s.create(cred, wiki, { type, ...rest }));
        return text(`${created ? "Saved" : "Already saved as"} ${record.id} in ${wiki}.`);
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
        const r: RecordWire = await unwrap(s.patch(cred, wiki, id, patch));
        return text(`Updated ${r.id} in ${wiki} (rev ${r.rev}).`);
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
        "A feature is a new capability (a branch and a PR); a job is one commit; a task has no commit. A job's parent is a feature and a task's a job; both may stand alone.",
      inputSchema: z.object({
        wiki: wikiKey,
        kind: z.enum(["feature", "job", "task"]),
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
        const r = await unwrap(s.patch(cred, wiki, id, edit));
        return text(`Edited ${r.id} (rev ${r.rev}).`);
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
