---
id: database
type: reference
created: "2026-10-05T20:22:00Z"
consequence: 9
locus: output-meta
scope: repo
summary: shapa.db - the one sqlite file per wiki holding the F/J/T work ledger and M/R/I rows; committed whole, single-writer, swept after every merge.
---

# Database

The wiki's single source of truth: the work ledger (features, jobs, tasks)
and memory/rule/issue rows, in one sqlite file tracked in git.

## Owns

- `shapa/db.py` - schema, row/item CRUD, worktree-to-primary-checkout resolution, the after-merge sweep.
- `shapa/ledger.py` - `shapa ledger`/`row`/`correction`, plus the git and harness trigger functions (`on_commit`, `pre_commit`, `reconcile`, `on_merge`, `hook_posttool`).
- `shapa/migrate4.py` - format 3 to 4 migration (notes/checklist/ideas/memory-log into rows and ledger items).

## Interfaces in

| Caller | Shape |
|---|---|
| `shapa ledger add/claim/release/close/tree/branch/issues` | CLI |
| `shapa row add/edit/rm/link/tag` | CLI |
| git `post-commit` | `shapa ledger on-commit` (closes the `J<n>` the commit subject names) |
| git `pre-commit` | `shapa ledger pre-commit` (blocks a commit of the db file off the default branch) |
| PostToolUse (after `gh pr merge`) | `shapa ledger hook-posttool` |
| SessionStart | `shapa ledger hook-start` / `reconcile` (closes features whose branch merged elsewhere) |

## Interfaces out

- Rows and items read by the read path for ranking/recall and by `shapa ledger`/`row list`/`get` for direct display.

## Invariants

- Tables: `items` (F/J/T, `parent` hierarchy, `status`, `verify`, `git_ref`, `alias`), `mri` (M/R/I, `alias`, `summary`, `body`, `hash`), `tags`, `links` (`about`/`supersedes`/`conflicts`/`related`).
- An ID is a kind letter plus a per-wiki counter, never reused; the hierarchy lives in `parent`, not the ID.
- Every worktree of a repo resolves to the primary checkout's one file (`db_path` walks `.git` -> `commondir`); a feature branch never has its own copy.
- Committed only on the default branch; `shapa ledger pre-commit` refuses the commit anywhere else.
- A merged feature's sweep: closes its open jobs/tasks, deletes its `temp/F<n>/`, expires rows open >30 days, deletes closed rows from earlier merges, dedups/supersedes M/R/I rows, drops memories unused for 60 days, and links similar rule pairs `conflicts` for judgment.
- Deletions are never undone by the engine; the database file's git history is the only record of a removed row.
