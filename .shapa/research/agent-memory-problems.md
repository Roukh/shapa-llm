---
id: agent-memory-problems
type: reference
created: "2026-10-06T21:10:00Z"
consequence: 7
locus: output-meta
scope: repo
summary: The 15 best-evidenced coding-agent memory problems (2026), what tools do about each, shapa's current coverage, 20 raw feature candidates.
---

# Agent memory: the prominent problems (research 2026-10-06)

Web research round. Ranked by evidence of prominence (independent sources, issue clusters, papers). "shapa today" is from the code at 0.8.0, checked locally. Sources marked (weak) are secondary blogs or mirrors whose primary source was not confirmed. Feature candidates feed [[public-release-plan]]; the work-tracking side is [[agent-work-problems]].

## Ranked problems

| # | Problem | Evidence | Sources | shapa today |
|---|---|---|---|---|
| 1 | Rule files (CLAUDE.md, .cursorrules, AGENTS.md) fade or get ignored as the session grows | issue clusters on Claude Code and the Cursor forum | https://claudeissues.com/issue/2544-bug-claude-md-mandatory-rules-consistently-ignored-across-multiple-repositories (mirror), https://forum.cursor.com/t/cursorrules-getting-ignored/149416 | partial: per-prompt `fetch` re-injects relevant rows every turn instead of once |
| 2 | Compaction wipes working memory; instruction files and skills are not re-read after compact | 8+ distinct Claude Code reports, some labelled design flaw | https://claudeissues.com/issue/21925-design-flaw-context-compaction-destroys-workflow-no-claude-md-reload-no-pause-au , https://claudeissues.com/issue/13919-bug-claude-skills-context-completely-lost-after-auto-compaction-causing-repeated | partial: per-prompt recall survives compaction; no PreCompact/PostCompact hook, no pinned rows |
| 3 | Context rot: longer input degrades reliability; distraction, poisoning, confusion, clash | Chroma 18-model study; Anthropic and Breunig converge on the taxonomy | https://research.trychroma.com/context-rot , https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents , https://simonwillison.net/2025/Jun/29/ | partial: relevance floor and one-line summaries; no hard token budget |
| 4 | Stale and contradictory memories; no forgetting or supersession | MemoryAgentBench names selective forgetting the weakest competency across systems; mem0 shipped "dream" consolidation for it | https://arxiv.org/html/2507.05257v2 , https://mem0.ai/blog/what-is-memory-staleness-in-ai-causes-risks-solutions | partial: after-merge sweep deletes closed/expired/duplicate/superseded rows; no contradiction detection, no last-verified date |
| 5 | No shared team memory; learnings stay on one laptop | several vendors built point fixes (Hjarni, Egregore) for the same gap | https://hjarni.com/blog/shared-team-memory-for-claude-code , https://codex.danielvaughan.com/2026/04/18/git-backed-team-memory-egregore-codex-hooks/ | yes in principle: the repo wiki is tracked in git; binary SQLite blocks merging, so it only commits on the default branch |
| 6 | Vendor lock-in: memory format and location differ per harness | recurring framing across dev blogs | https://developertoolkit.ai/en/org/lock-in-and-portability/ (weak) | partial: hooks for Claude Code; MCP-only for Codex and OpenCode |
| 7 | Memory cannot be inspected, edited, attributed or rolled back | named gap in a 2026 survey; IETF draft on agent memory integrity | https://arxiv.org/pdf/2606.30306 , https://www.ietf.org/archive/id/draft-khandelwal-bmwg-agent-memory-integrity-01.html | partial: `shapa row list/edit`, git history of `shapa.db` (binary, no readable diff), `origin` column |
| 8 | Memory poisoning via prompt injection (content the agent read becomes durable memory) | MINJA (NeurIPS 2025) >95% injection success; live ChatGPT memory exploits | https://arxiv.org/abs/2503.03704 , https://www.theregister.com/2026/01/08/openai_chatgpt_prompt_injection/ | partial: correction capture stores only the operator's own words; capture distils agent text with no trust tier |
| 9 | Secrets and PII persisted into memory | multiple security write-ups; a cross-user contamination figure of 57-71% circulates (weak) | https://www.deepinspect.ai/blog/ai-agent-secrets-handling (weak), https://bmdpat.com/blog/mem0-agent-memory-contamination (weak) | partial: `redact.py` on capture, `ledger scrub` |
| 10 | Cross-project leakage under one global memory | known limitation of account-wide memory | https://community.openai.com/t/persistent-memory-context-issues-with-chatgpt-4-despite-extensive-prompting/1049995/5 | yes: repo wiki plus global wiki, scope-gated writes |
| 11 | Parallel sessions clobber shared memory or duplicate work | 2026 posts on parallel-agent contention; 36.9% of multi-agent failures attributed to inter-agent misalignment (weak) | https://tianpan.co/blog/2026-04-20-parallel-agent-shared-memory-contention , https://dev.to/olund/running-parallel-agent-sessions-without-them-stepping-on-each-other-441o | partial: ledger claim/release, worktrees write the primary checkout's db |
| 12 | Nobody can tell whether memory helps; LoCoMo is near-saturated and recall scores do not track task success | 2026 benchmark papers plus Zep's critique of mem0's claims | https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/ , https://arxiv.org/pdf/2609.24971 | partial: in-repo retrieval eval fixture; use counters in the index; no outcome signal |
| 13 | Recall latency and freshness lag (graph memory needs async LLM extraction) | Zep's own paper documents the v1 latency problem | https://arxiv.org/html/2501.13956v1 | strong: local BM25 plus model2vec, warm daemon, no LLM on the read path |
| 14 | Benchmarks under-test forgetting, conflicts, paraphrase and abstention | MemoryAgentBench, LongMemEval-adjacent critiques | https://mem0.ai/blog/ai-memory-benchmarks-in-2026 | none |
| 15 | Cloud dependence and privacy of hosted memory | Tenable disclosures; Willison contrasts tool-gated recall with always-on cloud memory | https://simonwillison.net/2025/Sep/12/claude-memory/ | strong: local files, no hosted service |

## What tools do today, and the remaining gap (top 8)

1. Rule fade: split rule files, manual reminder commands. Gap: no automatic re-injection tied to context pressure.
2. Compaction: nothing shipped beyond the summary; users re-paste CLAUDE.md. Gap: no re-read of memory after compact, no structured state that survives it.
3. Context rot: prompting patterns (tool loadout, quarantine, pruning, offloading). Gap: not built into harnesses by default.
4. Staleness: mem0 dream consolidation, decay in a few layers. Gap: most markdown and SQLite stores (Basic Memory, claude-mem) append and never reconcile.
5. Team memory: commit CLAUDE.md (rules only); Hjarni and Egregore. Gap: nothing native; merge semantics for memory unsolved.
6. Lock-in: AGENTS.md and MCP. Gap: no portable memory file format has converged.
7. Audit: Claude Code `/memory` shows what is loaded. Gap: no diff over time, no per-row authorship, no rollback.
8. Poisoning: per-exploit patches, instruction hierarchies. Gap: no memory layer checks provenance or trust before writing or recalling.

## Raw feature candidates (tagged to problem #)

1. Last-verified timestamp per row, staleness badge at recall [4]
2. Supersession links with a visible chain instead of silent delete [4, 7]
3. Audit trail: every write carries session id, harness, time and source [7]
4. Browse/edit/merge/delete UI (TUI or local web) over rows and ledger [7]
5. Readable git diffs of memory: text export or a `git diff` textconv driver for `shapa.db` [5, 6, 7]
6. Row-level merge for two branches editing memory (merge driver) [5, 11]
7. Lease on a row while a session updates it [11]
8. Trust tier per row (operator-said, agent-derived, read-from-web/file) with recall filtering [8, 9]
9. Secret scan before any write, reject or redact [9]
10. Hard repo boundary, cross-repo recall only on request [10]
11. Export/import in a documented plain format (JSONL, Markdown) to move memory between harnesses and tools [6]
12. Thin per-harness hook adapters over one store [6]
13. Hard token budget on injected memory, ranked truncation [1, 3]
14. Pinned rows re-injected right after compaction (PreCompact/SessionStart-after-compact hook) [2]
15. Scheduled or on-merge consolidation pass (dedupe, merge, demote) [4]
16. Shadow-mode recall log: what was injected vs what the agent used, as a "does memory help" signal [12, 14]
17. Distractor-aware ranking: demote rows from unrelated features or branches [3, 11]
18. Session-start "what changed since your last session" digest instead of a full dump [1, 3]
19. Per-row visibility: private, team-shared, global [5, 9, 10]
20. Quarantine queue for memories captured from untrusted content, promoted only on confirmation [8]

Related: [[read-path]] · [[write-path]] · [[database]]
