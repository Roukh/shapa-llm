// REST (/v1): one route per contract row, declared with zod so validation, types and the
// generated /openapi.json come from the same schemas in records.ts. Every handler is a thin call
// into a store method; scope and wiki-allowlist checks happen in the store.

import { createRoute, z, type OpenAPIHono } from "@hono/zod-openapi";
import { store } from "./auth.js";
import { unwrap } from "./errors.js";
import {
  CloseBody, CreateBody, ErrorBody, FocusOut, FocusPromote, FocusPut, KINDS, LIMITS, PatchBody, ProjectCardOut, ProjectItemBody, ProjectItemOut,
  ProjectNewBody, ProjectOut, ProjectsOut, RecordOut, RecordWithWiki, RelatedOut, SearchOut, SkillSummary, SkillVersionOut, SyncRequest, SyncResponse,
  TagsOut, TasksOut, WIKI_KEY_RE, WikiOut, WikiPut, type Kind,
} from "./records.js";
import { RELATED_LIMIT, TAGS_LIMIT } from "./tagapi.js";
import { TASKS_LIMIT } from "./tasks.js";
import { STAGE_MAX } from "./tags.js";
import type { Cred } from "./store.js";

export interface AppEnv {
  Bindings: Env;
  Variables: { cred: Cred };
}
export type App = OpenAPIHono<AppEnv>;

const body = <T extends z.ZodType>(schema: T, description: string) => ({ content: { "application/json": { schema } }, description });
const err = (description: string) => body(ErrorBody, description);
const errors = {
  400: err("Invalid request"),
  401: err("Missing, wrong or revoked credential"),
  403: err("Scope or wiki not allowed for this device"),
};

const wikiParam = z.string().regex(WIKI_KEY_RE).openapi({ param: { name: "wiki", in: "path" }, example: "global" });
const idParam = z.string().min(1).max(120).openapi({ param: { name: "id", in: "path" }, description: "Record id or alias, URL-encoded." });
const security = [{ bearer: [] }];

const intQuery = (min: number, max: number, dflt: number) => z.coerce.number().int().min(min).max(max).default(dflt);

export function registerV1(app: App): void {
  app.openAPIRegistry.registerComponent("securitySchemes", "bearer", { type: "http", scheme: "bearer" });
  const tag = ["v1"];
  const cred = (c: { get(k: "cred"): Cred }): Cred => c.get("cred");

  app.openapi(
    createRoute({
      method: "get", path: "/v1/whoami", tags: tag, security, summary: "The calling device",
      responses: {
        200: body(z.object({ id: z.string(), name: z.string(), scopes: z.array(z.string()), wikis: z.array(z.string()).nullable(), reach: z.enum(["full", "baseline"]) }), "The device"),
        ...errors,
      },
    }),
    async (c) => {
      const d = await unwrap(store(c.env).whoami(cred(c)));
      return c.json({ id: d.id, name: d.name, scopes: d.scopes, wikis: d.wikis, reach: d.reach }, 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/wikis", tags: tag, security, summary: "Wikis this device may use",
      responses: { 200: body(z.object({ wikis: z.array(WikiOut) }), "Wikis"), ...errors },
    }),
    async (c) => c.json({ wikis: await unwrap(store(c.env).wikis(cred(c))) }, 200),
  );

  app.openapi(
    createRoute({
      method: "put", path: "/v1/wikis/{wiki}", tags: tag, security, summary: "Register a wiki (idempotent)",
      request: { params: z.object({ wiki: wikiParam }), body: { content: { "application/json": { schema: WikiPut } }, required: true } },
      responses: { 200: body(WikiOut, "Registered or updated"), 201: body(WikiOut, "Created"), 409: err("Key belongs to another origin"), ...errors },
    }),
    async (c) => {
      const { wiki } = c.req.valid("param");
      const r = await unwrap(store(c.env).putWiki(cred(c), wiki, c.req.valid("json")));
      return c.json(r.wiki, r.created ? 201 : 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/instructions", tags: tag, security, summary: "Session defaults for a client with no hooks",
      request: {
        query: z.object({
          client: z.enum(["mcp", "rest"]).default("rest"),
          wiki: z.string().regex(WIKI_KEY_RE).optional(),
          budget: intQuery(LIMITS.instructionsBudget.min, LIMITS.instructionsBudget.max, LIMITS.instructionsBudget.default),
          full: z.enum(["0", "1", "true", "false"]).optional().openapi({ description: "1 adds the rules and the other live features to the focus." }),
          stage: z.coerce.number().int().min(0).max(STAGE_MAX).default(0).openapi({ description: "Load the rules, memories and skills up to this stage (default 0: the baseline). A baseline-reach device always gets 0." }),
        }),
      },
      responses: {
        200: {
          description: "Text, within the budget (send Accept: text/plain for the text alone)",
          content: {
            "application/json": { schema: z.object({ text: z.string(), version: z.string() }) },
            "text/plain": { schema: z.string() },
          },
        },
        ...errors,
      },
    }),
    async (c) => {
      const q = c.req.valid("query");
      const full = q.full === "1" || q.full === "true";
      const r = await unwrap(store(c.env).instructions(cred(c), { client: q.client, ...(q.wiki ? { wiki: q.wiki } : {}), budget: q.budget, full, stage: q.stage }));
      if ((c.req.header("accept") ?? "").includes("text/plain")) return c.text(r.text, 200) as never;
      return c.json(r, 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/wikis/{wiki}/focus", tags: tag, security,
      summary: "The wiki's northstar and three ranked fires, each with its open jobs and tasks",
      request: { params: z.object({ wiki: wikiParam }) },
      responses: { 200: body(FocusOut, "The focus, and the candidates for a free slot"), 404: err("No such wiki"), ...errors },
    }),
    async (c) => c.json(await unwrap(store(c.env).focus(cred(c), c.req.valid("param").wiki)) as never, 200),
  );

  app.openapi(
    createRoute({
      method: "put", path: "/v1/wikis/{wiki}/focus", tags: tag, security,
      summary: "Set the northstar and/or replace the fires (at most three; a feature or free text each)",
      request: { params: z.object({ wiki: wikiParam }), body: { content: { "application/json": { schema: FocusPut } }, required: true } },
      responses: { 200: body(FocusOut, "The focus"), 404: err("No such wiki"), 409: err("A feature is closed or already a fire"), ...errors },
    }),
    async (c) => c.json(await unwrap(store(c.env).setFocus(cred(c), c.req.valid("param").wiki, c.req.valid("json"))) as never, 200),
  );

  app.openapi(
    createRoute({
      method: "post", path: "/v1/wikis/{wiki}/focus/promote", tags: tag, security,
      summary: "Put a feature (default: the best candidate) in a fire slot (default: the first free one)",
      request: { params: z.object({ wiki: wikiParam }), body: { content: { "application/json": { schema: FocusPromote } }, required: true } },
      responses: { 200: body(FocusOut, "The focus"), 404: err("No candidate or no such wiki"), 409: err("No free slot, or the feature is closed or already a fire"), ...errors },
    }),
    async (c) => c.json(await unwrap(store(c.env).promoteFocus(cred(c), c.req.valid("param").wiki, c.req.valid("json"))) as never, 200),
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/wikis/{wiki}/skills", tags: tag, security, summary: "The wiki's skills: the active and pending versions of each",
      request: { params: z.object({ wiki: wikiParam }) },
      responses: { 200: body(z.object({ skills: z.array(SkillSummary) }), "Skills"), 404: err("No such wiki"), ...errors },
    }),
    async (c) => c.json({ skills: await unwrap(store(c.env).skills(cred(c), c.req.valid("param").wiki)) }, 200),
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/wikis/{wiki}/skills/{name}", tags: tag, security,
      summary: "One skill: its active version, or ?version=N",
      request: {
        params: z.object({ wiki: wikiParam, name: z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/).openapi({ param: { name: "name", in: "path" } }) }),
        query: z.object({ version: z.coerce.number().int().min(1).max(999_999).optional() }),
      },
      responses: { 200: body(SkillVersionOut, "The version"), 404: err("No such skill or version"), ...errors },
    }),
    async (c) => {
      const { wiki, name } = c.req.valid("param");
      const v = c.req.valid("query").version;
      return c.json(await unwrap(store(c.env).skill(cred(c), wiki, name, v)) as never, 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/search", tags: tag, security, summary: "BM25 search over live records, and by tag",
      request: {
        query: z.object({
          q: z.string().max(500).optional().openapi({ description: "Words. Optional when tags is given." }),
          k: intQuery(1, LIMITS.searchK, 8),
          wiki: z.string().regex(WIKI_KEY_RE).optional(),
          types: z.string().max(200).optional().openapi({ description: "Comma-separated type names." }),
          tags: z.string().max(700).optional().openapi({ description: "Comma-separated tags, at most 8; every one must match. A lower stage ranks first on a tie." }),
        }),
      },
      responses: { 200: body(SearchOut, "Hits"), ...errors },
    }),
    async (c) => {
      const q = c.req.valid("query");
      const types = q.types ? q.types.split(",").map((t) => t.trim()).filter(Boolean) : undefined;
      const tags = q.tags ? q.tags.split(",").map((t) => t.trim()).filter(Boolean) : undefined;
      if (!q.q?.trim() && !tags?.length) return c.json({ error: { code: "invalid", message: "give q (words), tags, or both" } }, 400) as never;
      const r = await unwrap(store(c.env).search(cred(c), {
        query: q.q ?? "", k: q.k, ...(q.wiki ? { wiki: q.wiki } : {}), ...(types ? { types } : {}), ...(tags ? { tags } : {}),
      }));
      return c.json(r, 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/tags", tags: tag, security,
      summary: "The tag index: tags by record count, each with its counts by type (system tags left out unless asked)",
      request: {
        query: z.object({
          wiki: z.string().regex(WIKI_KEY_RE).optional().openapi({ description: "One wiki (default: every wiki this device may read)." }),
          prefix: z.string().max(80).optional(),
          system: z.enum(["0", "1", "true", "false"]).optional().openapi({ description: "1 lists stage:*, project:*, skill:* and the other system tags too." }),
          limit: intQuery(1, TAGS_LIMIT.max, TAGS_LIMIT.default),
        }),
      },
      responses: { 200: body(TagsOut, "The tags"), 404: err("No such wiki"), ...errors },
    }),
    async (c) => {
      const q = c.req.valid("query");
      return c.json(await unwrap(store(c.env).tags(cred(c), {
        ...(q.wiki ? { wiki: q.wiki } : {}), ...(q.prefix ? { prefix: q.prefix } : {}), system: q.system === "1" || q.system === "true", limit: q.limit,
      })), 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/wikis/{wiki}/records/{id}/related", tags: tag, security,
      summary: "Records related to one: linked to it (either direction), then by shared tags, then newest",
      request: { params: z.object({ wiki: wikiParam, id: idParam }), query: z.object({ limit: intQuery(1, RELATED_LIMIT.max, RELATED_LIMIT.default) }) },
      responses: { 200: body(RelatedOut, "The related records"), 404: err("Not found"), ...errors },
    }),
    async (c) => {
      const { wiki, id } = c.req.valid("param");
      return c.json(await unwrap(store(c.env).related(cred(c), wiki, id, c.req.valid("query").limit)), 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/tasks", tags: tag, security,
      summary: "The operator's task list: open tasks of an owner (default human) in every wiki this device may read, rank first, then oldest",
      request: {
        query: z.object({
          owner: z.enum(["human", "agent"]).default("human"),
          status: z.enum(["open", "closed", "all"]).default("open"),
          project: z.string().max(80).optional().openapi({ description: "Only tasks of this project key." }),
          wiki: z.string().regex(WIKI_KEY_RE).optional(),
          limit: intQuery(1, TASKS_LIMIT, TASKS_LIMIT),
        }),
      },
      responses: { 200: body(TasksOut, "Tasks and how many match in all"), 404: err("No such wiki"), ...errors },
    }),
    async (c) => {
      const q = c.req.valid("query");
      return c.json(await unwrap(store(c.env).tasks(cred(c), {
        owner: q.owner, status: q.status, limit: q.limit, ...(q.project ? { project: q.project } : {}), ...(q.wiki ? { wiki: q.wiki } : {}),
      })), 200);
    },
  );

  const projectKeyParam = z.string().min(1).max(120).openapi({ param: { name: "key", in: "path" }, description: "Project key: lowercase letters, digits and . _ / - (URL-encode a slash)." });
  const projectWiki = z.string().regex(WIKI_KEY_RE).default("global").openapi({ description: "The wiki holding the project (default: global)." });

  app.openapi(
    createRoute({
      method: "get", path: "/v1/projects", tags: tag, security, summary: "The projects of a wiki: key, title, open tasks",
      request: { query: z.object({ wiki: projectWiki }) },
      responses: { 200: body(ProjectsOut, "Projects, the busiest first"), 404: err("No such wiki"), ...errors },
    }),
    async (c) => c.json(await unwrap(store(c.env).projects(cred(c), c.req.valid("query").wiki)), 200),
  );

  app.openapi(
    createRoute({
      method: "put", path: "/v1/projects/{key}", tags: tag, security, summary: "Create a project's file from the template (idempotent)",
      request: { params: z.object({ key: projectKeyParam }), body: { content: { "application/json": { schema: ProjectNewBody } }, required: false } },
      responses: { 200: body(ProjectCardOut, "Already there"), 201: body(ProjectCardOut, "Created"), 404: err("No such wiki"), ...errors },
    }),
    async (c) => {
      const { key } = c.req.valid("param");
      const b = c.req.valid("json") ?? {};
      const r = await unwrap(store(c.env).projectNew(cred(c), key, b.wiki ?? "global", b.title));
      return c.json(r.project, r.created ? 201 : 200);
    },
  );

  app.openapi(
    createRoute({
      method: "post", path: "/v1/projects/{key}/items", tags: tag, security,
      summary: "Append one numbered item to a section of the project's file (no-op when it is already there)",
      request: { params: z.object({ key: projectKeyParam }), body: { content: { "application/json": { schema: ProjectItemBody } }, required: true } },
      responses: { 200: body(ProjectItemOut, "Already there"), 201: body(ProjectItemOut, "Added"), 404: err("No such project"), 413: err("The file would pass 1 MB"), ...errors },
    }),
    async (c) => {
      const { key } = c.req.valid("param");
      const b = c.req.valid("json");
      const r = await unwrap(store(c.env).projectAdd(cred(c), key, b.wiki ?? "global", b.section, b.text));
      return c.json(r, r.added ? 201 : 200) as never;
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/projects/{key}", tags: tag, security,
      summary: "One project: its file by section, and every task, idea, decision, rule, skill and memory that carries project:KEY (50 each, with the rest counted)",
      request: { params: z.object({ key: projectKeyParam }), query: z.object({ wiki: projectWiki }) },
      responses: { 200: body(ProjectOut, "The project"), 404: err("No such project"), ...errors },
    }),
    async (c) => c.json(await unwrap(store(c.env).project(cred(c), c.req.valid("param").key, c.req.valid("query").wiki)) as never, 200),
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/get", tags: tag, security, summary: "A record by id or alias across wikis",
      request: { query: z.object({ id: z.string().min(1).max(120), wiki: z.string().regex(WIKI_KEY_RE).optional() }) },
      responses: { 200: body(z.object({ matches: z.array(RecordWithWiki) }), "Matches (maybe none)"), ...errors },
    }),
    async (c) => {
      const q = c.req.valid("query");
      return c.json(await unwrap(store(c.env).get(cred(c), q.id, q.wiki)), 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/wikis/{wiki}/records", tags: tag, security, summary: "List live records",
      request: {
        params: z.object({ wiki: wikiParam }),
        query: z.object({
          kind: z.enum(KINDS).optional(), status: z.enum(["open", "claimed", "closed"]).optional(),
          tag: z.string().max(80).optional(), after: intQuery(0, Number.MAX_SAFE_INTEGER, 0), limit: intQuery(1, LIMITS.listLimit, 50),
        }),
      },
      responses: { 200: body(z.object({ records: z.array(RecordOut), next: z.number().int().nullable() }), "A page; pass next as after"), 404: err("No such wiki"), ...errors },
    }),
    async (c) => {
      const q = c.req.valid("query");
      const f: { kind?: Kind; status?: string; tag?: string; after: number; limit: number } = { after: q.after, limit: q.limit };
      if (q.kind) f.kind = q.kind;
      if (q.status) f.status = q.status;
      if (q.tag) f.tag = q.tag;
      return c.json(await unwrap(store(c.env).list(cred(c), c.req.valid("param").wiki, f)), 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/wikis/{wiki}/records/{id}", tags: tag, security, summary: "One record",
      request: { params: z.object({ wiki: wikiParam, id: idParam }) },
      responses: { 200: body(RecordOut, "The record"), 404: err("Not found"), ...errors },
    }),
    async (c) => {
      const { wiki, id } = c.req.valid("param");
      return c.json(await unwrap(store(c.env).record(cred(c), wiki, id)), 200);
    },
  );

  app.openapi(
    createRoute({
      method: "post", path: "/v1/wikis/{wiki}/records", tags: tag, security, summary: "Create a record; the server allocates the id",
      request: { params: z.object({ wiki: wikiParam }), body: { content: { "application/json": { schema: CreateBody } }, required: true } },
      responses: { 200: body(RecordOut, "Identical row text already stored"), 201: body(RecordOut, "Created"), 404: err("No such wiki or parent"), 409: err("Conflict"), 413: err("Too large"), ...errors },
    }),
    async (c) => {
      const r = await unwrap(store(c.env).create(cred(c), c.req.valid("param").wiki, c.req.valid("json")));
      if (r.notice) c.header("X-Shapa-Notice", r.notice);
      return c.json(r.record, r.created ? 201 : 200);
    },
  );

  app.openapi(
    createRoute({
      method: "patch", path: "/v1/wikis/{wiki}/records/{id}", tags: tag, security, summary: "Edit fields; base_rev guards against a lost update",
      request: { params: z.object({ wiki: wikiParam, id: idParam }), body: { content: { "application/json": { schema: PatchBody } }, required: true } },
      responses: { 200: body(RecordOut, "The record"), 404: err("Not found"), 409: err("base_rev mismatch (current record in error.current) or alias taken"), ...errors },
    }),
    async (c) => {
      const { wiki, id } = c.req.valid("param");
      const r = await unwrap(store(c.env).patch(cred(c), wiki, id, c.req.valid("json")));
      if (r.notice) c.header("X-Shapa-Notice", r.notice);
      return c.json(r.record, 200);
    },
  );

  for (const action of ["claim", "release"] as const) {
    app.openapi(
      createRoute({
        method: "post", path: `/v1/wikis/{wiki}/records/{id}/${action}`, tags: tag, security, summary: `${action} a ledger item`,
        request: { params: z.object({ wiki: wikiParam, id: idParam }) },
        responses: { 200: body(RecordOut, "The record"), 404: err("Not found"), 409: err("Item is closed"), ...errors },
      }),
      async (c) => {
        const { wiki, id } = c.req.valid("param");
        const s = store(c.env);
        return c.json(await unwrap(action === "claim" ? s.claim(cred(c), wiki, id) : s.release(cred(c), wiki, id)), 200);
      },
    );
  }

  app.openapi(
    createRoute({
      method: "post", path: "/v1/wikis/{wiki}/records/{id}/close", tags: tag, security, summary: "Close a ledger item and its open descendants",
      request: { params: z.object({ wiki: wikiParam, id: idParam }), body: { content: { "application/json": { schema: CloseBody } }, required: true } },
      responses: { 200: body(z.object({ record: RecordOut, closed: z.array(z.string()) }), "Closed ids, the item first"), 404: err("Not found"), ...errors },
    }),
    async (c) => {
      const { wiki, id } = c.req.valid("param");
      const b = c.req.valid("json");
      return c.json(await unwrap(store(c.env).close(cred(c), wiki, id, b.reason, b.git_ref)), 200);
    },
  );

  app.openapi(
    createRoute({
      method: "delete", path: "/v1/wikis/{wiki}/records/{id}", tags: tag, security, summary: "Tombstone a record (needs the delete scope)",
      request: { params: z.object({ wiki: wikiParam, id: idParam }) },
      responses: { 200: body(RecordOut, "The tombstone"), 404: err("Not found"), ...errors },
    }),
    async (c) => {
      const { wiki, id } = c.req.valid("param");
      return c.json(await unwrap(store(c.env).remove(cred(c), wiki, id)), 200);
    },
  );

  app.openapi(
    createRoute({
      method: "post", path: "/v1/wikis/{wiki}/sync", tags: tag, security,
      summary: "Two-way sync by revision (one transaction; replaying a request changes nothing)",
      request: { params: z.object({ wiki: wikiParam }), body: { content: { "application/json": { schema: SyncRequest } }, required: true } },
      responses: { 200: body(SyncResponse, "Results per op, changes since the cursor, counters"), 404: err("No such wiki"), 413: err("Too many ops or body too large"), ...errors },
    }),
    async (c) => c.json(await unwrap(store(c.env).sync(cred(c), c.req.valid("param").wiki, c.req.valid("json"))), 200),
  );
}
