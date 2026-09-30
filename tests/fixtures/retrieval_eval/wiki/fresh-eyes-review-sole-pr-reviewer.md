---
id: fresh-eyes-review-sole-pr-reviewer
type: rule
created: "2026-07-31T02:51:02.260146+00:00"
consequence: 8
locus: output-meta
uses: 0
summary: fresh-eyes-review is the sole PR review mechanism (CodeRabbit/Qodo retired); review agents must never run mutating git in a checkout they didn't create.
scope: global
status: active
source: memri (consolidated; ids in this file's git history)
---
# fresh-eyes-review is the sole PR reviewer (CodeRabbit/Qodo retired)

Operator decision 2026-08-01: CodeRabbit and Qodo are retired as PR reviewers. `skills/engineering/fresh-eyes-review/` is now the only PR review mechanism, repo-wide — the manual fresh-eyes substitute had already become the de facto standing procedure (see [[third-party-review-bots-billing-blocked]] for why) before the retirement made it official. `BLIND-SPOTS.md` gained an additive CodeRabbit-practices checklist (security-sensitive patterns, error-handling paths, resource cleanup, API-contract drift, naming/dead-code, test-assertion quality, race conditions, boundary input validation) on top of its existing 10-item taxonomy; `SKILL.md` now states sole-reviewer status. The bot infra itself (`coderabbit-app.js`, `qodo.js`, `danger/rules.js`) was left in place, not decommissioned.

**Gotcha**: `SKILL.md` documents invoking a `Workflow` tool (`review-prs.workflow.js`) for the fresh-reviewer + adversarial-verify pipeline, but that tool isn't present in every session's toolset. Workaround: read `BLIND-SPOTS.md` directly, then dispatch a fresh-context agent with the *original job spec* (not the PR's own description) plus the checklist against the real diff. This skips the adversarial-verify pass — fine for CLEAN verdicts; note the gap when findings surface.

**Caught a real fabricated-verification claim** (PR #116, open-trader): a PR body claimed a companion fix had "already been applied live" when it hadn't; a zero-context review re-ran the cited command and found it false, catching a crash-loop-on-merge before it happened. An operational claim needs the same re-read-back evidence discipline as a code claim.

**Never run mutating git in a checkout you didn't create**: a fresh-eyes subagent once ran `git checkout origin/main -- .` inside the operator's persistent `uilib` checkout, staging ~76 paths of `origin/main` over ~30 files of uncommitted operator edits — rescued only by a fortuitous reflog snapshot. `checkout --`, `reset`, `clean`, `stash`, and `pull` are forbidden in any checkout a review agent didn't itself create; use a throwaway worktree or clone for a clean comparison tree instead.
