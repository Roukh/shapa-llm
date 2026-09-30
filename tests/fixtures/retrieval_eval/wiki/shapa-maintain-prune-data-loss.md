---
id: shapa-maintain-prune-data-loss
type: rule
created: "2026-07-13T03:10:19.772982+00:00"
consequence: 9
locus: meta
uses: 0
summary: shapa maintain --prune, init, and the orphan-pruner have caused real data loss; four live hazards, treat all as mandatory discipline.
scope: global
status: active
source: memri (consolidated; ids in this file's git history)
---
# shapa CLI operational hazards: prune, init, orphan links, reconciliation

Real data-loss hazards; mandatory discipline for any `.shapa` wiki.

1. **`maintain --prune` can silently delete real wiki files.** A hook test suite, run where a real wiki and the real CLI on `PATH` coexist, twice triggered a REAL prune of the live repo: `shapa-maintain.sh` falls back to `os.getcwd()` on a JSON-parse error, one test fed invalid JSON without synthetic-cwd/PATH overrides — deleting several `.shapa/arch/*.md` plus `adr-constraints.json` (recovered via `git restore`). Compounding: the pruner hit exactly the files with zero frontmatter — `type: reference` protection only applies when a `type` parses. Fixed in PR #233/#234 (pinned `PATH=/usr/bin:/bin`). Takeaways: full frontmatter everywhere; tests exercising a real prune path must isolate BOTH cwd AND `PATH`.
2. **`shapa init <dir>` repoints the GLOBAL default.** Run inside any repo it silently rewrites the machine-wide `~/.shapa/config.json` pointer — one agent's `init` in an unrelated worktree misrouted two concurrent sessions' captures until restored. Check the default first; wanted engine fix: an explicit `--set-default` flag.
3. **`arch/` files still need ≥1 `[[wikilink]]`.** Despite the exemption spec, the orphan-pruner has deleted zero-link `arch/` files (twice on open-trader, silent unstaged deletions of merged files; recovered from HEAD; a `Related: [[...]]` line stops it). Until the engine enforces the exemption, every new `arch/` file must carry a wikilink, mandatory.
4. **No reconciliation or supersession.** The maintenance loop can't resolve contradictory notes; the fetch hook surfaces stale ones as readily as current. Wanted, not built: `superseded_by` plus a contradiction pass. The additive-only habit is the risk — one real case left an old reference unmarked, a ledger stale, and artifacts uncommitted. When superseding, retire the replaced note: `git mv` into `archive/` with `status: superseded` and `superseded_by`.

Related: [[shapa-notes-need-rich-links]] · [[shapa-git-memory-integration]].
