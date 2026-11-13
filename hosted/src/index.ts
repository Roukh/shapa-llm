// Entry point: the Hono app (/health, /openapi.json, /admin, /v1).

import { createApp } from "./app.js";

export { ShapaStore } from "./store.js";

const app = createApp();

export default {
  fetch: (request: Request, env: Env, ctx: ExecutionContext): Promise<Response> | Response => app.fetch(request, env, ctx),
} satisfies ExportedHandler<Env>;
