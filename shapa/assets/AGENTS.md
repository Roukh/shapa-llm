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
  .shapa-code.db         derived code map of the checkout: `shapa map` (gitignored, rebuilt)
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

- A feature is a new capability: something the project could not do before
  (a new command, integration, file format or workflow), built over several
  jobs that are reviewed together. Everything else is a job: a fix, a
  correction to existing behavior, docs, release or repo work, even when it
  spans several files. When unsure, it is a job.
- A job under a feature commits on the feature's branch. A standalone job
  commits on the session's batch branch `jobs-<YYYY-MM-DD>` (a `-2` suffix
  when that name is taken): every standalone job of a session shares that one
  branch and one PR.
- An ID is the kind letter plus a per-wiki counter, never reused. The
  hierarchy is the `parent` column, not the ID. Standalone jobs and tasks are
  fine.
- `shapa ledger` lists live work; `shapa ledger add F|J|T TITLE [--parent ID]
  [--verify CMD]`; `claim`/`release`; `close ID` (runs the item's verify
  command first, refuses on failure); `tree ID`; `edit ID --parent ID|none`
  moves a job or task, or makes it standalone; `branch F<n>` names and
  records a feature's branch; `issues ID` shows past issues for an item.
- Triggers (git hooks linked by `shapa init`, or re-linked by hand with
  `shapa ledger git-hooks`): a git
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
- An idea is a memory tagged `idea` (`shapa save --type idea`), not a kind.
  A decision is a memory tagged `decision` (`--type decision`). Neither is
  swept for disuse.
- A rule tagged `principle` is written by `shapa learn apply` from the
  operator's corrections: its summary is the statement, its body the evidence.
  The tags the learning loop keeps on it and on corrections (`principle`,
  `violations:N`, `serves:...`, `repeat-of:...`, `learned` and the like) are
  the engine's: `shapa row add|tag`, `shapa save` and the MCP `save` refuse
  them. Edit a principle's wording through `shapa learn apply --into`, never
  by hand.

## The global wiki as the one brain

- Tasks: `shapa save --type human_task|agent_task` (or `shapa ledger add`)
  files a `T` item tagged with its owner, standing alone, with no feature.
  `shapa tasks` lists the operator's open `human_task` items (`--all` for
  every wiki); open text identical to a stored task is stored once.
- Stages: the tag `stage:N` (one digit, one per record) on a rule, memory or
  skill version. `stage:0` is the baseline, printed in full at every session
  start within 6,000 characters. Only `shapa stage set|clear|list|pending` at
  the terminal sets it; an agent that asks for `stage:0` leaves a
  `stage-proposed:0` tag, and the operator reviews it with `shapa stage
  pending`. `shapa bootstrap --stage N` prints stages up to N.
- A skill a repo needs everywhere moves to the global wiki with
  `shapa skill uproot NAME` (run in the repo); `shapa skill list` names where
  each skill lives and flags a repo skill that shadows a global one.
- Stage 0 belongs to the global wiki: a repo wiki takes stages 1-9 only.
- Tags are the relations: `shapa tags` lists them with counts,
  `shapa fetch --tag NAME` (MCP `search` with `tags`) returns what carries a
  tag, `shapa related ID` returns what shares tags or links with it. The tag
  `project:KEY` marks a record as part of project KEY and keeps an `about`
  link to that project's document.
- Project files: `shapa project new KEY`, `show`, `list`, `add KEY SECTION
  TEXT`. A project is one document built from `assets/project-template.md`;
  `add` appends one line under a section (`issues`, `questions`,
  `implementations`, `problem`, `solution`, `diff`, `advantages`, `villains`,
  `loop_market`, `loop_product`).
- Write each record back as it happens: decisions, ideas, tasks, and a
  project item for anything that belongs to a project.

## Documents

`D` documents (database schema 2) hold a titled body kept whole: a spec, a
runbook, pasted research. `shapa doc add TITLE --scope global|repo` (body from
`--body`, `--file` or stdin), `edit`, `rm`, `list`; `shapa get D<n>` or its
alias prints it. They take tags and links like rows, pass the same secret
redaction, and are never swept for staleness. Recall shows the title only.
Every delete (row, item or document) leaves a tombstone, so a hosted store
(`shapa remote`, opt-in) can tell a deletion from a missing file.

## The database file

- Every worktree of a repo uses the primary checkout's `shapa.db` (resolved
  through git's common directory), so there is one ID counter per repo and a
  feature branch never changes its own copy.
- It is committed only on the default branch. `shapa commit` does this
  itself, as a SessionEnd hook (with SessionStart as a catch-up for a
  session that crashed before SessionEnd fired), scrubbing any configured
  terms first. A git `pre-commit` hook running `shapa ledger pre-commit`
  still refuses a manual commit of it on any other branch: a branch that
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
Until a migrated or older wiki is restructured by hand (rows sorted, arch
boxes written), every session's bootstrap opens with a directive to dispatch
one dedicated agent with `shapa upgrade --reconfigure-prompt <wiki>`; that
agent ends it with `shapa upgrade --mark-reconfigured <wiki>`.
