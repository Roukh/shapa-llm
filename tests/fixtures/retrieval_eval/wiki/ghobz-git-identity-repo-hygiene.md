---
id: ghobz-git-identity-repo-hygiene
type: rule
created: "2026-06-18T21:46:37.916606+00:00"
consequence: 7
locus: output-meta
uses: 0
summary: Dual gh-account (Roukh/gh0bz) identity and switching rules, plus git/gh hazards - account flips, stash sharing, merge, PR-base, YAML CI gotchas.
scope: global
status: active
source: memri (15 notes consolidated; ids in this file's git history)
---
# ghobz Git Identity & Repo Hygiene

Two `gh` accounts: `Roukh` (roukh-*), `gh0bz` (ghobz org). Match the remote owner (`git remote -v`) before any git/PR op: set local (never `--global`) identity and `gh auth switch`. In gh0bz repos (incl. `ghobz_projects/*`) `admin@ghobz.com` may author commits but never reach client runtime (SMTP, CRM data, env vars). Fix wrong author: `commit --amend --reset-author` + `push --force-with-lease`.

Pre-commit: strip scraper artifacts (`.firecrawl/`, `.playwright-mcp/`), unused assets (grep zero refs), screenshots, dead code, oversized media (compress, don't delete), `node_modules`; re-verify build/typecheck.

- A false 404 on a roukh-* repo = wrong active account, not revoked access; accounts flip mid-session (background `gh pr view` polling)—re-switch before each `push`/`gh api`.
- `gh repo list`/`search`/`api .../repos` see only the active account—audits miss the other's repos; `gh repo delete` needs the `delete_repo` scope (`gh auth refresh -s delete_repo`).
- `gh pr merge --squash --delete-branch` can fail on a worktree conflict AFTER the GitHub merge succeeded—check `gh pr view --json state,mergedAt` before destructive retries.
- Push a non-default PR base current FIRST—else the diff silently includes every commit the base lacks (`gh pr view <n> --json files` to verify).
- Never truncate `git merge`/status output (hides CONFLICT lines). Bash cwd resets between calls—never assume a worktree before destructive ops. `guard.py` denies `git branch -D`/`--force` unconditionally; no ack-file bypass.
- `git stash` is shared across worktrees—a bare `pop` can grab another agent's entry; `push -u -m "<tag>"`, restore via `apply <sha>`.
- A textually clean merge can still be semantically wrong when branches extend one code path—re-read touched functions; green tests say nothing about untested paths.
- An unquoted YAML step `name:` with `: ` silently breaks that job on `pull_request` while other checks stay green—quote it, verify the real job list ran.

Related: [[worktree-hygiene-discipline]].
