You are restructuring one shapa wiki to format 4. That is your only job. The wiki is `{wiki}` and its repo is `{repo}`.

Format 4: the wiki's tracked `shapa.db` holds the work ledger and the memory, rule and issue rows. Markdown remains only in `arch/` (one file per box of a diagram of the system, written for agents) and `research/` (one file per topic). `AGENTS.md` in the wiki is the schema; read it first. Read `placement.md` too.

Ground rules:
- Work only inside the wiki directory, from the repo's primary checkout on its default branch. The database commits only there. If the wiki has uncommitted changes you did not make, stop and report them. `.shapa-reconfigure-claim` is yours: it marks this job as taken, it is gitignored, and finishing removes it.
- Never run `shapa maintain --prune`.
- Never print secret values.
- If the repo is public, nothing private may land in it: no secrets, no client names, no other projects' details.
- Follow the operator's own rules from the session context: privacy, git identity, commit style.
- Deleting is fine; git history keeps everything. Never delete content that is still live without moving it first.

Steps:

1. Run `shapa upgrade {wiki}` for the mechanical migration. It turns notes into rows, the checklist into ledger features and jobs, and ideas and operator log records into memories. Read the work list it prints.

2. Fix the rows. Read them with `shapa row list` and each one with `shapa get ID`.
   - A memory (`M`) is an event: something the operator said that matters, or a shift in strategy.
     - Delete agent reports, summaries, fragments and stale status.
     - Re-file standing instructions as rules (`shapa row add R ... --scope repo`, then `shapa row rm` the old one).
     - Re-file corrections of an agent as issues (`I`).
   - Rules (`R`):
     - Merge overlapping rules into one: keep the clearer row, fold the unique detail in with `shapa row edit`, and `rm` the rest.
     - A rule that contradicts a stricter one gets fixed or removed.
   - Delete rows about code, services or files that no longer exist. Check the repo before deleting.
   - Add tags where a row belongs to an area. Tags drive which past issues an agent sees.

3. Fix the ledger. Read it with `shapa ledger list`.
   - Feature titles are plain outcomes ("The site works on phones"), never leftover section names.
   - Job titles say what done looks like.
   - Close (`shapa ledger close ID`) every item the repo shows is already done. Close runs the item's verify command first; when that fails, leave the item open and name it in your report.

4. Fix the files.
   - The wiki root keeps only `AGENTS.md`, `placement.md`, the database and the format and git files.
   - `arch/` holds `index.md`, with a table of boxes and a table of edges, plus one file per box. Each box file is rows, not prose: purpose, owned paths, interfaces in and out, invariants. At most 12 files.
   - `research/` holds one file per topic: question, method, findings, sources, date.
   - Split documents over the word cap.
   - Fold live content from other root notes and other folders (`archive/`, `attic/`, `tools/`, plans, logs) into those boxes, research files or rows, then delete the originals. Tool code doesn't belong in a wiki: move it into the repo and say where.

5. Verify.
   - `shapa upgrade --check {wiki}` exits 0.
   - `shapa validate` shows no errors for this wiki.
   - `git status` shows changes only under the wiki.

6. Finish.
   - Run `shapa upgrade --mark-reconfigured {wiki}`. It records the restructure in the database and ends the session-start directive.
   - Commit the wiki alone (the database included) on the default branch, in one commit, with a message such as "chore(shapa): restructure the wiki to format 4". Push only if the repo's own rules let agents push.

7. Report in at most 12 lines:
   - rows by kind before and after;
   - rows deleted or re-filed, and why in one phrase per group;
   - ledger features and open jobs;
   - files removed and boxes written;
   - anything you were unsure about and left for the operator.
