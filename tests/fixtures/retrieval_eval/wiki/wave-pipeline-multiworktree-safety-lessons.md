---
id: wave-pipeline-multiworktree-safety-lessons
type: rule
created: "2026-09-02T02:22:48.351955+00:00"
summary: "Eight parallel-worktree defects: shared taskUuid, untrusted CI transcripts, cwd/branch bugs, sandbox Date() crash, and OAuth 401 refresh races."
scope: global
status: active
source: memri (consolidated; ids in this file's git history)
consequence: 7
locus: meta
uses: 0
---
# Wave-Pipeline / Multi-Worktree Safety Lessons

Eight real defects from parallel wave-pipeline tracks and cross-worktree review:

- **Shared taskUuid corrupts acceptance criteria**: two tracks on one brain taskUuid overwrote each other's `acceptance_criteria` on PATCH. Refuse dispatch when configs share a taskUuid, or key criteria per track.
- **Trust the CI re-run, not the transcript**: a PR claimed "1246/34"; re-running the same SHA gave "1226/33" - re-run CI at merge/verify and diff counts against the PR body.
- **Don't hardcode `origin/main` as the merge-base fallback**: an orphan-history `main` (open-trader) breaks that assumption. Make the base branch configurable per repo.
- **Never `git checkout` a review branch in the PRIMARY checkout**: it moves HEAD off the real branch and blocks the next `--ff-only` merge. Review from a throwaway worktree, `gh pr diff`, or `git show <sha>:<path>`.
- **Bash cwd persists across calls and drives branch resolution**: a subagent editing worktree B with cwd still in worktree A got misidentified - derive branch from the target file's git dir, not shell cwd; `cd` explicitly each call.
- **`git worktree add <relative-path>` nests inside the current worktree**: use an absolute path or verify cwd is the true repo root. See [[worktree-hygiene-discipline]].
- **Sandbox `Date()` crash**: a default param computing `new Date()` throws in the Workflow sandbox, killing every track reaching merge; a sibling bug iterated `r.prs` unguarded against nulls. Fix: thread `dateStamp` via config + try/catch, filter nulls before iterating. See [[workflow-tool-script-gotchas]]. An unpatched second copy of the same function in `wave-pipeline.lib.js` still needs auditing.
- **Parallel sessions 401 together (OAuth race)**: 6 build subagents died with `401` within ~18 minutes while the orchestrator's auth stayed valid—a concurrent token-refresh race across headless subagents sharing one credential (upstream GH #7100, NOT_PLANNED), not revocation. Treat a 401 burst as retry/resume-able, not a killed batch.
