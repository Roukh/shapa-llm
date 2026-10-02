---
name: shapa-upgrade
description: 'Bring every shapa wiki to the installed shapa''s current format. Use when session context says "shapa: wiki <path> is format N<M - run the shapa-upgrade skill", when an installer or `shapa upgrade --check` lists wikis behind, or after updating shapa.'
---

# shapa-upgrade

Every shapa update can change what a conformant wiki looks like. `shapa upgrade`
applies the mechanical part itself and hands back a per-wiki work list of
judgment items. This skill dispatches one agent per behind wiki to finish that
list, then proves everything is current.

## 1. Find the wikis that are behind

```bash
shapa upgrade --all --check --json
```

Read `behind` (paths) and, per wiki in `wikis`, `mechanical` (pending
migrations - format 3 adds `gitattributes` and `memory`, the session-note
conversion), `counters` (per note, the legacy `uses:`/`last_used:`
frontmatter lines the upgrade deletes) and `work` (judgment items: `code`,
`message`, `files` - format 3 adds **MEMLOG**: malformed memory-log lines
that need hand repair). `steps` says what each mechanical migration changes.
Nothing behind means you are done.

## 2. Dispatch one agent per behind wiki

Run them in parallel, one per wiki path. Use a subagent when this session may
write to that wiki's repo. When the harness write guard is scoped to the
session's cwd, start a headless session inside that repo instead:

```bash
cd <repo containing the wiki> && claude -p "<the brief below, with the wiki path>"
```

Brief for each agent:

1. Run `shapa upgrade <wiki path>`. It applies the mechanical migrations and
   prints what is left. Rerun it after every change; it is idempotent.
2. Resolve each work item using the lean wiki shape in the wiki's `AGENTS.md` §11:
   - **F10 caps** (40 root notes, 12 `arch/` refs, 250 KB): archive what is no
     longer live. Set `status: superseded`, then `shapa maintain --lean --apply`
     moves it into `archive/`. Archive, never delete.
   - **DUP / DUP-ID**: merge into one note. Keep the richer note, fold in the
     other's unique facts, and archive the other with `supersedes:` on the keeper.
   - **F04 summary**: write one line, at most 160 chars, from the note's own
     content. Never invent facts.
   - **F07 length**: split into linked notes of 150-300 words each (references
     at most 2,000 words).
   - **F11 agenda**: keep the top 3 fires only. Move the rest to `ideas.md`,
     an append-only dated log.
   - **F05/F06 scope, LAYOUT**: project memories live only in that project's own
     wiki. The global wiki holds cross-project rules and facts. Move misplaced
     notes to the right wiki (`git mv` within a repo); never copy them into
     the global wiki.
   - **PARSE / NOFM / F01-F03 / S01 / S02 / S04**: repair the frontmatter to the
     schema in `AGENTS.md` §3.
   - **MEMLOG** (format 3): one or more lines in `memory/*.jsonl` don't parse
     as JSON. The log is otherwise append-only - never rewrite the file.
     Open it, fix or delete only the malformed line(s) by hand, keep every
     other line byte-for-byte, and let this go into the same upgrade commit.
3. Validate. `shapa upgrade <wiki path>` must print `current`, and
   `shapa validate <wiki path>` must report no errors.
4. Commit in that repo, on its current branch, staging the wiki directory -
   `git add -- <wiki path>` covers the whole tree, including the `memory/`
   log directory and the new `.gitattributes` (format 3: both are part of
   this commit, same as any other mechanical migration) -
   `&& git commit -m "chore(shapa): upgrade wiki to format N"`.
   Never push. If the wiki is not in a git repo, skip the commit and say so.
   **The counter removal is part of this commit.** The upgrade deletes every
   `uses:`/`last_used:` frontmatter line (listed under `[counters]`) after
   copying the values into the index store. Those deletions are the format 2
   migration, not counter noise. Never restore them (`git checkout`,
   `git restore`) or leave them out of the commit, even if the repo's own
   instructions say to discard counter-only diffs. That rule was for shapa
   0.6 writing counters on every read; it does not apply to an upgrade.
   **The memory conversion is part of this commit too** (format 3): every
   live `memory-session-*.md` note was converted into one `memory/` log
   record and moved into `archive/` (`git mv` when this is a git checkout -
   `git status` shows it as a rename, not a delete+add). Commit the archive
   move and the new log lines together; never leave the archived note
   untracked or restore it to the root.
5. If an item needs an operator decision (two notes contradict and neither is
   clearly right, or the owner of a note is unclear), leave it unresolved and
   report it. Do not force the check green.

## 3. Prove it

```bash
shapa upgrade --all --check
```

This exits 0 when every wiki is current. If it doesn't, report the remaining
wikis and their items with each agent's reason. Never claim success over a red
check.
