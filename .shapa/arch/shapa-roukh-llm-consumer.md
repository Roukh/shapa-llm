---
id: shapa-roukh-llm-consumer
type: reference
created: "2026-09-30T02:00:00Z"
consequence: 7
locus: output-meta
scope: repo
summary: shapa spec §7 - what moved from roukh-llm into shapa, roukh-llm's thin-consumer end state, and the MCP-based Codex/OpenCode surface.
---

Which memory code moved out of roukh-llm into shapa, what roukh-llm keeps, and how non-Claude harnesses reach shapa. Part of [[shapa-backend-spec]] (§7).

## 7. What moves out of roukh-llm

**Moves into shapa-llm** (generalized off roukh-llm's `WORKSPACE_REPOS`/4-repo hardcoding):
- `resolve_global_shapa()`/`resolve_repo_shapa()`/`note_files()` → `config.wiki_roots()` ([[shapa-wiki-resolution]]).
- `bootstrap()` → `shapa/bootstrap.py` (Tier 1), index-only/summary-based with `score.py`'s richer value formula instead of a bare `(-consequence,-recency)` sort.
- `do_search()`/`rank_search()` → `shapa fetch --query` / `shapa search` (Tier 2/3), hybrid-ranked ([[shapa-index-backend]]) and MCP-exposed, not a manual-invocation-only side door.
- `save`'s `--global`/`--repo` gate → `capture.py --scope` / MCP `save` tool / `shapa/assets/placement.md`, parameterized generically (no repo allowlist).

**Stays in roukh-llm**: `guard.py`'s destructive-git blocking and `.claude/`-write-scope enforcement (general session hardening, unrelated to memory); the legacy Supabase `projects`/`inspect`/`export` subcommands (rename the file — no longer about `.shapa` at all). Brain-DB coherence is never shapa core: shapa keeps zero DB/infra coupling.

**roukh-llm's thin-consumer end state** — executed in roukh-llm, tracked here:
1. `.claude/settings.json`'s `SessionStart: memory.py bootstrap` → `shapa bootstrap`; add `UserPromptSubmit → shapa fetch`.
2. `Stop`/`SubagentStop` → `shapa capture` + `shapa maintain --prune`.
3. `skills/workflow/memory/SKILL.md` + `skills/hermes/brain-tools/SKILL.md` repointed to `shapa search`/the MCP tool.
4. roukh-llm's duplicated `.shapa/AGENTS.md` copy replaced by the shipped one via `shapa upgrade` / `shapa init --upgrade-docs` (it had already drifted).
5. Retire the memri-backed OLD hook (`roukh-llm/scripts/runtime/memory.py`) — the fix for the cross-project leakage the 2026-09-30 probe found live and severe (archived spec §1). Timing: operator decision 1 ([[shapa-operator-decisions]]).

## 7.1 Codex/OpenCode — not fully deferred

The spec ships a minimal real surface, not just a plan:

- **MCP stdio server** (`shapa/mcp.py`) — `search`/`get`/`save`/`placement` tools, stdlib JSON-RPC fallback when the `mcp` SDK extra isn't installed. This is the vendor-neutral mechanism both Codex and OpenCode can register today (Basic Memory's own pitch — "works across Claude, Codex, Cursor, ChatGPT, and any MCP client" — is direct precedent).
- **Codex**: no per-turn hook exists, but Codex natively ingests `AGENTS.md` at session start — its Tier 1 for free. `shapa bootstrap --format=agents-md` regenerates a static context block a repo's own `AGENTS.md` can `@`-include, refreshed on demand. Tier 2/3 come from the MCP server.
- **OpenCode**: registers the same MCP server in its config for Tier 2/3. A plugin calling `shapa fetch` on OpenCode's prompt-submit-equivalent event is a documented follow-up (its plugin API is less stable than MCP registration).
- `install.sh --harness codex|opencode|all` routes registration to the right config file (`~/.codex/config.toml`'s `[mcp_servers.shapa]`, OpenCode's own config) alongside the Claude Code default.
