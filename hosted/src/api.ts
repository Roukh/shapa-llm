// REST (/v1): one route per contract row, declared with zod so validation, types and the
// generated /openapi.json come from the same schemas in records.ts. Every handler is a thin call
// into a store method; scope and wiki-allowlist checks happen in the store.

import { createRoute, z, type OpenAPIHono } from "@hono/zod-openapi";
import { store } from "./auth.js";
import { unwrap } from "./errors.js";
import {
  CloseBody, CreateBody, ErrorBody, KINDS, LIMITS, PatchBody, RecordOut, RecordWithWiki, SearchOut, SyncRequest,
  SyncResponse, WIKI_KEY_RE, WikiOut, WikiPut, type Kind,
} from "./records.js";
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
        200: body(z.object({ id: z.string(), name: z.string(), scopes: z.array(z.string()), wikis: z.array(z.string()).nullable() }), "The device"),
        ...errors,
      },
    }),
    async (c) => {
      const d = await unwrap(store(c.env).whoami(cred(c)));
      return c.json({ id: d.id, name: d.name, scopes: d.scopes, wikis: d.wikis }, 200);
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
      const r = await unwrap(store(c.env).instructions(cred(c), { client: q.client, ...(q.wiki ? { wiki: q.wiki } : {}), budget: q.budget }));
      if ((c.req.header("accept") ?? "").includes("text/plain")) return c.text(r.text, 200) as never;
      return c.json(r, 200);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/v1/search", tags: tag, security, summary: "BM25 search over live records",
      request: {
        query: z.object({
          q: z.string().min(1).max(500),
          k: intQuery(1, LIMITS.searchK, 8),
          wiki: z.string().regex(WIKI_KEY_RE).optional(),
          types: z.string().max(200).optional().openapi({ description: "Comma-separated type names." }),
        }),
      },
      responses: { 200: body(SearchOut, "Hits"), ...errors },
    }),
    async (c) => {
      const q = c.req.valid("query");
      const types = q.types ? q.types.split(",").map((t) => t.trim()).filter(Boolean) : undefined;
      const r = await unwrap(store(c.env).search(cred(c), { query: q.q, k: q.k, ...(q.wiki ? { wiki: q.wiki } : {}), ...(types ? { types } : {}) }));
      return c.json(r, 200);
    },
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
      return c.json(await unwrap(store(c.env).patch(cred(c), wiki, id, c.req.valid("json"))), 200);
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
