---
id: hosted-sync
type: reference
created: "2026-11-12T00:00:00Z"
consequence: 9
locus: output-meta
scope: repo
summary: Hosted wire record and sync protocol - record fields and limits, the server's op rules, the client's snapshot/classify/send/apply steps and commands.
---

# Hosted: records and sync

The wire record every surface exchanges, and the two-way sync of a local wiki with the store. Part of [[hosted]].

## Owns

- `hosted/src/records.ts` (zod wire schemas, the single source of the wire types and of `/openapi.json`), `sync.ts`, `repo.ts`, `store.ts` (the sync transaction).
- `shapa/remote.py`, `remotesync.py` (wiki keys, scope, the hook mode), `remotehttp.py`, `sync.py` (snapshot, classify, exchange), `syncapply.py` (the one-transaction apply), `syncrec.py` (validation of server input, note file safety), `syncstate.py` (credentials, per-wiki state and lock), `dbsync.py` (explicit-id upserts, relabel).

## Interfaces

| Direction | What |
|---|---|
| in | `POST /v1/wikis/{wiki}/sync` from `shapa remote sync` (CLI, or `--hook` at SessionStart/SessionEnd) |
| out | `changes` pages applied to the local `shapa.db` in one write transaction |

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

## Sync

Request `{cursor, ops, want, counters}`; response `{cursor, results, changes, counters, more}`.

| Op | Shape |
|---|---|
| put | `{op: "put", base_rev: int\|null, record}` (`null` = new to the server as far as the client knows) |
| del | `{op: "del", id, uid, deleted_at, base_rev: int\|null}` (needs scope `delete`, except a note, `uid` empty, whose last writer is this device) |

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
- A put is also `rejected` (checked before anything else) for: a tag list a rule of [[hosted-tags]] refuses (a lookalike of a system tag, two stage markers, `stage:0` or `stage-proposed:0` in a repo wiki, a bad project tag, a task with both or neither owner tag in the global wiki), and a note named `session-instructions` or `response-style` in the global wiki from a device without the delete scope.
- `changes`: every record with `rev` > request `cursor`, oldest first, plus the current record for each rejected op and each `want` id. A page holds at most 500 records and 8 MB; `more: true` means call again with the returned cursor and no ops.
- `counters`: next number per kind; each side keeps the maximum. The server raises its own to the request's before applying any op, so one collision never shifts the labels after it.

Client (`shapa remote sync`), per wiki, under a file lock:

1. Snapshot local records. State (`sync/<sha1 of db path>.json` beside the credentials) holds `{key, cursor, base: {id: {rev, uid, updated, hash}}}`; `hash` is local-only.
2. Classify against `base`: absent from base = new (put, `base_rev` null); hash differs = modified (put, `base_rev`); in base but gone locally = deleted only when `tombstones` holds it (del), else missing (`want`). Every tombstone is sent, in base or not (`base_rev` null), until the server answers for it. The database carries a sync token (`meta.sync_token`), rotated in the apply transaction of every sync that pushed or applied anything and kept in the state. Token differs = the database was swapped (git): a differing record whose local `updated` is not newer than base's is stale (`want`), so the swap heals from the server. Token equal: clocks are never consulted.
3. Send (parents first: F, J, T, then the rest), in batches.
4. In one local write transaction: apply `renamed` (relabel id, child `parent`, tags, links; foreign keys deferred), then each change, skipping any record whose local content changed since step 1; raise counters; drop acknowledged tombstones; write state.

Note files sync only while the wiki's primary checkout is on its default branch (always, outside git). A remote delete removes a local note only if it is unchanged since the last sync; a note another device wrote and this one deleted locally comes back (delete it through the API); a note this device wrote stays deleted: the base marks a note this device put (`own`, cleared when anyone else's edit replaces it), and a marked note gone from the wiki is sent as a `del` (no uid, found by path), which the server accepts without the `delete` scope when that device is the note's last writer. Remote text is never executed and never picks a path outside the note pattern.

## Client commands

| Command | Does |
|---|---|
| `shapa remote login URL [--device NAME] [--scopes ...] [--token-stdin]` | owner key from `$SHAPA_OWNER_KEY` or a prompt, never argv; writes `remote.json` (`url, device, device_id`) and `remote.token` (0600) under `~/.shapa` (`$SHAPA_REMOTE_DIR` overrides) |
| `shapa remote sync [--cwd DIR] [--all] [--key KEY] [--hook] [--if-changed] [--dry-run] [--json]` | wikis in scope (repo + global), or every registered one; `--hook` reads hook stdin, stays within 8 s, always exits 0, prints at most one line; `--if-changed` skips the network when nothing local changed |
| `shapa remote status [--check] [--json]` | login, last sync and pending changes per wiki; `--check` exits 1 when not logged in or unreachable |
| `shapa remote devices [--revoke ID]`, `logout` | device admin (owner key), forget credentials |
| `shapa doc add\|edit\|rm\|list\|merge` | documents; `shapa get D<n>`; MCP `save` gains `document` and `idea`; `add` takes `--project` and `--merge` |
| `shapa focus ...`, `shapa skill ...` | the northstar and fires, and skills (see [[hosted-documents]]); both sync as documents, with no command of their own |
