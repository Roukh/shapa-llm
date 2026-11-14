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

- `hosted/` - the Worker (TypeScript): `src/index.ts` (OAuth provider wiring), `store.ts` (Durable Object: schema, records, sync, search), `records.ts` (zod wire schemas, the single source of the wire types and of `/openapi.json`), `api.ts` (REST), `mcp.ts`, `auth.ts`, `instructions.ts`, `redact.ts`, `focus.ts` (northstar, fires, promotion), `skills.ts` (versions, lessons), `projects.ts` (the lean gate).
- `shapa/remote.py` - `shapa remote` CLI, credentials, HTTP (stdlib `urllib`). `shapa/sync.py` - snapshot, classify, apply.
- `shapa/db.py` additions: `docs`, `tombstones`, explicit-id upserts, relabel.

## Adopted, not written

`@cloudflare/workers-oauth-provider` (OAuth 2.1, dynamic client registration, PKCE), `agents` + `@modelcontextprotocol/server` (stateless streamable-HTTP MCP), `hono` + zod OpenAPI, Durable Object SQLite with FTS5 (one object, name `main`: the single writer, so sync needs no merge engine), KV (`OAUTH_KV`, OAuth state only).

## Record (wire)

`{id, uid, kind, created, updated, rev, deleted, device, data, tags, links}`

| Field | Rule |
|---|---|
| `id` | label unique per wiki: kind letter + counter (`M12`); a note's id is its path |
| `uid` | the record's identity: 16-64 chars of `[A-Za-z0-9_-]`, random, set where the record is created, never changed; `""` for notes (identity = path). A row from before schema 2 gets the first 16 hex of SHA-256 over `id + "\n" + created`, so every copy of that database derives the same one |
| `kind` | `F J T` ledger, `M R I` rows, `D` document, `N` note file |
| `created` | `YYYY-MM-DDTHH:MM:SSZ`, immutable; `""` for notes |
| `updated` | same format; compared as strings for last-write-wins |
| `rev` | server sequence number of the record's last change (server-set) |
| `deleted` | tombstone flag; a tombstone keeps `id`, `uid`, `kind`, `created`, `rev` |
| `device` | name of the device that last wrote (server-set) |
| `tags` | sorted unique strings (<=80 chars each) |
| `links` | sorted `[dst, rel]`, `rel` in `about supersedes conflicts related`; the record is `src` |

| Kind | `data` |
|---|---|
| F J T | `title, body, parent, status (open/claimed/closed), closed_reason, git_ref, alias, claimed_by, closed` |
| M R I | `summary` (one line, <=160), `body, alias, source, session` |
| D | `title` (one line, <=200), `body, alias, mime` (default `text/markdown`), `source` |
| N | `body` (the whole file). Server derives `alias` (frontmatter `id`, else stem) and `summary` |

- Type names: feature, job, task, memory, rule, issue, document, note. `idea` = a memory tagged `idea` (no kind of its own).
- `verify` never leaves a machine: it is a shell command, so it is not sent, stored, served or applied. A remote `git_ref` is applied only when it is a commit hash or a branch named for that feature.
- Note ids match `^(arch/|research/)?[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$`; a file named `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` or `placement.md` (any case, any folder) never syncs. Both sides enforce this before any file write.
- `updated` or `deleted_at` more than 5 minutes ahead of the server clock is clamped to it.
- Limits: one record <= 1 MB JSON; <= 200 ops and <= 8 MiB per sync request (the client cuts batches at 4 MB); <= 500 `want` ids; a `changes` page <= 500 records; any other body <= 1.1 MB. Over a limit: 413.
- Text fields pass secret redaction on the server at write and on the client before a local write.

## Focus, skills and the project document

None of these is a record kind or a schema change: each is a document (`D`) told apart by tags, so it syncs, searches and tombstones like any document. Python (`shapa/focus.py`, `skills.py`, `db.add_doc`) and the Worker (`focus.ts`, `skills.ts`, `projects.ts`) implement the same rules.

| What | Record | Rules |
|---|---|---|
| Focus | one `D` per wiki: alias `focus`, tag `focus`, mime `application/json`, body `{"fires":[{"feature":"F22","text":""},null,null],"northstar":"...","v":1}` | `northstar` <= 600 chars, one line. Exactly three ranked slots, each empty, a ledger feature (an open feature of that wiki, never twice), or free text <= 200 (the global wiki has no ledger: text only). A fourth is refused. A malformed body reads as empty |
| Rank | tag `rank:N` (1-999) on a feature | lower first; unranked after ranked; ties oldest first |
| Skill version | one `D` per version: tags `skill`, `skill:NAME`, `v:N`, `status:active\|pending\|previous`, alias `skill:NAME.vN`, title `NAME vN`, body a `SKILL.md` (frontmatter `name`, `description` <= 1024, then text) | `NAME` is `[a-z0-9][a-z0-9-]{0,63}`; a skill lives in the wiki that owns it (global, or a repo's); every version is kept; at most one is `active` (the highest if a race left two); activating moves the old one to `previous` |
| Project document | any `D` without a `focus` or `skill` tag; its project is the `project:KEY` tag (`[a-z0-9][a-z0-9._/-]{0,79}`), else the wiki's own | one live document per project per wiki |

- The tags `focus` and `skill` are reserved: REST create, PATCH, `shapa doc add` and `shapa row tag` refuse them. A sync put that carries them must carry what they promise (a skill: `skill:NAME`, `v:N`, a status; a focus record: a JSON object body, and only one per wiki), so they are no way around the lean gate.
- Lean gate: the creation of a second project document is refused with a message naming the first (REST 409, MCP error, `shapa doc add` exit 2), or appended to it as a `## title` section with `merge: true` (REST create body, MCP `save`, `--merge`). Both sides cap the result at 1 MB. A retag (PATCH, `shapa row tag`) that would move a document into a held project is refused. Documents already stored are grandfathered: a sync put is never refused for the project, because the server cannot tell a document a client already holds from a new second one, so two devices that race can leave two documents. `shapa doc list` flags a project over the limit and `shapa doc merge SRC DST` folds them.
- A `project:KEY` tag must be exactly `clean_project(KEY)` (`cleanProject` in the Worker): `project:Foo`, a zero-width or full-width lookalike, an empty or overlong key are refused on REST create, PATCH, a sync put and `shapa doc add` / `shapa row tag`, checked on the tag as sent (before the 80-character tag cut).
- The focus record has one fixed uid in every wiki (`shapa-focus-record`, `FOCUS_UID` in `focus.py` and `focus.ts`): two devices that each create it offline hold the same record, sync merges them (last write wins), and the Worker still refuses a focus put under another uid.
- A skill version is never edited through REST PATCH or the MCP `update` tool, and the Python MCP `skill` tool cannot activate: only `shapa skill activate` (reaching the store by sync) does. No PATCH, MCP `update`, sync put or `shapa row tag` adds or removes the `focus` or `skill` tag of a stored record (a focus record retagged `skill` would be an active skill version), a record never carries both, and the focus record's tags are fixed.
- Activation needs the delete scope. A sync put that carries `status:active` on a skill version, or edits or demotes a stored active one, is accepted only from a device token (never an OAuth token) holding `delete`, which is the operator's own machine, so `shapa skill activate` keeps working. From any other device a new active version is stored as `status:pending` (the put is `accepted`, the version is pending and the next pull says so), and a put that touches a stored active version is `rejected` ("changes only from a device token with the delete scope"). Pending versions are still pushed by write-only devices.
- A skill is served by name only while one version is active (REST `GET .../skills/NAME`, MCP `skill`, `shapa skill show`, Python MCP `skill show`): with none, it is not found and a pending version is read by number (`?version=N`, `--version N`, `version`). Lessons are capped: a row files for at most 3 skills (its first three `skill:` tags) and a skill keeps at most 5 pending versions (later lessons are not filed until the operator activates or retires some); the Worker checks the once-per-row marker and the pending count from tags and a SQL lookup, loading only the one body the new version copies.
- Closing a feature that is a fire frees its slot, the later fires move up, and the best candidate takes the last slot: `shapa/db.py` `close`, the Worker's `close` (REST and MCP). A closed or missing feature is not shown. Free-text fires are never freed. The operator overrides with `shapa focus promote` / `POST .../focus/promote`.
- A memory, rule or issue that names a skill (tag `skill:NAME`; in Python also an `about` link to a version) files a new `pending` version: the skill's latest version (any status) plus a `## Lesson [ROWID]` section whose lines are all quoted (`> `), the body cut at 1500 characters, once per row. It never activates. Python does it in `add_row`, `tag` and `link`; the Worker in REST and MCP create and in PATCH of tags; a pulled record never re-files, so a lesson is filed once. Only a device with the write scope can file one.
- Skill bodies never reach session start or session instructions.

## The learning loop: what syncs and what stays on the device

Nothing in the loop is a record kind, a link relation or a Worker change; it is ordinary rows, tags and bodies, so a sync put and the Worker's REST and MCP treat it as any rule or issue.

- **Syncs** (plain records): a principle is an `R` row tagged `principle` (statement = summary; body = optional `Why:` and `Also:` lines, then `Evidence:` lines `- I12 repo-key date: text`; counters in tags `violations:N`, `revisions:N`, `evidence:N`, `last-seen:ISO`, `serves:REPO-KEY/F3|northstar|none`, and `churn`); a learned issue carries `learned`, `learned-at:ISO`, `generalized-into:REF`, `serves:...`; a repeat carries `repeat`, `repeat-of:REF` (`global:` prefixes the other wiki's row), `violation`, `blocked`; `related` and `conflicts` links; a feature that was ever a fire carries the item tag `fire`. Tags are at most 80 characters and 100 per record, bodies far below the record limit (evidence keeps the newest 12, wordings 4). `REF` is a label: when the sync relabels a colliding record, tags that name the old label are not rewritten, so after such a collision a `repeat-of` or `generalized-into` can name another row. That is a known gap: it skews a count, never a principle's text, and never activates anything.
- **Skill versions** a principle files (a rule tagged `skill:NAME`) are the documents of the section above, with its delete-scope activation rule untouched. `shapa learn` never activates one: it files a pending version and only `shapa skill activate` makes it live, on this device or any other. A principle that syncs in keeps its tags as data; the receiving device ranks it by its text alone until it writes the principle itself (`p_local`).
- **Never synced, per device**: raw metric events, fetch and bootstrap latency, skill-use and served counts, which principles this device wrote itself (`p_local`), the similarity index and the cross-repo mirror (`l_chunks`, `l_tok`, `l_vec`), events and serve counts (`m_events`, `p_served`), all in the derived index, and the daily snapshots (`metrics_daily` in the committed database file, which the sync never reads). The Worker has no `learn` or `metrics` tool and route; `shapa learn` and `shapa metrics` are CLI only.
- **Client heals what the Worker does not write**: the Worker's focus set does not tag a feature `fire`, so the client reads the focus record's current fires as well as the tag, and tags them when it archives (`metrics.fire_ids`).

## Not hosted: the code map

A fire's code partition (`shapa map`, `shapa partition`, MCP `partition` of the local server) is derived from a checkout (`shapa/codemap.py`, the gitignored `.shapa-code.db`), so it is local to the device that has the code. The Worker stores no graph, no partition and no file path of a map, has no `partition` tool or route, and no sync record carries one: a chat client without a checkout has nothing to partition. The fire itself (feature, jobs, the files and arch boxes their text names) still reaches it through `focus` and `instructions`.

## Wikis

Key `global` for the global wiki; a repo wiki's key is the lowercase slug (`^[a-z0-9][a-z0-9._-]{0,63}$`) of its primary checkout's directory name, overridable with `--key`. `PUT /v1/wikis/{key}` `{name, kind, origin}` is idempotent; `origin` is `host/owner/repo` (never credentials). Same key with a different origin: 409. A repo with no git origin has no automatic key: it syncs after one `--key KEY`, which the sync state keeps.

## Auth

| Credential | Use |
|---|---|
| Owner key | secret `SHAPA_OWNER_KEY` (>=32 chars, else admin and consent answer 503). `Authorization: Bearer` on `/admin/*`; typed into the OAuth consent page. 5 failures per 15 min per IP, then 429 |
| Device token | `shp_<device_id>_<secret>`; minted by `POST /admin/devices`, shown once, stored as SHA-256 |
| OAuth 2.1 | chat clients: `/authorize` (consent: owner key, device name, scopes, optional wiki list), `/token`, `/register`. Each grant is one device; the token's resource is `<origin>/mcp` and it is also accepted on `/v1` |

- Scopes: `read`, `write` (needs `read`), `delete` (needs both). A device may be limited to a list of wiki keys (default all).
- Every request resolves to a device row in the store; a revoked device is 401 on its next request, token or OAuth.
- Admin: `POST /admin/devices` `{name, scopes, wikis?}` -> `{id, token}`; `GET /admin/devices`; `DELETE /admin/devices/{id}`.
- Unauthenticated: `GET /health`, `GET /openapi.json`.

## REST (`/v1`, JSON, Bearer)

| Route | Scope | Does |
|---|---|---|
| `GET /whoami` | read | the calling device |
| `GET /wikis`, `PUT /wikis/{wiki}` | read, write | list, register |
| `GET /instructions?client=mcp\|rest&wiki=&budget=&full=` | read | session defaults: `{text, version}` (`Accept: text/plain` for the text alone). Default: northstar, fires and their tree; `full=1` adds rules and the other live features |
| `GET /wikis/{wiki}/focus` | read | `{wiki, northstar, fires: [fire\|null x3], candidates}`; a fire is `{slot, feature, text, title, status, jobs: [{id, title, status, tasks}], files, boxes}` |
| `PUT /wikis/{wiki}/focus` | write | `{northstar?, fires?: [{feature?, text?}\|null, ...<=3]}`; 400 for a fourth fire, a non-feature, an empty fire; 409 for a closed or repeated feature |
| `POST /wikis/{wiki}/focus/promote` | write | `{feature?, slot?}`: default the best candidate into the first free slot; 409 with no free slot |
| `GET /wikis/{wiki}/skills`, `GET /wikis/{wiki}/skills/{name}?version=` | read | `{skills: [{name, description, active, pending, versions}]}`; one version `{name, version, status, description, id, body}` (the active one without `version`) |
| `GET /search?q=&k=&wiki=&types=` | read | FTS5 BM25 over live records: `{results: [{wiki, id, type, summary, updated}], mode}` |
| `GET /get?id=&wiki=` | read | by id or alias across wikis: `{matches: [record + wiki]}` |
| `GET /wikis/{wiki}/records?kind=&status=&tag=&after=&limit=` | read | list |
| `GET /wikis/{wiki}/records/{id}` | read | one record (id or alias, URL-encoded) |
| `POST /wikis/{wiki}/records` | write | create from `{type, summary\|title, body, tags?, alias?, parent?, path?, mime?, project?, merge?}`; server allocates id and uid; 201 with the record, 200 with the existing row when its text is identical (or the project's document grown by `merge`); a second project document is 409 naming the first |
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
| del | `{op: "del", id, uid, deleted_at, base_rev: int\|null}` (needs scope `delete`) |

The server applies `ops` in order inside one transaction:

| Case | Result |
|---|---|
| put, a document whose project another live document (another `uid`) already holds, or a `focus`/`skill` record that does not carry what its tag promises | `rejected` with `error`, checked before every row below |
| put, id unknown | insert: `accepted` |
| put, id held by a record with another `uid` (tombstones count) | stored under a fresh id: `renamed` + `new_id`; later ops in the request that name the old id as `parent` or link `dst` are rewritten |
| put, `uid` already stored under another id | that stored record is the target: the same-`uid` rows below apply, and the result also carries `new_id` (the stored id) |
| put, same `uid`, content equal | `noop` |
| put, same `uid`, `base_rev` equals current `rev` | replace: `accepted` |
| put, note, `base_rev` null, a live note with another body is stored | `rejected`: the stored note wins (a file's mtime is not an edit time) |
| put, same `uid`, otherwise | incoming `updated` strictly newer wins (`accepted`), else `rejected` |
| del, no live record with that `uid` (found by `uid`, under any id) | `noop` |
| del, `base_rev` equals `rev`, or record `updated` <= `deleted_at` | tombstone: `accepted` |
| del, otherwise | `rejected` (a later edit beats a delete) |

- `results[i]` is `{id, status, rev, new_id?, error?}` for `ops[i]`. Every accepted change takes the next `rev`. An alias another live record holds is dropped, not refused.
- `changes`: every record with `rev` > request `cursor`, oldest first, plus the current record for each rejected op and each `want` id. A page holds at most 500 records and 8 MB; `more: true` means call again with the returned cursor and no ops.
- `counters`: next number per kind; each side keeps the maximum. The server raises its own to the request's before applying any op, so one collision never shifts the labels after it.

Client (`shapa remote sync`), per wiki, under a file lock:

1. Snapshot local records. State (`sync/<sha1 of db path>.json` beside the credentials) holds `{key, cursor, base: {id: {rev, uid, updated, hash}}}`; `hash` is local-only.
2. Classify against `base`: absent from base = new (put, `base_rev` null); hash differs = modified (put, `base_rev`); in base but gone locally = deleted only when `tombstones` holds it (del), else missing (`want`). Every tombstone is sent, in base or not (`base_rev` null), until the server answers for it. The database carries a sync token (`meta.sync_token`), rotated in the apply transaction of every sync that pushed or applied anything and kept in the state. Token differs = the database was swapped (git): a differing record whose local `updated` is not newer than base's is stale (`want`), so the swap heals from the server. Token equal: clocks are never consulted.
3. Send (parents first: F, J, T, then the rest), in batches.
4. In one local write transaction: apply `renamed` (relabel id, child `parent`, tags, links; foreign keys deferred), then each change, skipping any record whose local content changed since step 1; raise counters; drop acknowledged tombstones; write state.

Note files sync only while the wiki's primary checkout is on its default branch (always, outside git). A remote delete removes a local note only if it is unchanged since the last sync; a note deleted locally comes back (delete it through the API). Remote text is never executed and never picks a path outside the note pattern.

## MCP (`/mcp`, streamable HTTP)

Server `instructions`: call `session_start` first. No delete tool.

| Tool | Args | Does |
|---|---|---|
| `session_start` | `client?, wiki?, full?` | the session defaults text: northstar, fires and their tree (`full`: also rules and the other live features) |
| `focus` | `wiki?` (default `global`) | the northstar, the fires with their tree, the candidates |
| `focus_set` | `wiki, northstar?, fires?` or `promote?` | set them, or promote a feature; never both forms at once |
| `skills`, `skill` | `wiki?`; `name, wiki?, version?` | list skills; read one version (the active one by default) |
| `search` | `query, k?, wiki?, types?` | as REST search |
| `get` | `id, wiki?` | full record; lists every match when the id is in several wikis |
| `save` | `wiki, type (memory/idea/rule/issue/document/note), summary\|title, body, tags?, alias?, path?, project?, merge?` | create (a document obeys the lean gate) |
| `update` | `wiki, id, summary?, title?, body?, tags?, base_rev?` | edit |
| `ledger_list` | `wiki?` | live features with jobs and tasks |
| `ledger_add` | `wiki, kind, title, parent?, body?` | add |
| `ledger_set` | `wiki, id, action (claim/release/close/edit), fields?` | move or edit |
| `wikis` | - | keys, names, counts |

## Session instructions

Composed per request within `budget` characters (default 6000, 2000-40000), template version 2: what shapa is; the northstar of each wiki in scope (global first); the fires of each, ranked, each with its feature, open jobs and tasks and the files and arch boxes their text names; a hint with the top candidates where no fire is set; the global note `session-instructions` if present; the operator style (global note `response-style`); then the write-back format for the client kind: when to write each type and the exact `save` call (`mcp`) or `POST` body (`rest`). Only with `full`: the global wiki's rules (id + summary; a named `wiki` adds its own) and the live features per wiki. Every record text is masked and one line, and no skill body is ever included.

## Client commands

| Command | Does |
|---|---|
| `shapa remote login URL [--device NAME] [--scopes ...] [--token-stdin]` | owner key from `$SHAPA_OWNER_KEY` or a prompt, never argv; writes `remote.json` (`url, device, device_id`) and `remote.token` (0600) under `~/.shapa` (`$SHAPA_REMOTE_DIR` overrides) |
| `shapa remote sync [--cwd DIR] [--all] [--key KEY] [--hook] [--if-changed] [--dry-run] [--json]` | wikis in scope (repo + global), or every registered one; `--hook` reads hook stdin, stays within 8 s, always exits 0, prints at most one line; `--if-changed` skips the network when nothing local changed |
| `shapa remote status [--check] [--json]` | login, last sync and pending changes per wiki; `--check` exits 1 when not logged in or unreachable |
| `shapa remote devices [--revoke ID]`, `logout`, `instructions [--client NAME]` | device admin (owner key), forget credentials, print the session defaults |
| `shapa doc add\|edit\|rm\|list\|merge` | documents; `shapa get D<n>`; MCP `save` gains `document` and `idea`; `add` takes `--project` and `--merge` |
| `shapa focus ...`, `shapa skill ...` | the northstar and fires, and skills (see "Focus, skills and the project document"); both sync as documents, with no command of their own |

## Invariants

- The server never trusts a client for `rev`, `device` or id allocation on REST writes.
- No secret in the repo: account ids, URLs and keys stay in the deployer's environment and `~/.shapa`.
- Local database schema 2 is additive (`docs`, `tombstones`, counter `D`); a schema-1 reader still works.
- Hooks never block a session and never fail it.
- On a wiki with sync state the after-merge sweep never deletes a memory for disuse: use counts are per device.
