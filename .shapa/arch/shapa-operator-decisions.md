---
id: shapa-operator-decisions
type: reference
created: "2026-09-30T02:00:00Z"
consequence: 9
locus: meta
scope: repo
summary: shapa spec §10 - operator decisions 1-7 taken 2026-09-29/30 (leak fix timing, relevance floor, installer extras, placement, lean shape) plus one open.
---

The operator's binding decisions on the shapa backend, numbered as in the original spec, and the one still open. Part of [[shapa-backend-spec]] (§10). The options as first posed are archived in `archive/shapa-backend-spec-2026-09-30.md`.

## Decisions taken

1. **Leakage fix timing: A, but only when it affects the build (2026-09-30).** It doesn't affect the shapa-side build: that tests against the 155-note wiki at `roukh-llm/.worktrees/shapa-memory/.shapa` in place, and the live Claude Code checks use `claude -p --settings`. So the roukh-llm merge and hook switch happen with the roukh-llm consumer migration ([[shapa-roukh-llm-consumer]]).
2. **`MIN_RELEVANCE`: (b) percentile-relative (2026-09-30).** Drop results scoring below a fraction of the top result's fused score. The fraction is a config value, and its default is calibrated on the probe prompt set ([[shapa-wiki-resolution]]).
3. **`bootstrap.sh` extras: (b) interactive prompt (2026-09-30).** Ask whether to install `[semantic]` and `[mcp]`. With no TTY (unattended installs), install the core only and print the exact command to add the extras. Flags `--with-semantic`, `--with-mcp`, and `--full` skip the prompt.
4. **Project memories live only in that project's own `.shapa` (2026-09-30).** The global wiki holds cross-project rules and facts only. There is no `external/<repo>/` staging inside the global wiki. `--scope external --applies-to <repo>` resolves the target repo's path and writes into `<that repo>/.shapa/`, creating the folder if needed. If the repo can't be found locally, it errors and doesn't fall back to the global wiki. `wiki_roots()` loads another project's notes only when the cwd is inside that project.
5. **No phases or roadmaps in any planning output (2026-09-29).** This covers spec prose, CLI output, and generated docs. Plans are written as options, stack, recommendation, open decisions and the next action. See roukh-llm/.shapa/no-phases-or-roadmaps.md.
6. **Lean wiki shape (2026-09-30).** Every wiki, global or repo, has the same lean shape. The engine lints it and `shapa maintain` reports what to fix. Limits are config values; defaults are:
   - `agenda.md` is required. It lists the top 3 fires only (F11 if missing, or if it has more than 3 items). `ideas.md` is optional: an append-only dated log with a status per entry.
   - Live notes at the root (memory/rule/issue): at most 40 (F10). Reference docs in `arch/`: at most 12 (F10). Each note is 150–300 words and each reference at most 2,000 words (F07, [[shapa-note-schema-v2]]).
   - Live wiki total (everything except `archive/` and `attic/`): at most 250 KB (F10).
   - No duplicates: one note per subject. `maintain` merges near-duplicates (the existing merge threshold) and never keeps two live files on the same topic. Superseded notes get `status: superseded` and move to `archive/`.
   - `archive/` and `attic/` are never loaded and don't count toward the limits. They hold history and scratch waiting on an operator decision.
   - `scope` values are `global | repo` only. `external` is retired by decision 4.
7. **Reads never write notes (found 2026-09-30).** `record_use` used to rewrite `uses`/`last_used` in a note's frontmatter on every fetch, leaving counter-only diffs across every repo (26 dirty files in open-trader alone), which blocked upkeep and polluted git. Usage counters move into the index store (`store.py`), keyed by root and note id, and `score.py` reads them from there. Notes change only when their content does. `uses` and `last_used` stay tolerated as legacy frontmatter (ignored, no lint error) and `maintain --backfill` / `shapa upgrade` strip them. Acceptance: running fetch or bootstrap on a clean repo leaves `git status` clean.

## Open

- **Out-of-worktree `config.json` repair during the GAP E build.** A session found `~/.shapa/config.json` pointing at a corrupted path left by an earlier unmocked live probe. The workspace-boundary hook refused a direct write to that file, so the session ran `shapa.config.set_memory_dir(...)` via a one-off `python -c` — shapa's own public API, which the hook's pattern check didn't recognize as the same class of write. The value is correct and no worktree file, test or commit was touched, but the route around the hook was self-authorized and disclosed. Options: (a) accept it as a one-off, disclosed use of shapa's own API to undo shapa's own damage; (b) treat it as a boundary violation, have an operator redo it via a human-run `shapa init`, and add a guard so `set_memory_dir` refuses to run from an unattended session without an explicit flag. GAP E's code (`21b6d76`) is unaffected either way; no fix is queued pending the answer.
