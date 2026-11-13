// OAuth 2.1 through @cloudflare/workers-oauth-provider: the authorization server (discovery,
// dynamic registration, PKCE, token endpoint) and, for the MCP endpoint, the resource server.
// A token issued after the owner approves a client carries a device id; like a device token it is
// resolved to a live device row on every request, so revoking the device refuses it at once.

import { OAuthAuthorizationServer, OAuthResourceServer, type OAuthResourceContext } from "@cloudflare/workers-oauth-provider";
import { resolveDeviceBearer, store } from "./auth.js";
import { SCOPES } from "./records.js";
import type { Cred } from "./store.js";

export interface AuthProps extends Record<string, unknown> {
  cred: Cred;
}

export interface OAuthKit {
  origin: string;
  mcpResource: string;
  server: OAuthAuthorizationServer<Env>;
  mcp: OAuthResourceServer<Env, AuthProps>;
}

const kits = new Map<string, OAuthKit>();

/** The origin this request was served at (the OAuth issuer and resource are built from it). */
export function originOf(request: Request): string {
  return new URL(request.url).origin;
}

/** The OAuth role objects for an origin. The issuer and resource URL are absolute, so they are
 * built on first use (per origin) rather than at module load. */
export function oauthKit(
  origin: string,
  mcpHandler: (request: Request, env: Env, ctx: OAuthResourceContext<AuthProps>) => Promise<Response>,
): OAuthKit {
  const cached = kits.get(origin);
  if (cached) return cached;
  const mcpResource = `${origin}/mcp`;
  const server = new OAuthAuthorizationServer<Env>({
    issuer: origin,
    resources: [mcpResource],
    authorizeEndpoint: "/authorize",
    tokenEndpoint: "/token",
    clientRegistrationEndpoint: "/register",
    scopesSupported: [...SCOPES],
  });
  const mcp = new OAuthResourceServer<Env, AuthProps>({
    resourceMetadata: { resource: mcpResource, authorization_servers: [origin], resource_name: "shapa" },
    validateToken: (env) => async (resource, token) => {
      const cred = await resolveBearer(server, env, resource, token);
      return cred ? { props: { cred }, audience: resource } : null;
    },
    handler: { fetch: (request, env, ctx) => mcpHandler(request, env, ctx) },
  });
  if (kits.size >= 8) kits.clear();
  const kit = { origin, mcpResource, server, mcp };
  kits.set(origin, kit);
  return kit;
}

/** A bearer token to a store credential: a device token, or an OAuth access token for the MCP
 * resource. Returns null unless the device behind it is live (so revocation takes effect now). */
export async function resolveBearer(
  server: OAuthAuthorizationServer<Env>, env: Env, resource: string, token: string,
): Promise<Cred | null> {
  if (token.startsWith("shp_")) return resolveDeviceBearer(env, token);
  const t = await server.validateToken<{ deviceId?: string }>(resource, token, env);
  const id = t?.props?.deviceId;
  if (typeof id !== "string") return null;
  const cred: Cred = { device: id };
  const who = await store(env).whoami(cred);
  return who.ok ? cred : null;
}

