---
id: agent-work-problems
type: reference
created: "2026-10-06T21:20:00Z"
consequence: 7
locus: output-meta
scope: repo
summary: The 12 best-evidenced agent work-tracking problems (2026), tool survey with verified stars, beads' storage lesson, 20 ledger candidates.
---

# Agent work tracking: problems and tools (research 2026-10-06)

Web research round. Star and open-issue counts verified with the GitHub API on 2026-10-06. claudeissues.com is a third-party mirror of anthropics/claude-code issues. Memory-side problems are in [[agent-memory-problems]]; candidates feed [[public-release-plan]].

## Ranked problems

| # | Problem | Evidence | Sources | shapa today |
|---|---|---|---|---|
| 1 | Plans and TODOs lost on compaction or a new session | dozens of Claude Code issues; a Codex report of progress falling from 97% to 42% after AGENTS.md was dropped mid-task; Anthropic calls it the core problem of long-running agents | https://claudeissues.com/issue/24686-bug-plans-made-in-plan-mode-are-lost-after-compacting-context , https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents | strong: the ledger lives in `shapa.db`, shown at session start |
| 2 | "Fake done": success claimed without verification, or checks gamed | Stop-hook tools built only to catch it; measured as reward hacking on long-horizon benchmarks | https://dev.to/moonrunnerkc/ai-coding-agents-lie-about-their-work-outcome-based-verification-catches-it-12b4 | strong: `ledger close` runs the item's verify command first; result not stored on the row |
| 3 | Parallel agents collide on files, branches and merge targets; textually clean merges break semantically | study of 33,596 agent PRs: four in five compete with another agent PR for the same merge target | https://codex.danielvaughan.com/2026/07/28/agent-pr-merge-conflicts-concurrent-coding-agents-codex-cli-worktree-isolation-coordination-defence/ | partial: claim/release, one branch per feature; no file-level overlap check |
| 4 | Scope creep and goal drift ("while I was in there") | viral Aug-2026 thread; named "scope drift"/"session rot" | https://prefactor.tech/problems/agent-scope-creep | partial: features carry a goal; nothing compares the diff to it |
| 5 | Markdown plan and TODO files rot silently | essays on it; Claude Code plan mode pins a stale plan across topic pivots | https://claudeissues.com/issue/53046-bug-plan-mode-pins-a-stale-plan-file-across-topic-pivots-in-same-session-refile | strong: no plan files; ledger rows close by git events, sweep prunes |
| 6 | Issue-tracker MCP servers cost too many tokens | GitHub MCP tool definitions measured at ~42-55k tokens before the first prompt | https://getunblocked.com/blog/github-mcp-token-cost/ | strong: hook injection is metadata-only; MCP surface is 4 tools |
| 7 | The git-tracked task store itself becomes unmergeable | beads moved from git-tracked files to Dolt (cell-level merge, sync over `refs/dolt/data`) for this reason; README confirms Dolt today | https://blog.gascity.com/posts/beads-1-1-dolt-migrations-with-clones/ , https://github.com/gastownhall/beads | exposed: `shapa.db` is binary, committed only on the default branch; worktrees write the primary checkout's file |
| 8 | No dependency tracking: blocked work started, epics closed early | beads' core pitch ("ready" detection); several 2026 newcomers built on it | https://github.com/gastownhall/beads/issues/4437 | partial: feature contains jobs and tasks; no blocks edges |
| 9 | Worktree and parallel-session overhead, zombie sessions | a whole tool category (claude-squad, Conductor, Sculptor, vibe-kanban); vibe-kanban sessions stuck "running" | https://github.com/BloopAI/vibe-kanban/issues/2495 | partial: after-merge sweep removes feature temp dirs; worktrees not swept |
| 10 | No link from task to the commit or PR that did it | CCPM and Kiro exist to close it | https://github.blog/2025-09-02-spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/ | strong: `J<n>:` commit closes the job, PR merge closes the feature |
| 11 | Duplicate and zombie backlog piles up once agents can write the tracker | beads' duplicate finder drowns on large backlogs | https://github.com/gastownhall/beads/issues/6075 | partial: sweep deletes stale and duplicate rows |
| 12 | Human review and plan approval become the bottleneck | vibe-kanban's own positioning; Cursor plan mode not respected | https://forum.cursor.com/t/plan-mode-is-not-respected-by-the-agent/151802 | none: no "ready for review" state |

## Tool survey

| Tool | Stars / open issues (2026-10-06) | Storage | Init | Differentiator | Top complaints |
|---|---|---|---|---|---|
| beads (`gastownhall/beads`, was steveyegge) | 27,687 / 1,382 | Dolt (embedded or server), JSONL export | `bd init`, hooks and sync prompts | dependency graph, ready detection, hash ids; inspired Claude Code Tasks | migrations unmergeable with un-pulled clones, slow hydration, N+1 graph queries, duplicate finder at scale |
| claude-task-master | 28,174 / 213 (last push 2026-04) | JSON under `.taskmaster/` | `task-master init --rules ...` | PRD-to-tasks via LLM, MCP | stale MCP state corrupting tags, PRD parse timeouts |
| github/spec-kit | 140,423 / 265 | Markdown spec, plan, tasks | `specify init --ai claude` | spec as the primary artifact, 30+ agent integrations | workflow and version drift |
| ccpm | 8,401 / 4 (last push 2026-03) | GitHub Issues | shell scripts | PRD to epic to issue to commit traceability | worktree state bugs |
| Backlog.md | 6,947 / 84 | Markdown in `.backlog/` | `backlog init` | git-diffable Markdown, TUI kanban plus web | id reuse, web-mode memory leak |
| vibe-kanban | 28,270 / 544 | own DB, one worktree per agent | app | agent-agnostic orchestration board | sessions stuck running, hangs |
| claude-squad | 8,573 / 54 | none (tmux plus worktrees) | TUI | session multiplexer | worktree cleanup |
| Claude Code Tasks / TodoWrite / plan mode | built in | per-task JSON, shareable across sessions | none | first-party | plan loss on compaction, stale plan pinned |
| Codex plans, Cursor plan mode | built in | session state | none | first-party | rules dropped at compaction, plan mode ignored |
| Newcomers (clu, np, Dispatch, tasukura) | small | SQLite or MCP-only | single binary | claims before mutation | too new to judge |

## Raw ledger feature candidates (tagged to problem #)

1. Append-only event log tracked in git with SQLite rebuilt as a cache: merges become line merges [7]
2. `shapa resume`: open feature, open jobs, last verify failure as the first injection after compaction or a new session [1]
3. Verify result (exit code, output tail, commit) stored on the closed row as a replayable artifact [2]
4. `shapa audit`: re-run stored verify commands of closed items, flag the ones that now fail [2]
5. Non-blocking warning when a `J<n>:` commit touches far more files than its job declared [4]
6. `blocks` / `blocked_by` edges between jobs plus a `shapa ready` query [8]
7. Claims with a TTL so session start releases claims from crashed agents [3]
8. Overlap check: open features touching the same files or symbols, shown before work starts [3]
9. Staleness flag on any doc linked to a job when its described files changed N commits ago [5]
10. A tiny verb set (next, claim, close, link) for CLI and MCP to keep schema tokens low [6]
11. Tasks also close from git evidence, not only jobs and features [10]
12. Typed close reasons (done, superseded, covered, wontfix, duplicate, obsolete) [11]
13. Structured handoff export when a session ends mid-feature, readable by another harness [1, 12]
14. Stale-worktree sweep in the same after-merge pass that prunes rows [9]
15. Duplicate detection scoped to one feature, not the whole ledger [11]
16. "Who else is on this file or job" lookup from open claims [3]
17. Tiered verify: fast check gates close, full suite advisory [2]
18. Drift check: feature diff vs its one-sentence goal, flagged for review [4]
19. A `review` state between open and closed, with an approve command [12]
20. Harness-neutral ledger format so Claude Code, Codex, Cursor and Gemini sessions share one plan [1, 3]

Cross-cutting: beads is a live case study of shapa's bet. It started with git-tracked files and moved to Dolt because concurrent writers across clones could not merge. shapa's one-commit-per-job and default-branch-only commits reduce but do not remove that exposure (candidates 1, 6, 7).

Related: [[database]] · [[agent-memory-problems]]
