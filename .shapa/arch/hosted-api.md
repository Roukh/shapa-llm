---
id: hosted-api
type: reference
created: "2026-11-12T00:00:00Z"
consequence: 9
locus: output-meta
scope: repo
summary: Hosted REST routes, MCP tools and the composed session instructions - scopes, shapes and the order and budget of the session text.
---

# Hosted: REST, MCP and session instructions

The two surfaces a client without the engine uses, and the session text they open with. Part of [[hosted]].

## Owns

- `hosted/src/api.ts` (REST), `mcp.ts`, `instructions.ts` (the session text), `search.ts`, `ledger.ts`, `wikis.ts`, `app.ts`.

## Interfaces

| Direction | What |
|---|---|
| in | `/v1/*` with a Bearer device or OAuth token; `/mcp` streamable HTTP |
| out | JSON per route; the session text (`GET /v1/instructions`, MCP `session_start`) |

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
| `POST /wikis/{wiki}/sync` | write | [[hosted-sync]] |

Ledger rules mirror `db.py`: a job's parent is a feature, a task's a job; no child under a closed parent; `claimed_by` is the device name. Errors: `{"error": {"code", "message"}}` with 400 `invalid`, 401, 403 `forbidden`, 404, 409 `conflict`, 413, 429.

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
