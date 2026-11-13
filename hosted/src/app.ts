// The Hono application: health and OpenAPI (public), /admin (owner key), /v1 (bearer).
// OAuth and MCP routes are dispatched in index.ts before this app sees the request.

import { OpenAPIHono, createRoute, z } from "@hono/zod-openapi";
import { bodyLimit } from "hono/body-limit";
import { createMiddleware } from "hono/factory";
import { HTTPException } from "hono/http-exception";
import { registerV1, type App, type AppEnv } from "./api.js";
import { bearerOf, requireOwnerKey, resolveDeviceBearer, randomToken, sha256Hex, store, unauthorized } from "./auth.js";
import { ApiError, unwrap } from "./errors.js";
import { DeviceOut, ErrorBody, LIMITS, SCOPES, WIKI_KEY_RE } from "./records.js";

function tooBig(c: { json: (b: unknown, s: 413) => Response }): Response {
  return c.json({ error: { code: "too_large", message: "request body is too large" } }, 413);
}

const v1Auth = createMiddleware<AppEnv>(async (c, next) => {
  const token = bearerOf(c.req.raw);
  if (!token) return unauthorized();
  const cred = await resolveDeviceBearer(c.env, token);
  if (!cred) return unauthorized();
  c.set("cred", cred);
  await next();
});

const ownerAuth = createMiddleware<AppEnv>(async (c, next) => {
  await requireOwnerKey(c.env, c.req.raw, bearerOf(c.req.raw));
  await next();
});

const DeviceCreate = z.object({
  name: z.string().min(1).max(64),
  scopes: z.array(z.enum(SCOPES)).min(1).max(3),
  wikis: z.array(z.string().regex(WIKI_KEY_RE)).max(100).nullable().default(null),
}).openapi("DeviceCreate");

function registerAdmin(app: App): void {
  app.openAPIRegistry.registerComponent("securitySchemes", "ownerKey", { type: "http", scheme: "bearer", description: "The owner key (SHAPA_OWNER_KEY)." });
  const tags = ["admin"];
  const security = [{ ownerKey: [] }];
  const j = <T extends z.ZodType>(schema: T, description: string) => ({ content: { "application/json": { schema } }, description });
  const errs = { 400: j(ErrorBody, "Invalid"), 401: j(ErrorBody, "Wrong owner key"), 429: j(ErrorBody, "Too many failed attempts"), 503: j(ErrorBody, "Owner key not configured") };

  app.openapi(
    createRoute({
      method: "post", path: "/admin/devices", tags, security, summary: "Mint a device token (shown once)",
      request: { body: { content: { "application/json": { schema: DeviceCreate } }, required: true } },
      responses: { 201: j(z.object({ id: z.string(), token: z.string(), name: z.string(), scopes: z.array(z.string()), wikis: z.array(z.string()).nullable() }), "The device and its token"), ...errs },
    }),
    async (c) => {
      const b = c.req.valid("json");
      const secret = randomToken();
      const d = await unwrap(store(c.env).createDevice({ name: b.name, scopes: b.scopes, wikis: b.wikis, source: "token", tokenHash: await sha256Hex(secret) }));
      return c.json({ id: d.id, token: `shp_${d.id}_${secret}`, name: d.name, scopes: d.scopes, wikis: d.wikis }, 201);
    },
  );

  app.openapi(
    createRoute({
      method: "get", path: "/admin/devices", tags, security, summary: "List devices",
      responses: { 200: j(z.object({ devices: z.array(DeviceOut) }), "Devices"), ...errs },
    }),
    async (c) => c.json({ devices: await unwrap(store(c.env).listDevices()) }, 200),
  );

  app.openapi(
    createRoute({
      method: "delete", path: "/admin/devices/{id}", tags, security, summary: "Revoke a device (its token stops on the next request)",
      request: { params: z.object({ id: z.string().regex(/^[0-9a-f]{16}$/).openapi({ param: { name: "id", in: "path" } }) }) },
      responses: { 200: j(z.object({ device: DeviceOut }), "The revoked device"), 404: j(ErrorBody, "Unknown device"), ...errs },
    }),
    async (c) => {
      const { id } = c.req.valid("param");
      const device = await unwrap(store(c.env).revokeDevice(id));
      return c.json({ device }, 200);
    },
  );
}

export function createApp(): App {
  const app = new OpenAPIHono<AppEnv>({
    defaultHook: (result, c) => {
      if (result.success) return;
      const issue = result.error.issues[0];
      const where = issue?.path.join(".") || "request";
      const sizeIssue = issue?.code === "too_big" && ["ops", "want", "body"].includes(String(issue.path[0]));
      return c.json(
        { error: { code: sizeIssue ? "too_large" : "invalid", message: `${where}: ${issue?.message ?? "is invalid"}` } },
        sizeIssue ? 413 : 400,
      );
    },
  });

  app.onError((e, c) => {
    if (e instanceof ApiError) {
      const headers = e.headers ?? {};
      for (const [k, v] of Object.entries(headers)) c.header(k, v);
      if (e.status === 401) c.header("WWW-Authenticate", 'Bearer realm="shapa"');
      return c.json({ error: { code: e.code, message: e.message, ...e.extra } }, e.status as 400);
    }
    if (e instanceof HTTPException) {
      const code = e.status === 413 ? "too_large" : "invalid";
      return c.json({ error: { code, message: e.status === 400 ? "malformed request body" : e.message } }, e.status as 400);
    }
    console.error("unhandled", e instanceof Error ? e.message : "error");
    return c.json({ error: { code: "internal", message: "internal error" } }, 500);
  });
  app.notFound((c) => c.json({ error: { code: "not_found", message: "no such route" } }, 404));

  // Everything under /v1 and /admin is private JSON or text: never cached, never sniffed as HTML.
  const hardenHeaders = createMiddleware<AppEnv>(async (c, next) => {
    await next();
    c.res.headers.set("Cache-Control", "no-store");
    c.res.headers.set("X-Content-Type-Options", "nosniff");
  });
  app.use("/v1/*", hardenHeaders);
  app.use("/admin/*", hardenHeaders);
  const limit = (maxSize: number) => bodyLimit({ maxSize, onError: tooBig });
  app.use("/v1/*", v1Auth);
  app.use("/v1/*", async (c, next) => (c.req.path.endsWith("/sync") ? limit(LIMITS.syncBodyBytes) : limit(LIMITS.jsonBodyBytes))(c, next));
  app.use("/admin/*", ownerAuth);
  app.use("/admin/*", limit(4096));

  registerV1(app);
  registerAdmin(app);

  app.get("/health", (c) => c.json({ ok: true }));
  app.doc("/openapi.json", {
    openapi: "3.1.0",
    info: { title: "shapa hosted", version: "0.1.0", description: "One store for every shapa wiki: records, ledger, sync, search. The MCP server is at /mcp." },
  });
  return app;
}
