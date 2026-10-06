---
id: index
type: reference
created: "2026-10-05T20:22:00Z"
consequence: 7
locus: output
scope: repo
summary: shapa-llm as a diagram - five boxes (read path, write path, database, CLI and hooks, installer) and how they call each other.
---

# Architecture index

> For the agent, not for humans: rows, not prose. One file per box.

## Boxes

| Box | File | Owns | Purpose |
|---|---|---|---|
| read path | [[read-path]] | `shapa/bootstrap.py`, `fetch.py`, `rank.py`, `bm25.py`, `embed.py`, `get.py`, `store.py`, `serve.py` | surfaces relevant rows/notes at session start and per prompt |
| write path | [[write-path]] | `shapa/capture.py`, `redact.py`, `save.py`, `memlog.py` | distils and records new M/I rows from hooks and explicit saves |
| database | [[database]] | `shapa/db.py`, `ledger.py`, `migrate4.py` | the one `shapa.db` ledger (F/J/T work items) and MRI rows |
| CLI and hooks | [[cli-and-hooks]] | `shapa/cli.py`, `maintain.py`, `heartbeat.py`, `score.py`, `validate.py`, `upgrade.py`, `mcp.py`, `frontmatter.py`, `nodes.py`, `status.py` | the `shapa` command, hook dispatch, maintenance, and the MCP surface |
| installer | [[installer]] | `install.sh`, `bootstrap.sh`, `shapa/assets/` | wires a fresh machine's harness and wiki to the engine |

## Edges

| From | To | What crosses |
|---|---|---|
| installer | cli-and-hooks | hook registrations (SessionStart/UserPromptSubmit/Stop/SubagentStop/PostToolUse) that call `shapa` subcommands |
| cli-and-hooks | read-path | `bootstrap`/`fetch`/`get`/MCP `search` dispatch |
| cli-and-hooks | write-path | `capture`/`save`/`correction` dispatch |
| cli-and-hooks | database | `ledger`/`row`/`upgrade`/`validate` dispatch |
| read-path | database | ranks ledger items and M/R/I rows together with any remaining note files |
| write-path | database | new M/I rows (`db.add_row`) |
| database | read-path | rows backing the derived, gitignored `.shapa-index.db` |
