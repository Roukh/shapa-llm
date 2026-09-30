---
id: worktree-hygiene-discipline
type: rule
created: "2026-07-12T22:53:23.904621+00:00"
summary: "Worktrees live at <repo>/.worktrees/; watch for stale main refs, missing nested npm installs, and sandboxed writes that silently no-op."
scope: global
status: active
consequence: 7
locus: meta
uses: 0
---
# Worktree Hygiene Discipline

Recurring worktree-mechanics failures, consolidated from operator reports and incident sessions:

- **Placement**: worktrees belong at `<repo>/.worktrees/` (repo root), never as siblings of the repo (`roukh-llm-wt-*`, `uilib-worktrees/`), never nested inside another worktree, and never left in `/tmp`. Tear one down right after its PR merges — several (`w2-roukh-ui`, `w2-task-lifecycle`) survived merge because teardown was skipped.
- **Stale `main` ref**: a worktree's local `main` branch doesn't auto-update and can lag `origin/main` by several merged PRs, so diffing against it is misleading (already-merged files look "new"). Diff/log against `origin/main` explicitly; don't force-update a branch another worktree has checked out live just to fix this.
- **Nested subpackages**: a fresh worktree shares tracked files but not gitignored `node_modules`. Any nested subpackage (its own `package.json`) needs its own `npm install` before trusting a full-suite test run — missing this looks like a regression (`ERR_MODULE_NOT_FOUND`) but isn't.
- **Sandboxed writes silently drop**: `git worktree add` to a path outside the primary project dir, run from a sandboxed Bash call, can create the branch ref but silently no-op the working-tree write — no error. Verify the directory actually exists afterward. Also: if `git worktree add` fails mid-`&&`-chain and a heredoc follows, later commands in that chain still run unconditionally in the ORIGINAL cwd — never put a commit command after a heredoc in the same call.

Source: memri f8c9f6b5-d4f5-45b4-84c2-ecd456b6facd, 03bcb318-7710-46f2-87ef-981709692bcb, 46bce4db, 9ada37fc-e4fe-4900-8eb1-933f149b88c4
