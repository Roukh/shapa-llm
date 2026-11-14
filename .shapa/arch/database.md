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
- `shapa/focus.py` (`shapa focus`) and `shapa/skills.py` (`shapa skill`) - the northstar, the three fires and the skill versions, kept in documents.

## Interfaces in

| Caller | Shape |
|---|---|
| `shapa ledger add/claim/release/close/tree/branch/issues` | CLI |
| `shapa row add/edit/rm/link/tag` | CLI |
| `shapa doc add/edit/rm/list` (`shapa/docs.py`) | CLI |
| git `post-commit` | `shapa ledger on-commit` (closes the `J<n>` the commit subject names) |
| git `pre-commit` | `shapa ledger pre-commit` (blocks a commit of the db file off the default branch) |
| PostToolUse (after `gh pr merge`) | `shapa ledger hook-posttool` |
| SessionStart | `shapa ledger hook-start` / `reconcile` (closes features whose branch merged elsewhere) |

## Interfaces out

- Rows and items read by the read path for ranking/recall and by `shapa ledger`/`row list`/`get` for direct display.

## Invariants

- Tables: `items` (F/J/T, `parent` hierarchy, `status`, `verify`, `git_ref`, `alias`), `mri` (M/R/I, `alias`, `summary`, `body`, `hash`), `tags`, `links` (`about`/`supersedes`/`conflicts`/`related`).
- Schema 2 (additive, applied lazily on connect; a read-only open tolerates schema 1; an older shapa ignores the new tables): `docs` (D, `title`, `body`, `mime`, `alias`, never swept for staleness) and `tombstones` (`id`, `kind`, `created`, `deleted_at`, written by `db.delete()` for every row, item or document it removes, the sweep included). `read_rows` also yields documents as `D` rows (title = summary) so the derived index, `fetch` and `bootstrap` see them. `uids` (`id` -> `uid`) is the record identity the hosted sync matches on: random (`secrets.token_hex(8)`) at insert, or for a record with none the legacy uid (first 16 hex of SHA-256 over `id + newline + created`), backfilled on migration and again at sync; it is a side table, not a column, because an older shapa builds its dataclasses from `SELECT *`. Tombstones carry the uid. An idea is an M row tagged `idea`.
- No schema change carries focus, skills or the lean gate (schema stays 2): a wiki's focus is one document (alias and tag `focus`, JSON body), a skill version a document tagged `skill`, `skill:NAME`, `v:N`, `status:...`, and a project's document the one without those tags (`project:KEY` tag, else the wiki's own). `db.add_doc` refuses a second document for a project (`DocExists` names the first) or merges it as a section; `db.merge_docs` folds two; `db.retag` changes tags and stamps the record so sync sees it; `db.close` of a feature calls `focus.on_feature_closed`; `db.add_row`/`tag`/`link` call `skills.lesson_from_row`. The tags `focus` and `skill` are reserved to `focus.py` and `skills.py` (`add_doc(system=True)`).
- `metrics_daily` (schema 2, additive, created lazily on connect like the other schema-2 tables; an older shapa never reads it): `(day, repo)` -> one JSON row of additive measures plus the wiki's live record count, written by `shapa metrics --snapshot` and the sweep (`metrics.prune`), `*` as the repo in the global wiki for the roll-up. It is local to the committed file: the sync never reads or writes it. Raw events are not in this file at all: they are `m_events`, `p_served`, `p_local` and the similarity index (`l_chunks`, `l_tok`, `l_vec`) in the derived `.shapa-index.db`. `metrics_daily` keeps 400 days a repo (about 800 bytes a row). A feature that was ever a fire carries the item tag `fire` (`focus.save`, healed by `metrics.harvest`).
- The learning loop adds no kind and no link relation: principles are R rows tagged `principle`, repeats and generalizations are tags (`repeat-of:REF`, `generalized-into:REF`) plus a `related` link when both ends are in one wiki, because the `links` table's `rel` check and the Worker's rel set stay closed.
- An ID is a kind letter plus a per-wiki counter, never reused; the hierarchy lives in `parent`, not the ID.
- Every worktree of a repo resolves to the primary checkout's one file (`db_path` walks `.git` -> `commondir`); a feature branch never has its own copy.
- Committed only on the default branch; `shapa ledger pre-commit` refuses the commit anywhere else.
- A merged feature's sweep: closes its open jobs/tasks, deletes its `temp/F<n>/`, expires rows open >30 days, deletes closed rows from earlier merges, dedups/supersedes M/R/I rows, drops memories unused for 60 days, and links similar rule pairs `conflicts` for judgment.
- Deletions are never undone by the engine; the database file's git history is the only record of a removed row.
