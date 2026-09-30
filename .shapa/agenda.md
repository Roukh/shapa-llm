---
id: agenda
type: memory
created: "2026-09-30T06:00:00Z"
consequence: 8
locus: meta
uses: 0
summary: Top 3 fires for shapa-llm — finish the backend build, keep reads from writing notes, ship an installer a stranger can run.
scope: repo
status: active
---

# Agenda: top 3 fires

1. **Finish the backend build** on `feat/shapa-backend`. Global and repo wikis load at startup, more notes are fetched per prompt, and search combines keyword and semantic ranking. Also covered: the MCP server, note format v2, and the lean-wiki lint. The spec is [[shapa-backend-spec]].
2. **Reads must never write notes.** Today `record_use` rewrites a note's `uses`/`last_used` counters on every fetch, so repos accumulate counter-only diffs (26 files in open-trader). The counters move to the index store (spec decision 7).
3. **An installer a stranger can run.** `bootstrap.sh` asks whether to add the semantic and MCP extras, and installs the core only when there's no terminal. It wires the startup and per-prompt hooks. It's verified by a live headless Claude Code session, not only unit tests.
