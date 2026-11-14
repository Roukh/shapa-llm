# shapa hosted

The server half of hosted shapa: one Cloudflare Worker that is the central store for every wiki.
The contract (record model, sync protocol, REST, MCP, auth) is `.shapa/arch/hosted.md`.

- **Store**: one SQLite-backed Durable Object (`ShapaStore`, name `main`) holds wikis, records
  (with tombstones), per-wiki id counters, a global `rev`, devices and the FTS5 index. One writer,
  so sync needs no merge engine.
- **REST** (`/v1`, Hono + zod-openapi) and **remote MCP** (`/mcp`, `agents` + the MCP SDK) call the
  same store methods. `GET /openapi.json` is generated from the zod schemas in `src/records.ts`.
- **Auth**: per-device tokens `shp_<device_id>_<secret>` (SHA-256 at rest) and OAuth 2.1 through
  `@cloudflare/workers-oauth-provider` (dynamic registration, PKCE, a consent page that asks for the
  owner key). Every request resolves to a live device row; revoking a device refuses both kinds.

## Layout

| File | Role |
|---|---|
| `src/index.ts` | Entry: dispatch to MCP, consent, OAuth server, or the Hono app |
| `src/app.ts`, `src/api.ts` | `/health`, `/openapi.json`, `/admin`, `/v1` routes |
| `src/store.ts` | The Durable Object; every method is one operation |
| `src/repo.ts`, `src/schema.ts` | SQL access and schema |
| `src/records.ts` | Wire schemas, limits, normalization (single source of the types) |
| `src/ledger.ts`, `src/sync.ts`, `src/search.ts`, `src/wikis.ts` | Operations |
| `src/instructions.ts` | Session instructions composer (northstar, fires and their tree; `full` adds the rest) |
| `src/focus.ts`, `src/skills.ts`, `src/projects.ts` | The northstar and three fires, versioned skills, and the one-document-per-project gate (all documents told apart by tags; no schema change) |
| `src/mcp.ts` | MCP tools |
| `src/auth.ts`, `src/consent.ts`, `src/devices.ts`, `src/kit.ts` | Credentials, consent page, device rows, OAuth wiring |
| `src/redact.ts` | Port of `shapa/redact.py` |

## Develop

```sh
cd hosted
npm ci
npm run types        # wrangler types -> worker-configuration.d.ts (generated, not committed)
npm run typecheck
npm test             # Vitest inside workerd; needs no running server and no secrets
npm run deploy:dry   # builds the bundle, deploys nothing
```

Run locally (the owner key is a value you choose, 32 or more characters; nothing reads a
`.dev.vars` you did not write yourself):

```sh
npx wrangler dev --var SHAPA_OWNER_KEY:<a-long-random-value>
curl -s -X POST localhost:8787/admin/devices -H "authorization: Bearer <that value>" \
  -H 'content-type: application/json' -d '{"name":"laptop","scopes":["read","write"]}'
```

## Deploy (a human does this once)

1. `wrangler login` (or set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`). `wrangler.jsonc`
   carries no account id.
2. `npx wrangler secret put SHAPA_OWNER_KEY` with a random value of 32 or more characters
   (`openssl rand -base64 48`). Without it, `/admin` and the consent page answer 503.
3. `npx wrangler deploy`. The Durable Object migration (`v1`) runs, and the `OAUTH_KV` namespace is
   created and bound automatically because the config gives no `id`.
4. Optional but recommended: put a Cloudflare rate-limiting rule on `/register`, `/token` and
   `/authorize`, and attach a custom domain. The Worker derives its OAuth issuer from the hostname
   a request arrives on.

## Operate

- Mint a token: `POST /admin/devices {name, scopes, wikis?}` with `Authorization: Bearer <owner key>`.
  The token is shown once.
- List or revoke: `GET /admin/devices`, `DELETE /admin/devices/{id}`.
- A chat client adds `https://<host>/mcp` as a remote MCP server and signs in on the consent page
  with the owner key.
- The owner key allows 5 wrong attempts per IP per 15 minutes, then 429. The IP is
  `CF-Connecting-IP`, which Cloudflare sets; behind `wrangler dev` a client can choose it.

## Limits

One record up to 1 MB of JSON; 200 ops per sync request; a `changes` page of 500 (plus the
records of rejected ops and `want` ids); a sync body up to 8 MiB, other JSON bodies up to 1.1 MB.
