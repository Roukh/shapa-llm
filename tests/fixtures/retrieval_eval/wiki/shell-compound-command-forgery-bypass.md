---
id: shell-compound-command-forgery-bypass
type: rule
created: "2026-07-11T21:03:18.611941+00:00"
consequence: 7
locus: output
uses: 0
summary: Guard/hook gotchas - compound-command forgery, quoted-keyword false positives, chained record-then-act denials, and cwd that ignores cd/--repo.
scope: global
status: active
source: memri (consolidated; ids in this file's git history)
---
# Guard-script and hook matching gotchas

Guard-script/hook bypasses found in sibling scripts; relevant to any gate (including `guard.py`) that verifies commands by output/string matching rather than exit status.

- **Compound-command forgery.** A gate that (1) matches a command's presence anywhere in a compound Bash call, then (2) substring-searches the whole tool response for a known success line, is bypassable: `real-command --that-can-fail; echo "the exact success line"` (or `... & echo ...`) passes (1) via its first segment and self-satisfies (2) via its forged tail. The splitter must break on every shell separator including a lone background `&` — e.g. `(?:&&|\|\||&(?!&)|[;\n|])`. Fix: require exactly one command-matching segment AND every other segment from a small allowlist (e.g. bare `cd <path>`); anything else voids the match.
- **Quote-stripping before keyword match.** A destructive-command guard blocked `gh pr create --body ...` because the body quoted a verification transcript containing `DROP TABLE` — evidence of a correctly-rejected drop, not a drop. Plain substring matching can't distinguish a dangerous keyword in the command from one inside a quoted argument; strip quoted spans before matching. Workaround when hit: `--body-file` instead of inline `--body`.
- **Chained commands defeat record-then-act gates.** A PreToolUse-style hook evaluates the whole command string before any of it runs — `checkpoint ... && gh pr merge ...` is denied in full, the gate seeing the pending merge with no checkpoint yet recorded. Issue any record-then-act sequence as two separate tool calls, never chained.
- **cwd does not follow `cd`/`--repo` flags.** Every Bash call's cwd resets to the session/repo root, so an in-command `cd` or a `--repo` flag is invisible to a hook inspecting the calling process's cwd — cross-repo operations misresolve. Resolve repo identity explicitly, never from cwd.

Related: [[agent-security-boundaries]] · [[diff-quality-gate-gotchas]].
