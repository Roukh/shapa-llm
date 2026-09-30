---
id: diff-quality-gate-gotchas
type: memory
created: "2026-08-01T01:10:54.436491+00:00"
consequence: 5
locus: output-meta
uses: 0
summary: diff-quality-gate.js has two false-result modes - --head doesn't affect the file-content read, and its nesting check flags JSON-schema literals as code.
scope: repo
status: active
---
# diff-quality-gate.js: two known false-result modes

Two gotchas in `scripts/quality/lib` (the mechanical diff-quality-gate used for PR checks):

1. **`--head <ref>` only affects added-line computation, not the file content read.** `runQualityGate()`'s default `readFile` always reads the actual working-tree disk content (`path.resolve(repoRoot, file)`), ignoring `--head`. Passing `--head origin/some-branch` while the working tree is checked out somewhere else gives a mismatched result: added-line ranges computed against the ref, but structure/length checked against stale disk content. Only a real checkout (worktree add, or a clean pull) whose disk content matches `--head` gives a trustworthy result — or read file content via `git show <head>:<file>` instead of disk.
2. **Nesting-depth check false-positives on JSON-schema-shaped object literals.** `mechanical-rules.js`'s `scanJsStructure` is a brace-counting heuristic with no concept of "plain object literal vs control flow" — a top-level `const` holding a nested JSON Schema gets flagged the same as real nested if/for/callback logic once it appears as an "added" line relative to whatever base is used (routinely happens when base=`origin/main` and the file is newer than main). Workaround used: extract the innermost schema fragment into its own top-level `const` to shorten the brace-nesting — not a real fix; the checker still can't distinguish data literals from code nesting.

Source: memri 58e7a87a-89d0-464d-b834-434309dff46a, dc7cb9f0-7189-45fa-8631-64f93ccd1593
