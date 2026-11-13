// Entry point. Dispatch by path:
//   /mcp and its protected-resource metadata -> OAuth resource server (device token or OAuth token) -> MCP
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
    if (pathname === "/mcp" || pathname.startsWith("/mcp/") || pathname === "/.well-known/oauth-protected-resource/mcp") {
      return kit.mcp.fetch(request, env, ctx);
    }
    if (pathname === "/authorize") return authorize(request, env, kit);
    if (pathname.startsWith("/.well-known/") || pathname === "/token" || pathname === "/register") {
      return kit.server.fetch(request, env, ctx);
    }
    return app.fetch(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
