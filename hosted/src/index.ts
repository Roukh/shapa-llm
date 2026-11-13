// Entry point. Dispatch by path:
//   /authorize                               -> consent page (owner key)
//   /.well-known/*, /token, /register        -> OAuth authorization server
//   everything else                          -> Hono app (/health, /openapi.json, /admin, /v1)

import { createApp } from "./app.js";
import { authorize } from "./consent.js";
import { getKit } from "./kit.js";

export { ShapaStore } from "./store.js";

const app = createApp();

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const { pathname } = new URL(request.url);
    const kit = getKit(request);
    if (pathname === "/authorize") return authorize(request, env, kit);
    if (pathname.startsWith("/.well-known/") || pathname === "/token" || pathname === "/register") {
      return kit.server.fetch(request, env, ctx);
    }
    return app.fetch(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
