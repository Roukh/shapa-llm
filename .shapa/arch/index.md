---
id: index
type: reference
created: "2026-10-05T20:22:00Z"
consequence: 7
locus: output
scope: repo
summary: shapa-llm as a diagram - ten boxes (read path, write path, database, CLI and hooks, installer, the hosted store in five parts) and the edges between them.
---

# Architecture index

> For the agent, not for humans: rows, not prose. One file per box.

## Boxes

| Box | File | Owns | Purpose |
|---|---|---|---|
| read path | [[read-path]] | `shapa/bootstrap.py`, `fetch.py`, `rank.py`, `bm25.py`, `embed.py`, `get.py`, `store.py`, `serve.py`, `codemap.py`, `partition.py` | surfaces relevant rows/notes at session start and per prompt, and a fire's code partition from the derived code map |
| write path | [[write-path]] | `shapa/capture.py`, `redact.py`, `save.py`, `memlog.py`, `learn.py`; `ledger.py`'s correction hook | distils and records new M/I rows from hooks and explicit saves; turns corrections into principles |
| database | [[database]] | `shapa/db.py`, `ledger.py`, `migrate4.py`, `docs.py`, `commit.py`, `focus.py`, `skills.py`, `skillmirror.py`, `stages.py`, `tasks.py`, `projectfile.py`, `relate.py` | the one `shapa.db`: F/J/T work ledger, M/R/I rows, documents, tombstones; commits itself on the default branch |
| CLI and hooks | [[cli-and-hooks]] | `shapa/cli.py`, `config.py`, `registry.py`, `githooks.py`, `maintain.py`, `heartbeat.py`, `score.py`, `validate.py`, `upgrade.py`, `reconfigure.py`, `mcp.py`, `hierarchy.py`, `frontmatter.py`, `nodes.py`, `status.py`, `metrics.py` | the `shapa` command, wiki resolution, hook dispatch, maintenance, metrics and the local MCP surface |
| installer | [[installer]] | `install.sh`, `bootstrap.sh`, `shapa/assets/` | wires a fresh machine's harness and wiki to the engine |
| hosted | [[hosted]] | `hosted/src/index.ts`, `oauth.ts`, `consent.ts`, `auth.ts`, `devices.ts`, `reach.ts`, `store.ts` | the opt-in Cloudflare Worker: wikis, auth, reach, invariants |
| hosted sync | [[hosted-sync]] | `hosted/src/records.ts`, `sync.ts`; `shapa/remote.py`, `remotesync.py`, `remotehttp.py`, `sync.py`, `syncapply.py`, `syncrec.py`, `syncstate.py`, `dbsync.py` | the wire record and the two-way sync of every wiki |
| hosted tags | [[hosted-tags]] | `hosted/src/tags.ts`, `gates.ts`, `relations.ts`, `tagapi.ts`, `tasks.ts` | system tags, stages, the baseline gate, the tag index, projects and tasks, what of the learning loop syncs |
| hosted documents | [[hosted-documents]] | `hosted/src/focus.ts`, `skills.ts`, `skillmatch.ts`, `projects.ts`, `projectfile.ts`, `projectapi.ts` | focus, skill versions and project documents (documents told apart by tags) |
| hosted API | [[hosted-api]] | `hosted/src/api.ts`, `mcp.ts`, `instructions.ts`, `search.ts`, `ledger.ts`, `wikis.ts` | REST, remote MCP and the composed session instructions |

## Edges

| From | To | What crosses |
|---|---|---|
| installer | cli-and-hooks | hook registrations (SessionStart/UserPromptSubmit/Stop/SubagentStop/PostToolUse/SessionEnd) that call `shapa` subcommands |
| cli-and-hooks | read-path | `bootstrap`/`fetch`/`get`/`map`/`partition`/MCP `search` dispatch |
| cli-and-hooks | write-path | `capture`/`save`/`correction`/`learn` dispatch |
| cli-and-hooks | database | `ledger`/`row`/`doc`/`focus`/`skill`/`commit`/`upgrade`/`validate` dispatch |
| read-path | database | ranks ledger items and M/R/I rows together with any remaining note files |
| write-path | database | new M/I rows (`db.add_row`), principles (rule rows tagged `principle`) |
| database | read-path | rows backing the derived, gitignored `.shapa-index.db`; items and rows read by `partition.py` for a fire's text |
| cli-and-hooks | hosted-sync | `shapa remote` dispatch; `shapa remote sync --hook` at session start, stop and end, where a harness installer wires it (shapa's own `install.sh` does not: J112) |
| hosted-sync | database | pulled records written through explicit-id upserts; local changes and tombstones read for the push |
| hosted-api | hosted | every route and tool runs through `ShapaStore.act` with the caller's reach view |
| hosted-api | hosted-tags | every create, PATCH and MCP write passes the tag and stage gates |
| hosted-api | hosted-documents | focus, skills and project routes and tools |
| hosted-sync | hosted-tags | every sync put passes the same tag and stage gates |
