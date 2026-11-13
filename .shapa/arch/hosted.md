---
id: hosted
type: reference
created: "2026-11-12T00:00:00Z"
consequence: 9
locus: output-meta
scope: repo
summary: Hosted shapa contract - a Cloudflare Worker (Durable Object SQLite), one REST API, remote MCP, per-device auth; every wiki syncs both ways by revision.
---

# Hosted

One store every client reads and writes: coding harnesses through `shapa remote sync`
(hooks), clients without hooks (chat apps, phones) through remote MCP or REST.
Opt-in: the engine works unchanged with no remote configured.

## Owns

- `hosted/` - the Worker (TypeScript): `src/index.ts` (OAuth provider wiring), `store.ts` (Durable Object: schema, records, sync, search), `records.ts` (zod wire schemas, the single source of the wire types and of `/openapi.json`), `api.ts` (REST), `mcp.ts`, `auth.ts`, `instructions.ts`, `redact.ts`.
- `shapa/remote.py` - `shapa remote` CLI, credentials, HTTP (stdlib `urllib`). `shapa/sync.py` - snapshot, classify, apply.
- `shapa/db.py` additions: `docs`, `tombstones`, explicit-id upserts, relabel.

## Adopted, not written

`@cloudflare/workers-oauth-provider` (OAuth 2.1, dynamic client registration, PKCE), `agents` + `@modelcontextprotocol/sdk` (streamable-HTTP MCP), `hono` + zod OpenAPI, Durable Object SQLite with FTS5 (one object, name `main`: the single writer, so sync needs no merge engine), KV (`OAUTH_KV`, OAuth state only).

## Record (wire)

`{id, kind, created, updated, rev, deleted, device, data, tags, links}`

| Field | Rule |
|---|---|
| `id` | label unique per wiki: kind letter + counter (`M12`); a note's id is its path |
| `kind` | `F J T` ledger, `M R I` rows, `D` document, `N` note file |
| `created` | `YYYY-MM-DDTHH:MM:SSZ`, immutable; `""` for notes. Identity = `id` + `created` |
| `updated` | same format; compared as strings for last-write-wins |
| `rev` | server sequence number of the record's last change (server-set) |
| `deleted` | tombstone flag; a tombstone keeps `id`, `kind`, `created`, `rev` |
| `device` | name of the device that last wrote (server-set) |
| `tags` | sorted unique strings (<=80 chars each) |
| `links` | sorted `[dst, rel]`, `rel` in `about supersedes conflicts related`; the record is `src` |

| Kind | `data` |
|---|---|
| F J T | `title, body, parent, verify, status (open/claimed/closed), closed_reason, git_ref, alias, claimed_by, closed` |
| M R I | `summary` (one line, <=160), `body, alias, source, session` |
| D | `title` (one line, <=200), `body, alias, mime` (default `text/markdown`), `source` |
| N | `body` (the whole file). Server derives `alias` (frontmatter `id`, else stem) and `summary` |

- Type names: feature, job, task, memory, rule, issue, document, note. `idea` = a memory tagged `idea` (no kind of its own).
- Note ids match `^(arch/|research/)?[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$`; root `AGENTS.md` and `placement.md` never sync. Both sides enforce this before any file write.
- Limits: one record <= 1 MB JSON; <= 200 ops per sync request; a `changes` page <= 500 records.
- Text fields pass secret redaction on the server at write and on the client before a local write.

## Wikis

Key `global` for the global wiki; a repo wiki's key is the lowercase slug (`^[a-z0-9][a-z0-9._-]{0,63}$`) of its primary checkout's directory name, overridable with `--key`. `PUT /v1/wikis/{key}` `{name, kind, origin}` is idempotent; `origin` is `host/owner/repo` (never credentials). Same key with a different non-empty origin: 409.

## Auth

| Credential | Use |
|---|---|
| Owner key | secret `SHAPA_OWNER_KEY` (>=32 chars, else admin and consent answer 503). `Authorization: Bearer` on `/admin/*`; typed into the OAuth consent page. 5 failures per 15 min per IP, then 429 |
| Device token | `shp_<device_id>_<secret>`; minted by `POST /admin/devices`, shown once, stored as SHA-256 |
| OAuth 2.1 | chat clients: `/authorize` (consent: owner key, device name, scopes), `/token`, `/register`. Each grant is one device |

- Scopes: `read`, `write`, `delete`. A device may be limited to a list of wiki keys (default all).
- Every request resolves to a device row in the store; a revoked device is 401 on its next request, token or OAuth.
- Admin: `POST /admin/devices` `{name, scopes, wikis?}` -> `{id, token}`; `GET /admin/devices`; `DELETE /admin/devices/{id}`.
- Unauthenticated: `GET /health`, `GET /openapi.json`.

## REST (`/v1`, JSON, Bearer)

| Route | Scope | Does |
|---|---|---|
| `GET /whoami` | read | the calling device |
| `GET /wikis`, `PUT /wikis/{wiki}` | read, write | list, register |
| `GET /instructions?client=mcp\|rest&wiki=&budget=` | read | session defaults: `{text, version}` (`Accept: text/plain` for the text alone) |
| `GET /search?q=&k=&wiki=&types=` | read | FTS5 BM25 over live records: `{results: [{wiki, id, type, summary, updated}], mode}` |
| `GET /get?id=&wiki=` | read | by id or alias across wikis: `{matches: [record + wiki]}` |
| `GET /wikis/{wiki}/records?kind=&status=&tag=&after=&limit=` | read | list |
| `GET /wikis/{wiki}/records/{id}` | read | one record (id or alias, URL-encoded) |
| `POST /wikis/{wiki}/records` | write | create from `{type, summary\|title, body, tags?, alias?, parent?, verify?, path?, mime?}`; server allocates the id; identical row text returns the existing row |
| `PATCH /wikis/{wiki}/records/{id}` | write | edit fields; optional `base_rev`, mismatch is 409 with `current` |
| `POST /wikis/{wiki}/records/{id}/claim\|release\|close` | write | ledger moves; `close` takes `{reason: agent\|operator, git_ref?}` and closes open descendants as `parent` |
| `DELETE /wikis/{wiki}/records/{id}` | delete | tombstone |
| `POST /wikis/{wiki}/sync` | write | below |

Ledger rules mirror `db.py`: a job's parent is a feature, a task's a job; no child under a closed parent; `claimed_by` is the device name. Errors: `{"error": {"code", "message"}}` with 400 `invalid`, 401, 403 `forbidden`, 404, 409 `conflict`, 413, 429.

## Sync

Request `{cursor, ops, want, counters}`; response `{cursor, results, changes, counters, more}`.

| Op | Shape |
|---|---|
| put | `{op: "put", base_rev: int\|null, record}` (`null` = new to the server as far as the client knows) |
| del | `{op: "del", id, created, deleted_at, base_rev: int\|null}` (needs scope `delete`) |

The server applies `ops` in order inside one transaction:

| Case | Result |
|---|---|
| put, id unknown | insert: `accepted` |
| put, id held by a record with another `created` (tombstones count) | stored under a fresh id: `renamed` + `new_id`; later ops in the request that name the old id as `parent` or link `dst` are rewritten |
| put, same identity, `base_rev` equals current `rev` | replace: `accepted` |
| put, same identity, content equal | `noop` |
| put, same identity, otherwise | incoming `updated` strictly newer wins (`accepted`), else `rejected` |
| del, record missing, already deleted, or other `created` | `noop` |
| del, `base_rev` equals `rev`, or record `updated` <= `deleted_at` | tombstone: `accepted` |
| del, otherwise | `rejected` (a later edit beats a delete) |

- `results[i]` is `{id, status, rev, new_id?}` for `ops[i]`. Every accepted change takes the next `rev`.
- `changes`: every record with `rev` > request `cursor`, oldest first, plus the current record for each rejected op and each `want` id. `more: true` means call again with the returned cursor and no ops.
- `counters`: next number per kind; each side keeps the maximum, which makes label collisions rare.

Client (`shapa remote sync`), per wiki, under a file lock:

1. Snapshot local records. State (`sync/<sha1 of db path>.json` beside the credentials) holds `{key, cursor, base: {id: {rev, created, updated, hash}}}`; `hash` is local-only.
2. Classify against `base`: absent from base = new (put, `base_rev` null); hash differs = modified (put, `base_rev`), unless local `updated` is older than base's, then stale (`want`); in base but gone locally = deleted only when `tombstones` holds it (del), else missing (`want`). A database swapped by git therefore heals from the server instead of pushing deletes.
3. Send (parents first: F, J, T, then the rest), in batches.
4. In one local write transaction: apply `renamed` (relabel id, child `parent`, tags, links; foreign keys deferred), then each change, skipping any record whose local content changed since step 1; raise counters; drop acknowledged tombstones; write state.

A remote delete removes a local note file only if it is unchanged since the last sync. Remote text is never executed and never chooses a path outside the note pattern.

## MCP (`/mcp`, streamable HTTP)

Server `instructions`: call `session_start` first. No delete tool.

| Tool | Args | Does |
|---|---|---|
| `session_start` | `client?, wiki?` | the session defaults text |
| `search` | `query, k?, wiki?, types?` | as REST search |
| `get` | `id, wiki?` | full record; lists every match when the id is in several wikis |
| `save` | `wiki, type (memory/idea/rule/issue/document/note), summary\|title, body, tags?, alias?, path?` | create |
| `update` | `wiki, id, summary?, title?, body?, tags?, base_rev?` | edit |
| `ledger_list` | `wiki?` | live features with jobs and tasks |
| `ledger_add` | `wiki, kind, title, parent?, body?, verify?` | add |
| `ledger_set` | `wiki, id, action (claim/release/close/edit), fields?` | move or edit |
| `wikis` | - | keys, names, counts |

## Session instructions

For clients with no hooks. Composed per request, within `budget` characters (default 6000): what shapa is; the global wiki's `session-instructions` note if present; the operator style (global note `response-style`); global rules (id + summary); live features per wiki; then the write-back format for the client kind - when to write each type, and the exact `save` call (`mcp`) or `POST` body (`rest`). A client with no auto-inject is pointed at it by one line in its custom instructions.

## Client commands

| Command | Does |
|---|---|
| `shapa remote login URL [--device NAME] [--scopes ...] [--token-stdin]` | owner key from `$SHAPA_OWNER_KEY` or a prompt, never argv; writes `remote.json` (`url, device, device_id`) and `remote.token` (0600) under `~/.shapa` (`$SHAPA_REMOTE_DIR` overrides) |
| `shapa remote sync [--cwd DIR] [--all] [--key KEY] [--hook] [--if-changed] [--dry-run] [--json]` | wikis in scope (repo + global), or every registered one; `--hook` reads hook stdin, stays within 8 s, always exits 0, prints at most one line; `--if-changed` skips the network when nothing local changed |
| `shapa remote status [--check] [--json]` | login, last sync and pending changes per wiki; `--check` exits 1 when not logged in or unreachable |
| `shapa remote devices [--revoke ID]`, `logout`, `instructions [--client NAME]` | device admin (owner key), forget credentials, print the session defaults |
| `shapa doc add\|edit\|rm\|list` | documents; `shapa get D<n>`; MCP `save` gains `document` and `idea` |

## Invariants

- The server never trusts a client for `rev`, `device` or id allocation on REST writes.
- No secret in the repo: account ids, URLs and keys stay in the deployer's environment and `~/.shapa`.
- Local database schema 2 is additive (`docs`, `tombstones`, counter `D`); a schema-1 reader still works.
- Hooks never block a session and never fail it.
