---
id: shapa-engine-safety
type: rule
created: "2026-08-01T00:45:16Z"
consequence: 8
locus: output
scope: repo
summary: Destructive maintain paths never trust frontmatter (arch/ protection is path-based since issue #5); headless claude calls need empty hooks, closed stdin.
---

Invariants for the engine's destructive paths (prune, merge, resolve), each learned from a real failure. Peers: [[shapa-init-and-discovery]], [[shapa-llm-identity]].

**arch/ protection is by path, not frontmatter.** Issue #5 (2026-08-01, high severity): heartbeat's orphan prune deleted two files in `open-trader/.shapa/arch/`. The cause was that `nodes.is_protected()` checked only `type: reference`. One file had `type` nested under a `metadata:` block, which the stdlib parser in `shapa/frontmatter.py` does not read since it handles flat keys and lists only. The other had no frontmatter at all. Both fell through to an empty type and had zero wikilinks. The fix, 6a4e5e2 (#6), made protection hold when a note is `type: reference` OR sits under `arch/`. Keep both signals, and never make a deletion guard depend on frontmatter an agent wrote. Source memri row: `archive/memri-2026-09-29-shapa-llm.json`.

**Headless `claude` from the engine.** `maintain --resolve` reconciles contradicting rule pairs through `claude -p --settings '{"hooks":{}}'`, with `stdin=subprocess.DEVNULL` and a neutral cwd. Without these the call blocks on stdin, or it inherits global hooks that write meta-output into the note. The reply must wrap the merged body in `<note></note>`. A guard rejects DISTINCT, empty, oversized or polluted output and leaves both notes untouched. `summary` backfill uses the same subprocess pattern.

**Never delete history.** `maintain --lean --apply` moves superseded notes into `archive/` (via `git mv` in a checkout) and never deletes. `archive/` and `attic/` are invisible to every reader.

[[rule]] [[issue]]
