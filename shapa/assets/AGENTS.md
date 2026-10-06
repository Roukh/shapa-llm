---
id: AGENTS
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 8
locus: output
---

# AGENTS.md — shapa wiki rules (format 4)

> Installed and rewritten by shapa (`shapa upgrade`); never hand-edited in a
> wiki. It is also the marker that makes a directory a wiki.

shapa = **S**elf-**H**ealing **A**utonomous **P**ersistent **A**gent. A wiki
holds only what one session needs. Pruning is the default path: every merged
feature cleans the database.

## Layout

```
<wiki>/                  a repo's .shapa/ (or shapa/), or the global wiki
  AGENTS.md  placement.md   shipped rules (this file is the marker)
  shapa.db               the database - tracked in git as the file itself
  arch/                  agent-facing diagram: index.md (boxes, edges) + one file per box
  research/              one .md per research topic, for operator and agent
  temp/F<n>/             scrap notes of one feature (gitignored, gone at its merge)
  .shapa-format          the wiki format (4)
  .shapa-index.db        derived search index and vectors (gitignored, rebuilt)
```

A repo wiki holds the work ledger and its own memories, rules and issues. The
global wiki holds memories, rules and issues only - no work ledger. Placement
of a new row: [[placement]].

## The work ledger (repo wikis)

| Kind | Holds | Git artifact | Closes on |
|---|---|---|---|
| `F` feature | jobs | branch `F<n>-slug` and a PR | the PR's merge |
| `J` job | tasks | one commit whose subject starts `J<n>:` | that commit |
| `T` task | - | none | `shapa ledger close T<n>` |

- An ID is the kind letter plus a per-wiki counter, never reused. The
  hierarchy is the `parent` column, not the ID. Standalone jobs and tasks are
  fine.
- `shapa ledger` lists live work; `shapa ledger add F|J|T TITLE [--parent ID]
  [--verify CMD]`; `claim`/`release`; `close ID` (runs the item's verify
  command first, refuses on failure); `tree ID`; `branch F<n>` names and
  records a feature's branch; `issues ID` shows past issues for an item.
- Triggers (git hooks installed per repo by `shapa ledger git-hooks`): a git
  `post-commit` hook runs `shapa ledger on-commit` (closes the
  `J<n>` the subject names); after `gh pr merge` a PostToolUse hook runs
  `shapa ledger hook-posttool`; SessionStart runs `shapa ledger hook-start`
  (closes features whose branch merged elsewhere). Hooks never run a verify
  command.
- Every merged feature closes its open jobs and tasks, deletes its
  `temp/F<n>/`, and sweeps the database: open rows older than 30 days close
  as expired; closed rows from earlier merges are deleted; duplicate and
  superseded memories, rules and issues are deleted; memories unused for 60
  days are deleted; similar rule pairs are linked `conflicts` for judgment.
  Deleted rows live on in the database file's git history.

## Memories, rules, issues

| Kind | Is | Written by |
|---|---|---|
| `M` memory | an event: something the operator said that matters, or a strategy shift | the Stop hook, from the operator's own messages only |
| `R` rule | a standing rule | `shapa row add R ... --scope ...` or `shapa save --type rule` |
| `I` issue | an agent mistake the operator corrected | the UserPromptSubmit hook (`shapa correction`) on "no", "wrong", "not like this" and the like |

- An issue links (`about`) to the work items claimed when it was written;
  claiming an item prints its most related past issues (direct link, shared
  tag, then text similarity).
- `shapa row list|add|edit|rm|link|tag`. `shapa get ID` (or an old note's id,
  kept as the row's alias) prints a row or a ledger item in full.
- Reads (`bootstrap`, `fetch`, MCP `search`/`get`) rank rows and any
  remaining note files together.

## The database file

- Every worktree of a repo uses the primary checkout's `shapa.db` (resolved
  through git's common directory), so there is one ID counter per repo and a
  feature branch never changes its own copy.
- It is committed only on the default branch. A git `pre-commit` hook running
  `shapa ledger pre-commit` refuses it on any other branch: a branch that
  commits its own copy would make every checkout swap the database.
- `.gitattributes` marks it `binary`; the rollback journal keeps the file
  complete between writes.

## arch and research

- `arch/` is for the agent: rows, not prose. `index.md` lists the boxes and
  edges of the system; each box gets one file (purpose, owned paths,
  interfaces, invariants). Gotchas are issue rows tagged with the box name.
  At most 12 files.
- `research/` holds one file per topic: question, method, findings, sources,
  date. Never loaded at session start; found by search.

## Validation and upgrade

`shapa validate --all-roots` checks note frontmatter (`arch/`, `research/`)
and the lean caps: at most 40 live root notes, 12 `arch/` files, 250 KB.
`shapa upgrade` migrates a format-3 wiki: memory/rule/issue notes become rows
(the note id kept as alias), checklist sections become features and their open
items jobs, open ideas and operator-sourced memory-log records become
memories, and `agenda.md`, `ideas.md`, `checklist.md` and `memory/` go away.
