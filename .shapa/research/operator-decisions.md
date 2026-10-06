---
id: operator-decisions
type: reference
created: "2026-09-30T02:00:00Z"
consequence: 6
locus: meta
scope: repo
summary: Historical record of the operator decisions (2026-09-29/30) that shaped the multi-root, lean-wiki, scope-gated design later implemented as format 4.
---

Historical decision log from designing the pre-format-4 backend. The decisions below are already implemented in the current engine (see `arch/`); kept here for why, not as a spec to re-read for how.

## Decisions taken

1. **Cross-project leakage fix.** A session's memory must never leak another project's notes into an unrelated repo's context. The fix shipped as part of a consumer migration in a separate, private repo and is out of this repo's scope.
2. **Relevance floor: percentile-relative.** Drop results scoring below a fraction of the top result's fused score, rather than a fixed absolute cutoff. The fraction is a config value, calibrated on a probe prompt set.
3. **Installer extras: interactive prompt.** `bootstrap.sh` asks whether to install the semantic and MCP extras. With no TTY (unattended installs), it installs the core only and prints the exact follow-up command. Flags skip the prompt.
4. **A project's memories live only in that project's own wiki.** No cross-project staging area inside the global wiki. Writing into a different repo's wiki by name is explicit and errors rather than falling back to the global wiki if that repo can't be found locally.
5. **No phases or roadmaps in any planning output.** Plans are written as options, stance, recommendation, open decisions and the next action.
6. **Lean wiki shape.** Every wiki, global or repo, keeps the same lean shape: a small cap on live root items, a small cap on reference docs, a total size cap, and no duplicate notes on one subject. This became format 4's work ledger plus the `arch/`/`research/` split.
7. **Reads never write notes.** A read path must never dirty a tracked file as a side effect of being read; usage counters move into a derived, gitignored index instead of living in frontmatter. This surfaced after a read path was found leaving counter-only diffs across many repos in one sweep, which blocked normal upkeep and polluted git history.

## Open (as of this record)

- **Out-of-scope config repair.** A session once needed to repair a corrupted global-wiki pointer from inside a sandboxed session, using the tool's own public API rather than the normal CLI path, because a workspace-boundary guard didn't recognize that API call as the same class of write as a direct file edit. The repair was correct and disclosed, but whether a self-authorized route around a guard (even using the tool's own sanctioned API) is acceptable, versus always requiring an operator-run fix, was left as a judgment call rather than resolved in code.
