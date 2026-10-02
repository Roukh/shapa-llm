---
id: shapa-backend-spec
type: reference
created: "2026-09-30T02:00:00Z"
consequence: 9
locus: output-meta
scope: repo
summary: Index of shapa's multi-root backend spec - target architecture, install, upgrades, open risks; links the part refs, memory v3 and the decision log.
---

Final architecture for shapa's multi-root memory backend: global+repo dual-wiki loading, semantic search/RAG, and context-optimized note structure, so `pip install shapa` fully operates for any user on any harness. Synthesized from a 3-design tournament (winner: design 0, total 6.6) plus grafts from designs 1/2 and fixes for every fatal flaw the judges found.

## Parts

This index stays under the 2,000-word reference cap by splitting the spec into linked refs. Section numbers (§N) used across them are the original spec's.

- §4 resolution, loading tiers, per-prompt fetch: [[shapa-wiki-resolution]]
- §5 sqlite index, ranking, daemon, perf budgets: [[shapa-index-backend]]
- §6 note schema v2 and validator codes: [[shapa-note-schema-v2]]
- §7 what moved out of roukh-llm, Codex/OpenCode surface: [[shapa-roukh-llm-consumer]]
- §10 operator decisions (taken and open): [[shapa-operator-decisions]]
- Memory format v3 (records log, capture, fused recall, no-answer floor): [[shapa-memory-v3]]
- Memory v3 lab grid and measured results: [[shapa-memory-v3-results]]

The original single-file spec, including the 2026-09-30 empirical probe (§1), the implementation slices (§8) and the decision options as first posed (§10), is archived verbatim at `archive/shapa-backend-spec-2026-09-30.md`.

## 2. Target architecture

```
                shapa/store.py — one sqlite file per wiki root (<root>/.shapa-index.db)
                notes table + FTS5 virtual table (when available) + JSON-sidecar vectors
                              ▲                          ▲
                              │ read (merged across roots)│ write (single, gated root)
        ┌─────────────────────┴──────┐          ┌──────────┴─────────────┐
        │ config.wiki_roots(start)    │          │ capture.py / shapa save │
        │ → [repo?, global]           │          │ --scope required        │
        └─────────────────────────────┘          └─────────────────────────┘
                │            │            │
   ┌────────────┴──┐  ┌──────┴───────┐  ┌─┴──────────────────────────┐
   │ Claude Code    │  │ shapa CLI     │  │ MCP stdio server (shapa mcp)│
   │ hooks:         │  │ (manual /     │  │ search / get / save /       │
   │ SessionStart → │  │  scripted)    │  │ placement — vendor-neutral   │
   │  bootstrap     │  └──────────────┘  │ surface for Codex/OpenCode/  │
   │ UserPromptSubmit│                    │ anything that speaks MCP    │
   │  → fetch        │                    └──────────────────────────────┘
   │ Stop/SubagentStop → capture, maintain --prune
   └─────────────────┘
```

Reads always fan out across every wiki root in scope ([[shapa-wiki-resolution]]). Writes stay single-target and explicitly scope-gated — automatic multi-root merge must never silently decide where a *new* note lands. Hooks remain the always-on Claude-Code-specific path; MCP is the one on-demand, harness-agnostic path every consumer (including Codex/OpenCode) can reach.

## 3. Install experience

```
pip install shapa                 # core, pure stdlib, BM25-only — unchanged default
pip install shapa[semantic]       # + model2vec embeddings ([embeddings] kept as a
                                   #   deprecated alias for one release)
pip install shapa[mcp]             # + official `mcp` SDK for the stdio server; falls back
                                   #   to a stdlib-only JSON-RPC shim when absent
```

`bootstrap.sh` (the curl one-liner, the install path most users hit) asks whether to add `[semantic]` and `[mcp]` (operator decision 3). With no TTY it installs the core only and prints the exact command to add the extras; `--with-semantic`, `--with-mcp` and `--full` skip the prompt. It also connects the **global** default wiki even when cwd has its own repo-local wiki, so a fresh machine never resolves to a global path that doesn't exist on disk. README states plainly: `pip install shapa` alone is keyword-only; `[semantic]` is what turns on real embeddings.

`install.sh` wires:
- Hooks: `SessionStart → shapa bootstrap`, `UserPromptSubmit → shapa fetch`, `Stop`/`SubagentStop → capture` then `maintain --prune`.
- `--mcp` registers `shapa mcp` (project-scoped `.mcp.json` for Claude Code).
- `--harness claude|codex|opencode|all` (default `claude`) routes hook/MCP registration to the right config surface ([[shapa-roukh-llm-consumer]]).
- The `shapa-upgrade` skill for every harness in scope, then `upgrade --all --check`.
- `--no-embeddings` / `--no-mcp` opt back out; `--uninstall` and the idempotent `jq` guard are unchanged.

## 11. Upgrades: every wiki to the current format

Operator directive (2026-10-01): every shapa update sends agents to bring each wiki to the current format.

- **Format.** `CURRENT_FORMAT` in `shapa/registry.py`; a release bumps it whenever a conformant wiki changes (schema, lean shape, shipped `AGENTS.md`). Format 2 = schema v2 + lean shape (decisions 6/7).
- **Marker.** `<wiki>/.shapa-format` holds one integer and is tracked, not a cache. Missing means format 1. `shapa upgrade` writes it only once nothing is left to fix, and never lowers it.
- **Registry.** `~/.shapa/wikis.json` (`$SHAPA_REGISTRY` overrides) lists every wiki seen by `init`, `bootstrap`, `fetch` or `upgrade`. Writes hold an `flock` and land via temp file + `os.replace`. `upgrade --all` (apply) drops paths gone from disk.
- **`shapa init`.** An empty folder is scaffolded at the current format. A named folder of notes without `AGENTS.md` is adopted: the missing scaffold files are added, the notes stay byte-identical, and it stays at format 1 for `upgrade`. Only `--global` writes `~/.shapa/config.json`.
- **`shapa upgrade [PATH|--all] [--check] [--json]`.** Applies the mechanical steps: refresh `AGENTS.md`/`placement.md`, add cache entries to `.gitignore`, move `uses`/`last_used` into the store (max/latest wins) and strip them, derive `id`/`scope`. `--check` lists every counter line it would delete, and `steps` (JSON) marks that deletion as part of the upgrade commit, never to be restored. Reports the judgment items per wiki: F10/F11, F07, F04 on memory/rule/issue, duplicate ids, lexical near-duplicates at the merge threshold, invalid frontmatter, F06, and notes outside root/`arch/`. It is idempotent and exits 1 while any wiki is behind (marker, pending step, or work left). A wiki marked newer than the installed shapa is never touched.
- **Prompts.** SessionStart appends one line when a wiki in scope has a marker behind. `install.sh`/`bootstrap.sh` end with `upgrade --all --check`.
- **Skill.** `shapa/assets/skills/shapa-upgrade/SKILL.md` is installed per harness by `install.sh`. It gives one agent per behind wiki, which resolves the work list and commits in that repo, and it ends on a green check.

## Acceptance and open risks

A change to a hook path (bootstrap, fetch, store/daemon, MCP) counts as done only after one non-synthetic check: an actual Claude Code session (real or scratch `settings.json`, e.g. `claude -p --settings`) fires the hook and shows the expected merged output. Green unit tests alone are not acceptance.

- **Codex/OpenCode parity is MCP-tool-level, not hook-level.** The Codex `--format=agents-md` path and an OpenCode plugin are only manually verifiable against those CLIs.
- **`maintain --backfill` summary drafting needs the `claude` CLI.** Without it, missing summaries surface as F04 for an agent or the MCP `save` tool to fill.
- **Daemon staleness check is a stat per request.** Fine at the §5 budgets; re-measure if corpora grow 10x.
- **RRF and the confidence floor** change ranking on real wikis in ways measured only against fixtures until a manual pass over the probe's 8-prompt-per-repo set.
