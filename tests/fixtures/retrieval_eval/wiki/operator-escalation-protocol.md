---
id: operator-escalation-protocol
type: rule
created: "2026-07-02T16:22:17.263744+00:00"
consequence: 9
locus: meta
uses: 0
summary: Never skip a blocking operator question; research ambiguity before asking; never persist durable state on your own initiative without asking first.
scope: global
status: active
---
# Operator Escalation Protocol

Standing rules from the operator's 2026-07-02 directive on ambiguity and blocking questions, plus the durable-write rule they subsume:

1. **Never skip a blocking operator question.** When a gated action's question times out or the operator is AFK, do not proceed on best judgment. Park that slice, keep working on independent non-gated work, and re-surface the question at the next operator contact or as the top item of the final report. Gated = merge authorization, macro/criteria changes, task placement, destructive/outward-facing ops, any strategy-level choice. The operator runs multiple projects; silence means busy elsewhere, not consent.
2. **Resolve ambiguity via research before guessing or asking.** When task intent, scope, or ground truth is unclear, dispatch research (codebase explore, git/PR history, web) to resolve it BEFORE guessing and BEFORE asking the operator. Only escalate ambiguity that survives research (genuine preference/strategy calls).
3. **Ask before durable writes.** Don't proactively persist state (memory notes, tasks/tickets, logs) on your own initiative. Draft it, show it, wait for confirmation — push only after approval. If the operator explicitly instructs a write/read, follow directly without re-asking.

See also [[approved-work-full-parallel-dispatch]] for the complementary rule once work IS approved.

Source: memri 5c1d7f55-9070-4d3f-bc84-3b5bc4ade205, 86441337-3e7a-48f2-9ab9-eeabc0bb2eb4, 1ee4f140-f2fd-4a22-9100-2ff462f30df9, fbba5063-4ea8-429e-950e-3fb626ef7d85
