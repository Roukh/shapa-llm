---
id: hosted-documents
type: reference
created: "2026-11-12T00:00:00Z"
consequence: 9
locus: output-meta
scope: repo
summary: Hosted focus, skill versions and project documents - documents told apart by tags, the lean gate, activation by the delete scope, lessons.
---

# Hosted: focus, skills and project documents

Three things that are documents (`D`) told apart by tags, implemented alike in Python and the Worker. Part of [[hosted]].

## Owns

- Worker: `hosted/src/focus.ts`, `skills.ts`, `skillmatch.ts`, `projects.ts` (the lean gate), `projectfile.ts` (template, sections, items), `projectapi.ts`.
- Engine: `shapa/focus.py`, `shapa/skills.py`, `shapa/skillmirror.py`, `shapa/projectfile.py`, `db.add_doc` / `db.merge_docs`.

## Interfaces

| Direction | What |
|---|---|
| in | REST focus, skills and projects routes, MCP `focus`, `focus_set`, `skills`, `skill`, `project_*` ([[hosted-api]]); sync puts of documents ([[hosted-sync]]) |
| out | the focus (fires) in the session text, skills through `skills`, `skill` and `skill_match` ([[hosted-api]]) |

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
- Skill FILES are the source of truth: `shapa skill mirror` (`shapa/skillmirror.py`) writes the operator's library (`skills/<category>/<name>/SKILL.md`, beside the global wiki) and a repo's `.shapa/skills/<name>/SKILL.md` as these versions (a version's `source` is `mirror:<hash>`), and they reach this store as ordinary record syncs (a repo's skill files are never notes). Every ACTIVE version is readable by every device, a baseline one included (`baseVisible`; a store older than this rule rebuilds its reach column once, meta key `skill_access_v1`); a pending or previous one only by a full device. The MCP tool `skill_match` {text, wiki?} (`skillmatch.ts`: BM25 over name counted twice and description, the engine's tokenizer, at most 6, each with name, description, wiki and a `matches:` reason; the global skills plus the named wiki's, which hides a global skill of the same name) is open to every device.
- A skill version's body is never in the session text, on either side: an agent reads it with `skill` (a baseline device reads every ACTIVE version, never a pending or previous one), and `skill_match` names at most 6 active skills for a task.
