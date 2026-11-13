// Wires the OAuth provider objects to the MCP handler for the origin a request arrived on.

import { oauthKit, originOf, type OAuthKit } from "./oauth.js";
import { serveMcp } from "./mcp.js";

export function getKit(request: Request): OAuthKit {
  return oauthKit(originOf(request), (req, env, ctx) => serveMcp(req, env, ctx, ctx.props.cred));
}
