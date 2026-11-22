---
id: hosted-tags
type: reference
created: "2026-11-12T00:00:00Z"
consequence: 9
locus: output-meta
scope: repo
summary: Hosted tags, stages and relations - system tags, stage 0 as the operator's, the baseline budget, the tag index, projects, tasks, learning-loop sync.
---

# Hosted: tags, stages and relations

Tags are the relations: stages, projects, owners and the learning loop's counters are tags, gated on every write path. Part of [[hosted]].

## Owns

- `hosted/src/tags.ts` (system tags, stage tags, buckets, the one rule for what a baseline device sees), `gates.ts` (tag and kind rules, the stage-0 gate, the baseline budget), `relations.ts` (the relation index and its reads), `tagapi.ts` (tag index, related), `tasks.ts` (the task list).
- Engine counterparts: `db._guard_tags`, `db.SYSTEM_TAGS`, `shapa/stages.py`, `tasks.py`, `relate.py`; `tests/test_parity.py` holds the lists equal.

## Interfaces

| Direction | What |
|---|---|
| in | every create, PATCH, MCP `save`/`update` and sync put passes the tag gates |
| out | `GET /v1/tags`, `related`, `tasks`, `projects` and the MCP tools of the same names (the session text prints none of them) |

## Tags, stages and relations

Tags are the relations. No new kind and no breaking schema change: `record_tags`, `record_links`, `tag_counts` and two columns (`records.base_vis`, `devices.reach`) are additive, written by `Repo.save` (so REST, MCP, sync, tombstones and the lesson filer all reach them) and rebuilt once (`meta` key `relations_v1`, safe to run twice) for rows stored before them.

| What | Rule |
|---|---|
| Stage tag | `stage:N`, N 0-9, on a rule, a memory or a skill version only; no other `stage` or `stage-proposed` spelling is accepted. `stage-proposed:0` is the only proposal tag. One stage marker per record: at most one of `stage:N` or `stage-proposed:0`, never both, on create, PATCH, MCP, a sync put and the one-time relabel (`relabelStage`: a legacy `stage:0` becomes `stage-proposed:0` in the global wiki, is dropped in a repo wiki, and is dropped beside a real `stage:1`-`stage:9`). `stage:0` and `stage-proposed:0` exist only in the global wiki (the baseline is the global wiki's); a repo wiki takes `stage:1`-`stage:9`. Checked on REST create and PATCH, MCP `save` and `update` (same code) and every sync put |
| Lookalikes | a tag that becomes a system tag (a name of `SYSTEM_TAGS`, or anything under a `SYSTEM_PREFIXES` prefix: `stage:`, `stage-proposed:`, `skill:`, `v:`, `status:`, `rank:`, `project:`, `repo:`) after NFKC folding, lower-casing and removal of spaces, control, format (zero-width, bidi) and variation-selector characters, but is not byte-equal to it, is refused (400, a sync put `rejected`) on every path, for every system tag: `Human_Task`, `agent` + a zero-width space + `_task`, `IDEA`, `Project:x`. One list (`tags.ts`), so the owner tags cannot be forged to drop the operator's task on a pull |
| Stage 0 is the operator's | only a sync put from a device TOKEN (never OAuth) holding the `delete` scope, with reach `full`, may add or remove `stage:0` or edit the text or tags of a record that carries it. From anyone else: a new record or a retag carrying `stage:0` is stored with `stage-proposed:0` (accepted; REST sets `X-Shapa-Notice`, MCP says so, a sync result carries `note`), and an edit of a stored stage-0 record is refused (REST 403, MCP error, sync `rejected`: "changes only from a device token with the delete scope"), with or without `base_rev`, by id or by uid. REST PATCH and MCP `update` never set stage 0, whatever the token. A stage:0 tag already stored when stages arrived becomes `stage-proposed:0` in the one-time backfill |
| Baseline budget | the stage-0 set of the global wiki (a rule or memory: its one-line summary, the only text a session start prints; an ACTIVE skill version: its description, a pending or previous one costs nothing because the session text never prints it) is at most 6,000 characters (`BASELINE_BUDGET`); a put past it is `rejected` naming the current size. A repo wiki has no baseline (it refuses `stage:0`) |
| Identical text | saving text a record already holds (a rule, memory, issue or open task) never changes that record's tags, stage, project, links or device unless the writer could have made that edit directly: the merge goes through the gates of a PATCH (tag rules, a stored `stage:0` record, reach, rank tags, stage gate). A writer that may not edit the holder gets the holder back unchanged (200) when it can read it, and a new separate record when it cannot (a baseline device never finds a hidden holder, so nothing reveals it). Tags that would make the holder invalid (two stages, two projects) are a 400, as an edit would be. A lesson is filed only from a non-baseline writer |
| System tags | `stage:*`, `stage-proposed:*`, `skill`, `skill:*`, `v:*`, `status:*`, `rank:*`, `focus`, `project:*`, `repo:*` (the repo an `agent_task` is addressed to; the local engine acts on it, the Worker only keeps it), `human_task`, `agent_task`, `idea`, `decision` (`tags.ts`: never a topic, never in the tag index unless `system=1`) |
| Project tag | `project:KEY` (`cleanProject`), at most one per record. A task, memory (ideas and decisions included), rule, issue or skill version carrying it also gets an `about` link to the project's document in the same wiki: set on create, moved or removed on retag, added on a sync put, and added to the records already waiting when the document is created later (at most 500 per document, oldest first). Never for the document itself, a feature or a job. `project` is accepted on an issue (REST create, MCP `save`). A project's file created by a baseline device links only the records it can read and edit (tasks, ideas, decisions); the rest are linked when a full device next writes them, and project reads resolve membership by tag, so they stay complete |
| Owner tags | `human_task` / `agent_task` only on a task, never both. The global wiki accepts kind `T` only, standalone, with exactly one of them: `F` and `J` are refused there (REST 400, sync `rejected`) |
| Tag index | `record_tags(wiki, tag, id, kind, open, vis, rank, seq, rev)` (indexes `(tag, wiki)`, `(wiki, tag, kind, open, rev)`, `(wiki, tag, rev)`, `(tag, kind, open, rank, seq)`; `GET /v1/wikis/{wiki}/records?tag=` lists through it, ordered by `rev`, never by a scan of every record's tags), `record_links(wiki, src, dst, rel)` (index `(wiki, dst)`), `tag_counts(wiki, tag, bucket, vis, n)`. Buckets: R rule, M memory, E idea, C decision, I issue, S skill version, D document, T task, J job, F feature, N note; O counts open tasks. Reads walk these, so what they cost does not grow with the record count: `GET /v1/tags` reads `tag_counts` (the work is the number of distinct tags), `tasks` and `projects` walk the owner-tag index, `related` reads at most 400 neighbours per tag and follows at most 12 of a record's tags |
| Related order | records linked to it (either direction), then by number of shared non-system tags, then newest; default 10, at most 50 |
| Search by tag | `GET /v1/search?q=&tags=a,b` (`q` optional with tags; every tag must match; at most 8); on a score tie (scores within a tenth) the lower stage ranks first |
| Projects | a project is `project:KEY`; its one document (the lean gate) starts from a template held in `projectfile.ts` (`PROJECT_TEMPLATE`, byte for byte as the contract's, ending with a newline). Section keys `issues questions implementations problem solution diff advantages villains loop_market loop_product`. An item is one line, masked, at most 500 characters, invisible characters removed; it fills the empty `1. ` first, is a no-op when the section already holds the text, creates a missing heading at its template position, and keeps the file under 1 MB. A wiki holds at most 1,000 projects |
| Task list | `GET /v1/tasks`: `human_task` (or `agent_task`) tasks of every readable wiki, `rank:N` (1-999) first then oldest, at most 100 with `total`. Identical open text (same owner tag, title, body, no parent) in a wiki is stored once |

## The learning loop: what syncs and what stays on the device

Nothing in the loop is a record kind, a link relation or a Worker change; it is ordinary rows, tags and bodies, so a sync put and the Worker's REST and MCP treat it as any rule or issue.

- **Syncs** (plain records): a principle is an `R` row tagged `principle` (statement = summary; body = optional `Why:` and `Also:` lines, then `Evidence:` lines `- I12 repo-key date: text`; counters in tags `violations:N`, `revisions:N`, `evidence:N`, `last-seen:ISO`, `serves:REPO-KEY/F3|northstar|none`, and `churn`); a learned issue carries `learned`, `learned-at:ISO`, `generalized-into:REF`, `serves:...`; a repeat carries `repeat`, `repeat-of:REF` (`global:` prefixes the other wiki's row), `violation`, `blocked`; `related` and `conflicts` links; a feature that was ever a fire carries the item tag `fire`. Tags are at most 80 characters and 100 per record, bodies far below the record limit (evidence keeps the newest 12, wordings 4). `REF` is a label: when the sync relabels a colliding record, tags that name the old label are not rewritten, so after such a collision a `repeat-of` or `generalized-into` can name another row. That is a known gap: it skews a count, never a principle's text, and never activates anything.
- **Skill versions** a principle files (a rule tagged `skill:NAME`) are the documents of [[hosted-documents]], with its delete-scope activation rule untouched. `shapa learn` never activates one: it files a pending version and only `shapa skill activate` makes it live, on this device or any other. A principle that syncs in keeps its tags as data; the receiving device ranks it by its text alone until it writes the principle itself (`p_local`).
- **Never synced, per device**: raw metric events, fetch and bootstrap latency, skill-use and served counts, which principles this device wrote itself (`p_local`), the similarity index and the cross-repo mirror (`l_chunks`, `l_tok`, `l_vec`), events and serve counts (`m_events`, `p_served`), all in the derived index, and the daily snapshots (`metrics_daily` in the committed database file, which the sync never reads). The Worker has no `learn` or `metrics` tool and route; `shapa learn` and `shapa metrics` are CLI only.
- **Client heals what the Worker does not write**: the Worker's focus set does not tag a feature `fire`, so the client reads the focus record's current fires as well as the tag, and tags them when it archives (`metrics.fire_ids`).
