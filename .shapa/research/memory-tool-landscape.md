---
id: memory-tool-landscape
type: reference
created: "2026-10-06T21:30:00Z"
consequence: 7
locus: output-meta
scope: repo
summary: 16 memory tools and 6 vendor-native memory features compared (stars verified 2026-10-06), the white space, a repo-polish checklist.
---

# Memory tool landscape (research 2026-10-06)

Web research round; stars from the GitHub API on 2026-10-06. Benchmark numbers quoted by vendors are self-reported and publicly disputed (mem0 vs Zep on LoCoMo). Problem side: [[agent-memory-problems]], [[agent-work-problems]]. Plan: [[public-release-plan]].

## OSS and commercial tools

| Tool | Stars | License, lang | Storage | Install | Recall / standout | Top complaints |
|---|---|---|---|---|---|---|
| claude-mem | 97,181 (repo created 2025-08) | Apache-2.0, TS | SQLite plus Chroma | `npx claude-mem install` or Claude Code `/plugin` | stop-time AI compression injected at next start; 3-step search skill; claims 7 harnesses | fragile background worker: https://github.com/thedotmack/claude-mem/issues/3606 , /3607 , /3609 |
| mem0 + OpenMemory MCP | 66,697 | Apache-2.0, Py | vector DB plus graph | `pip install mem0ai` | multi-level extraction, hybrid fusion; claims LoCoMo 92.5, LongMemEval 94.4 | time-blind conflicts https://github.com/mem0ai/mem0/issues/5352 , silent loss /5245 , add-only stale facts /4956 |
| graphiti / Zep | 31,496 / ~5k | Apache-2.0, Py | Neo4j/FalkorDB temporal graph | `pip install graphiti-core` | facts carry validity windows | quickstart breakage https://github.com/getzep/graphiti/issues/868 |
| cognee | ~31.5k | Apache-2.0, Py | graph plus vectors, local GLiNER | `uv pip install "cognee[gliner]"` | offline, no API key; Claude Code and Codex plugins | young, hackathon churn |
| supermemory | ~31.1k | MIT, TS | pluggable | npm / binary | memory API product | no redaction or audit https://github.com/supermemoryai/supermemory/issues/1348 |
| serena (memories) | ~30.1k | custom, Py | `.serena/memories/*.md` | uvx | model-facing CRUD memory tools; onboarding writes architecture notes | no ranking, pruning or sharing |
| beads | 27,687 | MIT, Go | Dolt, JSONL export | curl install, `bd init`, `bd setup <harness>` | dependency graph; community asks for memory: https://github.com/gastownhall/beads/issues/5877 (118 comments) | migration corruption /4380 |
| letta (MemGPT) | 25,056 | Apache-2.0, Py | Postgres/SQLite blocks | pip, docker | agent self-edits memory blocks | not retrieved |
| MemOS | ~11.7k | Apache-2.0 | hybrid "memory OS" | - | quotes 35.24% token savings | - |
| memvid | ~16.6k | Apache-2.0 | single-file frames | - | one portable file | forks fragment the name |
| basic-memory | 4,107 | AGPL-3.0, Py | Markdown plus wikilinks, Obsidian sync | `uv tool install basic-memory` | closest analogue to shapa's Obsidian browsing | AGPL friction; paid cloud tier |
| byterover-cli (was cipher) | ~5k | - | Qdrant/Milvus | npm | widest harness list; team workspace memory | rename split history |
| mcp-memory-service | ~2k | Apache-2.0, Py | SQLite-vec | pip, docker | autonomous consolidation | sqlite-vec loading, DB corrupts on copy |
| MCP reference "memory" server | (servers monorepo) | - | JSON graph | - | official reference | toy: no scoping or pruning |
| Cline Memory Bank, Roo/Kilo memory bank | (cline 69.9k) | - | fixed Markdown files in repo | copy instructions | git-diffable | grows unbounded, no ranking, manual updates; Roo variant stalled 2025-05 |

## Vendor-native memory (the main threat)

| Feature | What it does | What it does not do |
|---|---|---|
| Claude Code auto-memory (2026-03) | per-project `MEMORY.md` index plus topic files under the config dir, first 200 lines loaded | machine-local, one harness, not in the repo, no ledger, no team sharing, no pruning beyond a size cutoff |
| Codex memories | AGENTS.md plus generated `~/.codex/memories/` | machine-local, one harness, no correction capture |
| Cursor Memories | sidecar extraction into rules | per user per project, no portability, no why-trail |
| Windsurf memories | per-workspace auto memories | siloed per workspace |
| Copilot Memory (2026-03, default on) | repo facts validated against code, shared across Copilot surfaces | expires after 28 days, Copilot only |
| ChatGPT / Claude app memory | cross-chat synthesis, editable entries | chat-scoped, unusable by coding harnesses |

None of the native features is git-tracked, cross-harness, two-tier (global plus repo), correction-aware, or tied to a work ledger.

## White space

1. One memory store shared across harnesses, as plain local files.
2. Memory that is git-native and reviewable in a PR (only Markdown banks and Dolt-backed beads get close).
3. Pruning and consolidation that does not silently lose data (mem0 /5245, mcp-memory-service corruption).
4. Operator corrections as a first-class, attributed row type.
5. Clean global plus per-repo scoping with team sharing.
6. Ledger plus memory in one artifact (beads #5877 is the community asking for exactly this).
7. MIT, stdlib core, one SQLite file, no required service.
8. Benchmarks people trust: numbers in this niche are disputed; a neutral, reproducible harness is open ground.

## Repo-impressiveness checklist

| Item | Example |
|---|---|
| Install in one line, quickstart under 60 s | claude-mem `npx claude-mem install`; beads curl plus `bd init` |
| Demo GIF or video at the top | graphiti, claude-mem |
| Badges: CI, version, license, PyPI | beads, graphiti |
| Benchmark table | mem0 (LoCoMo, LongMemEval), cognee (BEAM) |
| Comparison table vs alternatives | graphiti vs GraphRAG |
| Docs site | docs.claude-mem.ai, beads.gascity.com, docs.mem0.ai |
| Plugin marketplace and MCP registry listings | Claude Code plugin marketplace, Smithery |
| Package manager presence | PyPI, npm, Homebrew |
| Architecture diagram (Mermaid) | beads |
| Harness list in the repo description line | claude-mem, byterover-cli |
| "Local, no API key" as a headline | cognee |
| Visible release cadence (tags, CHANGELOG) | claude-mem, beads |
| Community link (Discord or Discussions) | mem0, cognee, basic-memory |

shapa today (checked 2026-10-06): 2 stars, no description, no topics, no tags or releases, not on PyPI, a 374-character README with no demo, badges, comparison or benchmark.

Related: [[agent-memory-problems]] · [[install-and-cli-ux]]
