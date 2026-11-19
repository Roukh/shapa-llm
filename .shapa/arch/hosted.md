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

## Parts

The contract is split by box; a section named in code (`hosted.md, "Reach"`) is found here:

| Section | File |
|---|---|
| Owns, Adopted, Wikis, Auth, Reach, Not hosted: the code map, Invariants | this file |
| Record (wire), Sync, Client commands | [[hosted-sync]] |
| Focus, skills and the project document ("Focus and skills") | [[hosted-documents]] |
| Tags, stages and relations; The learning loop: what syncs | [[hosted-tags]] |
| REST, MCP, Session instructions | [[hosted-api]] |

## Owns

- `hosted/` - the Worker (TypeScript). This box: `src/index.ts` (OAuth provider wiring), `oauth.ts`, `consent.ts`, `auth.ts`, `devices.ts`, `reach.ts` (what a baseline device may write), `store.ts` (Durable Object: schema, records), `schema.ts`, `redact.ts`, `errors.ts`, `kit.ts`; `test/reach.test.ts`, `test/scale.test.ts`. The other source files belong to the parts above.
- Engine side: `shapa/remote.py` and the sync modules ([[hosted-sync]]); `shapa/db.py` additions: `docs`, `tombstones`, explicit-id upserts, relabel.

## Adopted, not written

`@cloudflare/workers-oauth-provider` (OAuth 2.1, dynamic client registration, PKCE), `agents` + `@modelcontextprotocol/server` (stateless streamable-HTTP MCP), `hono` + zod OpenAPI, Durable Object SQLite with FTS5 (one object, name `main`: the single writer, so sync needs no merge engine), KV (`OAUTH_KV`, OAuth state only).

## Wikis

Key `global` for the global wiki (it holds memories, rules, issues, documents, notes and standalone `human_task` / `agent_task` tasks, never a feature or job); a repo wiki's key is the lowercase slug (`^[a-z0-9][a-z0-9._-]{0,63}$`) of its primary checkout's directory name, overridable with `--key`. `PUT /v1/wikis/{key}` `{name, kind, origin}` is idempotent; `origin` is `host/owner/repo` (never credentials). Same key with a different origin: 409. A repo with no git origin has no automatic key: it syncs after one `--key KEY`, which the sync state keeps.

## Auth

| Credential | Use |
|---|---|
| Owner key | secret `SHAPA_OWNER_KEY` (>=32 chars, else admin and consent answer 503). `Authorization: Bearer` on `/admin/*`; typed into the OAuth consent page. 5 failures per 15 min per IP, then 429 |
| Device token | `shp_<device_id>_<secret>`; minted by `POST /admin/devices`, shown once, stored as SHA-256 |
| OAuth 2.1 | chat clients: `/authorize` (consent: owner key, device name, scopes, optional wiki list), `/token`, `/register`. Each grant is one device; the token's resource is `<origin>/mcp` and it is also accepted on `/v1` |

- Scopes: `read`, `write` (needs `read`), `delete` (needs both). A device may be limited to a list of wiki keys (default all).
- Every request resolves to a device row in the store; a revoked device is 401 on its next request, token or OAuth.
- Reach: `full` or `baseline` (see "Reach"). An OAuth grant is always `baseline`, so a chat app or phone reads stage 0 and writes notes, tasks, ideas, decisions and files, and never syncs.
- Admin: `POST /admin/devices` `{name, scopes, wikis?, reach?}` -> `{id, token, reach}` (reach default `full`); `GET /admin/devices` (shows `reach`); `DELETE /admin/devices/{id}`. `shapa remote devices --mint NAME --reach baseline --wikis global --env-file PATH --env-var VAR` is the client of the first: the token goes to a 0600 file (a symlink or a git-tracked file is refused before anything is minted) and is never printed.
- Unauthenticated: `GET /` (a plain-text card, under 900 characters, the same for every caller and reading nothing from the store: what this is, `<origin>/mcp`, `<origin>/v1`, `<origin>/openapi.json`, and the one instruction to call `session_start` / `GET /v1/instructions` and follow it over the agent's own defaults), `GET /health`, `GET /openapi.json`. No token ever travels in a URL.

## Reach

Every device has a reach, stored on its row (`devices.reach`, additive): `full` or `baseline`. An OAuth grant is always `baseline` (a row stored before reach existed is `baseline` when its source is oauth, else `full`). A device token is `full` unless minted `POST /admin/devices {..., "reach": "baseline"}` (for an agent that speaks REST with a Bearer token). `GET /v1/whoami` and the admin list show it. The local engine's machine holds a `full` token and is unaffected.

- Read: the filter is one place. `Repo.view(reach)` is what every operation is handed (`ShapaStore.act` is the only way in), and it applies `base_vis` / `vis` to every record read, list, count, tag, project, task, related and search. A baseline device sees a rule, memory, issue, skill version or note only when it carries `stage:0`, plus ideas and decisions, documents, tasks, features and jobs and the focus tree; anything else answers exactly as if it did not exist (404, absent from lists and counts). The `stage` threshold is fixed at 0 for it. `test/reach.test.ts` walks every GET route of `/openapi.json` and every read MCP tool as an OAuth client and as a baseline token, and fails when a route or tool is not in its table.
- Write (with the write scope): ideas and decisions, notes, documents, tasks (`human_task`, `agent_task`, `task`), project files and items, task claim, release and close, and edits of the notes, documents, tasks, ideas and decisions it can read. A write of a rule, memory (other than an idea or decision), issue or skill version is 403 at every stage, proposals included ("this device writes notes, tasks, ideas, decisions and files"); no stage tag, no feature or job, no `rank:N` set or dropped.
- Never: `POST .../sync` (403), the focus (set, promote, or PATCH of the focus record), a hidden or stage-0 record, any delete, a `skill` or `skill:NAME` tag (403 on any write; no lesson is ever filed from a baseline device's writes, on any path, an identical-text save included: it could otherwise file versions of a skill it cannot read, and the id and revision jump would say whether the skill exists), a `rank:N` tag.
- On a note `stage:0` is an access marker, not the baseline (the only stage tag a note takes, in any wiki): it is outside `baselineCost`/`baselineSet` and the BASELINE section, and no gate converts it to `stage-proposed:0`. A full device adds or drops it on any note (REST, MCP); a sync put never changes a note's tags (a note file has none: the stored tags stay, and a note sync creates or revives has none), so a client's cached tags never re-share a note; a note a baseline device creates gets it, and it never removes it or sets another stage tag. A device's `base_vis` is rebuilt once for the notes already stored (meta `note_access_v1`). The printed notes are ordinary notes to the filter: a baseline device is shown them only once they carry the marker, and the write guard below holds either way.
- One note per project, for every device on REST and MCP: a second note tagged `project:KEY` in a wiki, at create or by a retag, is a 409 naming the first (`gateProjectNote`); a holder a baseline device cannot read is not named, so the refusal is generic and the cap holds. Sync never refuses (stored extras are grandfathered, as for documents); a note with no project tag is not capped.
- An alias a baseline device asks for that any live record holds is dropped silently (the record is created without it; a rename keeps the alias the record had), never a 409: the holder may be hidden from it, and the alias's existence would leak. A full device still gets the 409 naming the holder.
- Saving text a hidden record already holds creates a new separate record (the baseline device never finds the holder); saving text a stage-0 record holds returns that record unchanged ("Identical text" in [[hosted-tags]]).
- The notes printed into every session text (`session-instructions` and `response-style` in the global wiki, by id, alias, path stem or frontmatter id, any case, with or without `.md`) are written, edited or deleted only by a device TOKEN with the delete scope and full reach (the operator's machine), on REST, MCP and sync, from any other device a 403 (sync `rejected`). Their text is masked (`redact`) and stripped of control, format and line-separator characters when printed; its line breaks stay.
- The write-back block of the session text is chosen by reach: a baseline device is shown only the types it may write, and nothing else of the text names a tool it may not use (no `focus_set`, no focus PUT).
- The stage-0 baseline (rules, memories, active skills) is part of the session text of every authenticated device, including one whose wiki list leaves out `global`; nothing else of the global wiki reaches such a device. The `stage` threshold is fixed at 0 for such a device too (as for a baseline one): only stage 0 is exempt from the wiki list, so `stage=9` never prints a global `stage:1`-`stage:9` rule, memory or skill body to a device that may not read `global`.
- Never, also: registering a wiki (`PUT /v1/wikis/{key}` for a key that does not exist is 403 for a baseline device; an existing one still answers 200); an engine tag on what it writes (any system tag or family of `tags.ts`: the learning loop's `principle`, `churn`, `violations:N`, `serves:` and the rest, `v:`, `status:`, `focus`, `rank:`, `stage:`; only `idea`, `decision`, `human_task`, `agent_task` and one `project:KEY` pass; a tag the record already held stays through an edit; a full device's write, sync included, is unchanged).
- The 1,000-project cap of a wiki counts, for a baseline device, only the projects it can see, so hitting it never tells it about a hidden project.

## Not hosted: the code map

A fire's code partition (`shapa map`, `shapa partition`, MCP `partition` of the local server) is derived from a checkout (`shapa/codemap.py`, the gitignored `.shapa-code.db`), so it is local to the device that has the code. The Worker stores no graph, no partition and no file path of a map, has no `partition` tool or route, and no sync record carries one: a chat client without a checkout has nothing to partition. The fire itself (feature, jobs, the files and arch boxes their text names) still reaches it through `focus` and `instructions`.

## Invariants

- The server never trusts a client for `rev`, `device` or id allocation on REST writes.
- Stage 0 and the baseline text are the operator's alone (sync from a delete-scope token); a baseline-reach device reads stage 0 and nothing above it and writes no staged kind.
- Known and accepted limit of reach: ids and `rev` are shared counters, so a baseline device's own new record id or a record's `rev` lets it estimate how many hidden records exist, never what they hold. Nothing else about a hidden record is observable: not an alias (dropped silently), not a skill name (a `skill:` tag is refused and writes nothing), not a project (membership counts only what it can read), not a text (identical text finds no hidden holder). Two things a baseline device can still learn, by asking about a name it chose: that a note path is taken (409 with one wording, whether the holder is hidden or not) and that some note holds a `project:KEY`; neither names or shows a hidden note.
- Deploy order: the Worker first, then the CLI. The Worker refuses what the new CLI also refuses (one stage marker, lookalikes, `stage:0` outside the global wiki), so an old CLI keeps working against the new Worker, and a new CLI never meets a Worker that accepts what it would drop.
- Reads stay flat as the data grows (`hosted/test/scale.test.ts`, 1,000 then 10,000 records, each read as a full and as a baseline device): the rows the store reads for one call are counted by SQLite and may not grow more than 3x (search 10x), or pass a floor a scan would cross; the call's time (median of batches) is held to the same ratios. Alias lookups carry `alias != ''` so SQLite uses the partial alias index; the session start's focus and note lookups read a few rows however many records exist.
- No secret in the repo: account ids, URLs and keys stay in the deployer's environment and `~/.shapa`.
- Local database schema 2 is additive (`docs`, `tombstones`, counter `D`); a schema-1 reader still works.
- Hooks never block a session and never fail it.
- On a wiki with sync state the after-merge sweep never deletes a memory for disuse: use counts are per device.
