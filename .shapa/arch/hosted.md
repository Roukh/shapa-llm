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

- `hosted/` - the Worker (TypeScript): `src/index.ts` (OAuth provider wiring), `store.ts` (Durable Object: schema, records, sync, search), `records.ts` (zod wire schemas, the single source of the wire types and of `/openapi.json`), `api.ts` (REST), `mcp.ts`, `auth.ts`, `instructions.ts`, `redact.ts`, `focus.ts` (northstar, fires, promotion), `skills.ts` (versions, lessons), `projects.ts` (the lean gate), `tags.ts` (system tags, stage tags, buckets, the one rule for what a baseline device sees), `gates.ts` (tag and kind rules, the stage-0 gate, the baseline budget), `relations.ts` (the relation index and its reads), `tagapi.ts` (tag index, related), `tasks.ts` (the task list), `projectfile.ts` (the template, sections, items), `projectapi.ts` (project create, items, read, list, the `about` link), `reach.ts` (what a baseline device may write).
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

- Type names: feature, job, task, memory, rule, issue, document, note. `idea` and `decision` are memories tagged `idea` / `decision` (no kind of their own); `human_task` and `agent_task` are tasks tagged so.
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
- A skill version's body reaches the session text only as a baseline skill: the active version of a skill carrying `stage:0` (printed with its name and description, the body indented and cut). A pending, previous or higher-stage version never does, and the local `shapa bootstrap` prints a skill's name and description only.

## Tags, stages and relations

Tags are the relations. No new kind and no breaking schema change: `record_tags`, `record_links`, `tag_counts` and two columns (`records.base_vis`, `devices.reach`) are additive, written by `Repo.save` (so REST, MCP, sync, tombstones and the lesson filer all reach them) and rebuilt once (`meta` key `relations_v1`, safe to run twice) for rows stored before them.

| What | Rule |
|---|---|
| Stage tag | `stage:N`, N 0-9, on a rule, a memory or a skill version only; no other `stage` or `stage-proposed` spelling is accepted. `stage-proposed:0` is the only proposal tag. One stage marker per record: at most one of `stage:N` or `stage-proposed:0`, never both, on create, PATCH, MCP, a sync put and the one-time relabel (`relabelStage`: a legacy `stage:0` becomes `stage-proposed:0` in the global wiki, is dropped in a repo wiki, and is dropped beside a real `stage:1`-`stage:9`). `stage:0` and `stage-proposed:0` exist only in the global wiki (the baseline is the global wiki's); a repo wiki takes `stage:1`-`stage:9`. Checked on REST create and PATCH, MCP `save` and `update` (same code) and every sync put |
| Lookalikes | a tag that becomes a system tag (a name of `SYSTEM_TAGS`, or anything under a `SYSTEM_PREFIXES` prefix: `stage:`, `stage-proposed:`, `skill:`, `v:`, `status:`, `rank:`, `project:`) after NFKC folding, lower-casing and removal of spaces, control, format (zero-width, bidi) and variation-selector characters, but is not byte-equal to it, is refused (400, a sync put `rejected`) on every path, for every system tag: `Human_Task`, `agent` + a zero-width space + `_task`, `IDEA`, `Project:x`. One list (`tags.ts`), so the owner tags cannot be forged to drop the operator's task on a pull |
| Stage 0 is the operator's | only a sync put from a device TOKEN (never OAuth) holding the `delete` scope, with reach `full`, may add or remove `stage:0` or edit the text or tags of a record that carries it. From anyone else: a new record or a retag carrying `stage:0` is stored with `stage-proposed:0` (accepted; REST sets `X-Shapa-Notice`, MCP says so, a sync result carries `note`), and an edit of a stored stage-0 record is refused (REST 403, MCP error, sync `rejected`: "changes only from a device token with the delete scope"), with or without `base_rev`, by id or by uid. REST PATCH and MCP `update` never set stage 0, whatever the token. A stage:0 tag already stored when stages arrived becomes `stage-proposed:0` in the one-time backfill |
| Baseline budget | the stage-0 set of the global wiki (a rule or memory: summary + body; an ACTIVE skill version: its description, a pending or previous one costs nothing because the session text never prints it) is at most 6,000 characters (`BASELINE_BUDGET`); a put past it is `rejected` naming the current size. A repo wiki has no baseline (it refuses `stage:0`) |
| Identical text | saving text a record already holds (a rule, memory, issue or open task) never changes that record's tags, stage, project, links or device unless the writer could have made that edit directly: the merge goes through the gates of a PATCH (tag rules, a stored `stage:0` record, reach, rank tags, stage gate). A writer that may not edit the holder gets the holder back unchanged (200) when it can read it, and a new separate record when it cannot (a baseline device never finds a hidden holder, so nothing reveals it). Tags that would make the holder invalid (two stages, two projects) are a 400, as an edit would be. A lesson is filed only from a non-baseline writer |
| System tags | `stage:*`, `stage-proposed:*`, `skill`, `skill:*`, `v:*`, `status:*`, `rank:*`, `focus`, `project:*`, `human_task`, `agent_task`, `idea`, `decision` (`tags.ts`: never a topic, never in the tag index unless `system=1`) |
| Project tag | `project:KEY` (`cleanProject`), at most one per record. A task, memory (ideas and decisions included), rule, issue or skill version carrying it also gets an `about` link to the project's document in the same wiki: set on create, moved or removed on retag, added on a sync put, and added to the records already waiting when the document is created later (at most 500 per document, oldest first). Never for the document itself, a feature or a job. `project` is accepted on an issue (REST create, MCP `save`). A project's file created by a baseline device links only the records it can read and edit (tasks, ideas, decisions); the rest are linked when a full device next writes them, and project reads resolve membership by tag, so they stay complete |
| Owner tags | `human_task` / `agent_task` only on a task, never both. The global wiki accepts kind `T` only, standalone, with exactly one of them: `F` and `J` are refused there (REST 400, sync `rejected`) |
| Tag index | `record_tags(wiki, tag, id, kind, open, vis, rank, seq, rev)` (indexes `(tag, wiki)`, `(wiki, tag, kind, open, rev)`, `(wiki, tag, rev)`, `(tag, kind, open, rank, seq)`; `GET /v1/wikis/{wiki}/records?tag=` lists through it, ordered by `rev`, never by a scan of every record's tags), `record_links(wiki, src, dst, rel)` (index `(wiki, dst)`), `tag_counts(wiki, tag, bucket, vis, n)`. Buckets: R rule, M memory, E idea, C decision, I issue, S skill version, D document, T task, J job, F feature, N note; O counts open tasks. Reads walk these, so what they cost does not grow with the record count: `GET /v1/tags` and TOPICS read `tag_counts` (the work is the number of distinct tags), `tasks` and the PROJECTS part walk the owner-tag index, `related` reads at most 400 neighbours per tag and follows at most 12 of a record's tags |
| Related order | records linked to it (either direction), then by number of shared non-system tags, then newest; default 10, at most 50 |
| Search by tag | `GET /v1/search?q=&tags=a,b` (`q` optional with tags; every tag must match; at most 8); on a score tie (scores within a tenth) the lower stage ranks first |
| Projects | a project is `project:KEY`; its one document (the lean gate) starts from a template held in `projectfile.ts` (`PROJECT_TEMPLATE`, byte for byte as the contract's, ending with a newline). Section keys `issues questions implementations problem solution diff advantages villains loop_market loop_product`. An item is one line, masked, at most 500 characters, invisible characters removed; it fills the empty `1. ` first, is a no-op when the section already holds the text, creates a missing heading at its template position, and keeps the file under 1 MB. A wiki holds at most 1,000 projects |
| Task list | `GET /v1/tasks`: `human_task` (or `agent_task`) tasks of every readable wiki, `rank:N` (1-999) first then oldest, at most 100 with `total`. Identical open text (same owner tag, title, body, no parent) in a wiki is stored once |

## Reach

Every device has a reach, stored on its row (`devices.reach`, additive): `full` or `baseline`. An OAuth grant is always `baseline` (a row stored before reach existed is `baseline` when its source is oauth, else `full`). A device token is `full` unless minted `POST /admin/devices {..., "reach": "baseline"}` (for an agent that speaks REST with a Bearer token). `GET /v1/whoami` and the admin list show it. The local engine's machine holds a `full` token and is unaffected.

- Read: the filter is one place. `Repo.view(reach)` is what every operation is handed (`ShapaStore.act` is the only way in), and it applies `base_vis` / `vis` to every record read, list, count, tag, project, task, related and search. A baseline device sees a rule, memory, issue or skill version only when it carries `stage:0`, plus ideas and decisions, notes, documents, tasks, features and jobs and the focus tree; anything else answers exactly as if it did not exist (404, absent from lists and counts). The `stage` threshold is fixed at 0 for it. `test/reach.test.ts` walks every GET route of `/openapi.json` and every read MCP tool as an OAuth client and as a baseline token, and fails when a route or tool is not in its table.
- Write (with the write scope): ideas and decisions, notes, documents, tasks (`human_task`, `agent_task`, `task`), project files and items, task claim, release and close, and edits of the notes, documents, tasks, ideas and decisions it can read. A write of a rule, memory (other than an idea or decision), issue or skill version is 403 at every stage, proposals included ("this device writes notes, tasks, ideas, decisions and files"); no stage tag, no feature or job, no `rank:N` set or dropped.
- Never: `POST .../sync` (403), the focus (set, promote, or PATCH of the focus record), a hidden or stage-0 record, any delete, a `skill` or `skill:NAME` tag (403 on any write; no lesson is ever filed from a baseline device's writes, on any path, an identical-text save included: it could otherwise file versions of a skill it cannot read, and the id and revision jump would say whether the skill exists), a `rank:N` tag.
- An alias a baseline device asks for that any live record holds is dropped silently (the record is created without it; a rename keeps the alias the record had), never a 409: the holder may be hidden from it, and the alias's existence would leak. A full device still gets the 409 naming the holder.
- Saving text a hidden record already holds creates a new separate record (the baseline device never finds the holder); saving text a stage-0 record holds returns that record unchanged ("Identical text").
- The notes printed into every session text (`session-instructions` and `response-style` in the global wiki, by id, alias, path stem or frontmatter id, any case, with or without `.md`) are written, edited or deleted only by a device TOKEN with the delete scope and full reach (the operator's machine), on REST, MCP and sync, from any other device a 403 (sync `rejected`). Their text is masked (`redact`) and stripped of control, format and line-separator characters when printed; its line breaks stay.
- The write-back block of the session text is chosen by reach: a baseline device is shown only the types it may write, and nothing else of the text names a tool it may not use (no `focus_set`, no focus PUT).
- The stage-0 baseline (rules, memories, active skills) is part of the session text of every authenticated device, including one whose wiki list leaves out `global`; nothing else of the global wiki reaches such a device. The `stage` threshold is fixed at 0 for such a device too (as for a baseline one): only stage 0 is exempt from the wiki list, so `stage=9` never prints a global `stage:1`-`stage:9` rule, memory or skill body to a device that may not read `global`.
- Never, also: registering a wiki (`PUT /v1/wikis/{key}` for a key that does not exist is 403 for a baseline device; an existing one still answers 200); an engine tag on what it writes (any system tag or family of `tags.ts`: the learning loop's `principle`, `churn`, `violations:N`, `serves:` and the rest, `v:`, `status:`, `focus`, `rank:`, `stage:`; only `idea`, `decision`, `human_task`, `agent_task` and one `project:KEY` pass; a tag the record already held stays through an edit; a full device's write, sync included, is unchanged).
- The 1,000-project cap of a wiki counts, for a baseline device, only the projects it can see, so hitting it never tells it about a hidden project.

## The learning loop: what syncs and what stays on the device

Nothing in the loop is a record kind, a link relation or a Worker change; it is ordinary rows, tags and bodies, so a sync put and the Worker's REST and MCP treat it as any rule or issue.

- **Syncs** (plain records): a principle is an `R` row tagged `principle` (statement = summary; body = optional `Why:` and `Also:` lines, then `Evidence:` lines `- I12 repo-key date: text`; counters in tags `violations:N`, `revisions:N`, `evidence:N`, `last-seen:ISO`, `serves:REPO-KEY/F3|northstar|none`, and `churn`); a learned issue carries `learned`, `learned-at:ISO`, `generalized-into:REF`, `serves:...`; a repeat carries `repeat`, `repeat-of:REF` (`global:` prefixes the other wiki's row), `violation`, `blocked`; `related` and `conflicts` links; a feature that was ever a fire carries the item tag `fire`. Tags are at most 80 characters and 100 per record, bodies far below the record limit (evidence keeps the newest 12, wordings 4). `REF` is a label: when the sync relabels a colliding record, tags that name the old label are not rewritten, so after such a collision a `repeat-of` or `generalized-into` can name another row. That is a known gap: it skews a count, never a principle's text, and never activates anything.
- **Skill versions** a principle files (a rule tagged `skill:NAME`) are the documents of the section above, with its delete-scope activation rule untouched. `shapa learn` never activates one: it files a pending version and only `shapa skill activate` makes it live, on this device or any other. A principle that syncs in keeps its tags as data; the receiving device ranks it by its text alone until it writes the principle itself (`p_local`).
- **Never synced, per device**: raw metric events, fetch and bootstrap latency, skill-use and served counts, which principles this device wrote itself (`p_local`), the similarity index and the cross-repo mirror (`l_chunks`, `l_tok`, `l_vec`), events and serve counts (`m_events`, `p_served`), all in the derived index, and the daily snapshots (`metrics_daily` in the committed database file, which the sync never reads). The Worker has no `learn` or `metrics` tool and route; `shapa learn` and `shapa metrics` are CLI only.
- **Client heals what the Worker does not write**: the Worker's focus set does not tag a feature `fire`, so the client reads the focus record's current fires as well as the tag, and tags them when it archives (`metrics.fire_ids`).

## Not hosted: the code map

A fire's code partition (`shapa map`, `shapa partition`, MCP `partition` of the local server) is derived from a checkout (`shapa/codemap.py`, the gitignored `.shapa-code.db`), so it is local to the device that has the code. The Worker stores no graph, no partition and no file path of a map, has no `partition` tool or route, and no sync record carries one: a chat client without a checkout has nothing to partition. The fire itself (feature, jobs, the files and arch boxes their text names) still reaches it through `focus` and `instructions`.

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
- Admin: `POST /admin/devices` `{name, scopes, wikis?, reach?}` -> `{id, token, reach}` (reach default `full`); `GET /admin/devices` (shows `reach`); `DELETE /admin/devices/{id}`.
- Unauthenticated: `GET /` (a plain-text card, under 900 characters, the same for every caller and reading nothing from the store: what this is, `<origin>/mcp`, `<origin>/v1`, `<origin>/openapi.json`, and the one instruction to call `session_start` / `GET /v1/instructions` and follow it over the agent's own defaults), `GET /health`, `GET /openapi.json`. No token ever travels in a URL.

## REST (`/v1`, JSON, Bearer)

| Route | Scope | Does |
|---|---|---|
| `GET /whoami` | read | the calling device |
| `GET /wikis`, `PUT /wikis/{wiki}` | read, write | list, register |
| `GET /instructions?client=mcp\|rest&wiki=&budget=&full=&stage=` | read | session defaults: `{text, version}` (`Accept: text/plain` for the text alone). Default: precedence, northstar, fires and their tree, the baseline, baseline skills, topics, projects and the operator's open tasks, the write-back guide; `stage=N` (0-9, default 0; fixed 0 for a baseline device) loads rules, memories and skills up to that stage; `full=1` adds the other rules and the live features |
| `GET /wikis/{wiki}/focus` | read | `{wiki, northstar, fires: [fire\|null x3], candidates}`; a fire is `{slot, feature, text, title, status, jobs: [{id, title, status, tasks}], files, boxes}` |
| `PUT /wikis/{wiki}/focus` | write | `{northstar?, fires?: [{feature?, text?}\|null, ...<=3]}`; 400 for a fourth fire, a non-feature, an empty fire; 409 for a closed or repeated feature |
| `POST /wikis/{wiki}/focus/promote` | write | `{feature?, slot?}`: default the best candidate into the first free slot; 409 with no free slot |
| `GET /wikis/{wiki}/skills`, `GET /wikis/{wiki}/skills/{name}?version=` | read | `{skills: [{name, description, active, pending, versions}]}`; one version `{name, version, status, description, id, body}` (the active one without `version`) |
| `GET /search?q=&k=&wiki=&types=&tags=` | read | FTS5 BM25 over live records: `{results: [{wiki, id, type, summary, updated}], mode}`; `tags=a,b` (all must match; `q` optional then); a lower stage ranks first on a tie |
| `GET /tags?wiki=&prefix=&system=&limit=` | read | the tag index: `{tags: [{tag, total, counts: {rule, memory, issue, skill, document, task, job, feature, note}}]}`, by count (default 200, at most 1000); system tags only with `system=1` |
| `GET /wikis/{wiki}/records/{id}/related?limit=` | read | `{id, wiki, related: [{id, type, summary, updated, via: link\|tags, shared}]}` |
| `GET /tasks?owner=human\|agent&status=open\|closed\|all&project=&wiki=&limit=` | read | the operator's task list: `{tasks: [{wiki, id, title, project, owner, status, created, rank}], total}`, rank then oldest, at most 100 |
| `GET /projects?wiki=`, `GET /projects/{key}?wiki=` | read | the projects of a wiki (default `global`): `{projects: [{key, title, open_tasks}], total}`; one project: `{key, wiki, title, document: {id, sections: {<key>: [text]}}, tasks, ideas, decisions, rules, skills, memories, features, more}`, 50 each with the rest counted in `more` (the open features of the repo wiki named KEY come with it). URL-encode a slash in KEY |
| `PUT /projects/{key}` `{title?, wiki?}` | write | create the project's file from the template; idempotent (201 created, 200 already there) |
| `POST /projects/{key}/items` `{section, text, wiki?}` | write | append one numbered item (201), or nothing when the section holds the text (200); 400 for a bad section or item, 404 for no project, 413 past 1 MB |
| `GET /get?id=&wiki=` | read | by id or alias across wikis: `{matches: [record + wiki]}` |
| `GET /wikis/{wiki}/records?kind=&status=&tag=&after=&limit=` | read | list |
| `GET /wikis/{wiki}/records/{id}` | read | one record (id or alias, URL-encoded) |
| `POST /wikis/{wiki}/records` | write | create from `{type, summary\|title, body, tags?, alias?, parent?, path?, mime?, project?, merge?}`; `type` also `decision`, `human_task`, `agent_task`; `project` tags a task, idea, decision, rule or memory (and links it to the project's file) or names a document's project; server allocates id and uid; 201 with the record, 200 with the existing row when its text is identical (or the project's document grown by `merge`; an open task with the same owner, title and body); a second project document is 409 naming the first; a `stage:0` tag is stored as `stage-proposed:0` and `X-Shapa-Notice` says so |
| `PATCH /wikis/{wiki}/records/{id}` | write | edit fields; optional `base_rev`, mismatch is 409 with `current`; a stored `stage:0` record is 403 |
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
| put, a document whose project another live document (another `uid`) already holds, or a `focus`/`skill` record that does not carry what its tag promises; a tag list or kind the tag rules refuse (a malformed stage tag, two project tags, a task with both or neither owner tag in the global wiki, an `F`/`J` in the global wiki) | `rejected` with `error`, checked before every row below |
| put that edits a stored `stage:0` record, or that would take the global baseline past 6,000 characters | `rejected` unless the device is a token with the `delete` scope (an identical put is `noop`); a new `stage:0` from any other device is `accepted` and stored as `stage-proposed:0`, the result carrying `note` |
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

- A `baseline` device never syncs: the whole request is 403.
- `results[i]` is `{id, status, rev, new_id?, error?, note?}` for `ops[i]`. Every accepted change takes the next `rev`. An alias another live record holds is dropped, not refused. `note` is set on an `accepted` or `renamed` result when the server stored something other than what the client sent: today only a `stage:0` put from a device that may not set stage 0, stored as `stage-proposed:0` ("stage:0 is the operator's: stored as stage-proposed:0 for review (...)"). The client may print it (the next pull carries the stored tags, so the local record is corrected either way); it is never an error.
- A put is also `rejected` (checked before anything else) for: a tag list a rule above refuses (a lookalike of a system tag, two stage markers, `stage:0` or `stage-proposed:0` in a repo wiki, a bad project tag, a task with both or neither owner tag in the global wiki), and a note named `session-instructions` or `response-style` in the global wiki from a device without the delete scope.
- `changes`: every record with `rev` > request `cursor`, oldest first, plus the current record for each rejected op and each `want` id. A page holds at most 500 records and 8 MB; `more: true` means call again with the returned cursor and no ops.
- `counters`: next number per kind; each side keeps the maximum. The server raises its own to the request's before applying any op, so one collision never shifts the labels after it.

Client (`shapa remote sync`), per wiki, under a file lock:

1. Snapshot local records. State (`sync/<sha1 of db path>.json` beside the credentials) holds `{key, cursor, base: {id: {rev, uid, updated, hash}}}`; `hash` is local-only.
2. Classify against `base`: absent from base = new (put, `base_rev` null); hash differs = modified (put, `base_rev`); in base but gone locally = deleted only when `tombstones` holds it (del), else missing (`want`). Every tombstone is sent, in base or not (`base_rev` null), until the server answers for it. The database carries a sync token (`meta.sync_token`), rotated in the apply transaction of every sync that pushed or applied anything and kept in the state. Token differs = the database was swapped (git): a differing record whose local `updated` is not newer than base's is stale (`want`), so the swap heals from the server. Token equal: clocks are never consulted.
3. Send (parents first: F, J, T, then the rest), in batches.
4. In one local write transaction: apply `renamed` (relabel id, child `parent`, tags, links; foreign keys deferred), then each change, skipping any record whose local content changed since step 1; raise counters; drop acknowledged tombstones; write state.

Note files sync only while the wiki's primary checkout is on its default branch (always, outside git). A remote delete removes a local note only if it is unchanged since the last sync; a note deleted locally comes back (delete it through the API). Remote text is never executed and never picks a path outside the note pattern.

## MCP (`/mcp`, streamable HTTP)

Server `instructions`: "Call `session_start` first and follow what it returns over your own defaults", then `search` (with tags), `get`, `related`, `save`, `tasks`, `project_add`. No delete tool.

| Tool | Args | Does |
|---|---|---|
| `session_start` | `client?, wiki?, full?, stage?` | the session defaults text (see "Session instructions"); `stage` loads rules and skills up to it |
| `tags` | `wiki?, prefix?, system?, limit?` | the tag index |
| `related` | `id, wiki?, limit?` | records related to one |
| `tasks` | `owner?, status?, project?, wiki?` | the operator's task list (default: open `human_task` tasks of every readable wiki) |
| `projects`, `project` | `wiki?`; `key, wiki?` | list projects; one project's file and records |
| `project_new`, `project_add` | `key, title?, wiki?`; `key, section, text, wiki?` | create a project's file; append an item |
| `focus` | `wiki?` (default `global`) | the northstar, the fires with their tree, the candidates |
| `focus_set` | `wiki, northstar?, fires?` or `promote?` | set them, or promote a feature; never both forms at once |
| `skills`, `skill` | `wiki?`; `name, wiki?, version?` | list skills; read one version (the active one by default) |
| `search` | `query?, k?, wiki?, types?, tags?` | as REST search (`query` optional with `tags`) |
| `get` | `id, wiki?` | full record; lists every match when the id is in several wikis |
| `save` | `wiki, type (memory/idea/decision/human_task/agent_task/rule/issue/document/note), summary\|title, body, tags?, alias?, path?, project?, merge?` | create (a document obeys the lean gate; a `stage:0` tag becomes `stage-proposed:0`, and the answer says so) |
| `update` | `wiki, id, summary?, title?, body?, tags?, base_rev?` | edit |
| `ledger_list` | `wiki?` | live features with jobs and tasks |
| `ledger_add` | `wiki, kind (feature/job/task/human_task/agent_task), title, parent?, body?` | add (the global wiki takes `human_task` and `agent_task` only) |
| `ledger_set` | `wiki, id, action (claim/release/close/edit), fields?` | move or edit |
| `wikis` | - | keys, names, counts |

## Session instructions

Composed per request within `budget` characters (default 14000, 2000-40000; the baseline alone may be 6,000), template version 3, in this order, each part inside its share of the budget:

1. `PRECEDENCE`: fixed text: the operator's standing rules and focus win over the agent's own defaults and stored preferences; load them every session; record every decision, note, idea and task back.
2. What shapa is; then `NORTHSTAR` of each wiki in scope (global first) and `FIRES` (ranked, each with its feature, open jobs and tasks and the files and arch boxes their text names), a hint with the top candidates where no fire is set.
3. `BASELINE (stage 0)`: every global-wiki rule and memory (ideas and decisions included) whose `stage:N` is at or under the threshold, in full: id, summary, then the body indented by two spaces, masked, cut at 1,200 characters. `BASELINE SKILLS`: the active version of each skill at or under the threshold: name, description, the body indented and cut to its share.
4. `TOPICS`: the tag index, at most 40 tags by record count, each `tag (rules N, skills N, memories N)` indented, then one line saying to fetch a topic by tag before acting. System tags never appear.
5. `PROJECTS`: up to 12 project keys of the global wiki with title and open tasks, then "The operator's open tasks: N", the five first (rank then oldest), a fixed sentence (answer "what do I need to do" from `tasks`; save what they must do as a `human_task`), and the five oldest open `agent_task` tasks.
6. The global note `session-instructions` if present; the operator style (global note `response-style`); then the write-back format for the client kind and the device's reach: when to write each type and the exact `save` call (`mcp`) or `POST` body (`rest`); a baseline device is shown only what it may write.

Only with `full`: the other global rules (id + summary; a named `wiki` adds its own) and the live features per wiki. Every record text that reaches the text is masked, stripped of control, format and line-separator characters, and one line or indented, so it can never open a section or an instruction block; a baseline record reached through the tag index with 1 MB of body is read as a prefix only. The two operator notes are printed as the operator wrote them, but still masked (secrets out) and stripped of control and format characters, with their line breaks kept; only a delete-scope device token writes them. The baseline and the baseline skills print for every authenticated device, including one limited to wikis that leave out `global`.

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
- Stage 0 and the baseline text are the operator's alone (sync from a delete-scope token); a baseline-reach device reads stage 0 and nothing above it and writes no staged kind.
- Known and accepted limit of reach: ids and `rev` are shared counters, so a baseline device's own new record id or a record's `rev` lets it estimate how many hidden records exist, never what they hold. Nothing else about a hidden record is observable: not an alias (dropped silently), not a skill name (a `skill:` tag is refused and writes nothing), not a project (membership counts only what it can read), not a text (identical text finds no hidden holder).
- Deploy order: the Worker first, then the CLI. The Worker refuses what the new CLI also refuses (one stage marker, lookalikes, `stage:0` outside the global wiki), so an old CLI keeps working against the new Worker, and a new CLI never meets a Worker that accepts what it would drop.
- Reads stay flat as the data grows (`hosted/test/scale.test.ts`, 1,000 then 10,000 records, each read as a full and as a baseline device): the rows the store reads for one call are counted by SQLite and may not grow more than 3x (search 10x), or pass a floor a scan would cross; the call's time (median of batches) is held to the same ratios. Alias lookups carry `alias != ''` so SQLite uses the partial alias index; the session start's focus and note lookups read a few rows however many records exist.
- No secret in the repo: account ids, URLs and keys stay in the deployer's environment and `~/.shapa`.
- Local database schema 2 is additive (`docs`, `tombstones`, counter `D`); a schema-1 reader still works.
- Hooks never block a session and never fail it.
- On a wiki with sync state the after-merge sweep never deletes a memory for disuse: use counts are per device.
