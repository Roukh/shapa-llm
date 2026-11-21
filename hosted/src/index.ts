// Entry point. Dispatch by path:
//   /mcp and its protected-resource metadata (also at the root path) -> OAuth resource server (device token or OAuth token) -> MCP
//   /authorize                               -> consent page (owner key)
//   /.well-known/*, /token, /register        -> OAuth authorization server
//   everything else                          -> Hono app (/health, /openapi.json, /admin, /v1)

import { createApp } from "./app.js";
import { authorize } from "./consent.js";
import { getKit } from "./kit.js";

export { ShapaStore } from "./store.js";

const app = createApp();
const PRM_ROOT_PATH = "/.well-known/oauth-protected-resource";
const PRM_PATH = `${PRM_ROOT_PATH}/mcp`;

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const { pathname } = new URL(request.url);
    const kit = getKit(request);
    if (pathname === "/mcp" || pathname.startsWith("/mcp/") || pathname === PRM_PATH) {
      return kit.mcp.fetch(request, env, ctx);
    }
    // Some clients probe the root metadata path first: serve the same document as the /mcp one.
    if (pathname === PRM_ROOT_PATH && (request.method === "GET" || request.method === "HEAD")) {
      const url = new URL(request.url);
      url.pathname = PRM_PATH;
      return kit.mcp.fetch(new Request(url, request), env, ctx);
    }
    if (pathname === "/authorize") return authorize(request, env, kit);
    if (pathname.startsWith("/.well-known/") || pathname === "/token" || pathname === "/register") {
      return kit.server.fetch(request, env, ctx);
    }
    return app.fetch(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
