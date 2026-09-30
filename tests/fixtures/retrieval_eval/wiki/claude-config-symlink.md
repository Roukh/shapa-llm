---
id: claude-config-symlink
type: reference
created: "2026-05-09T18:50:04.444695+00:00"
consequence: 4
locus: meta
uses: 0
summary: CLAUDE_CONFIG_DIR points at roukh-llm/.claude - that repo dir IS the live Claude config, edits apply immediately.
scope: global
status: active
---
# Claude Config Directory Is roukh-llm/.claude

`CLAUDE_CONFIG_DIR` points to `/home/roukh/Projects/workspace/roukh-llm/.claude` (exported in `~/.bashrc_custom`). That repo directory IS the live Claude config — editing `.claude/` in the repo immediately affects the running config. Never treat them as separate trees. Runtime state (sessions, cache, history) under it is gitignored.

Source: memri cc09a7de-9368-4b05-9b10-0074ef54bf38
