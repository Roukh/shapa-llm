---
id: coding-style-baseline
type: rule
created: "2026-05-09T18:50:04.444695+00:00"
consequence: 6
locus: output
uses: 0
summary: Global coding-style defaults - no gold-plating, immutable patterns, 200-400 line files, boundary-only validation, mechanical house-style enforcement.
scope: global
status: active
---
# Coding Style Baseline

Global coding-style defaults for any repo:
- No speculative additions or gold-plating: don't add features, refactors, or abstractions beyond what the task requires. A bug fix doesn't need surrounding cleanup. Three similar lines beat a premature abstraction. No half-finished implementations.
- Prefer immutable patterns: return new objects rather than mutating in place.
- File size: keep files 200-400 lines typical, 800 max. Organize by feature/domain; extract utilities from large modules.
- Validate only at system boundaries (user input, external APIs, file content). Trust internal code and framework guarantees. Never add error handling for scenarios that cannot happen.
- Any house-style constraint (em dashes, banned words, formatting) needs a mechanical post-pass or automated validation step, not just a prompt instruction: 15 sonnet prompt-writer agents given an explicit "no em dashes" instruction still produced 648 lines with em dashes across a page-prompt fan-out, fixed post-hoc with `sed`. Models regress to defaults under long-form generation, so don't rely on the instruction alone holding.

Source: memri b83a9d42-5e47-4dfb-94ae-401e3710a9f3, aa34cbe2-1765-4f63-bf57-2e9d08ad4fe2, af205ea4-7ebc-4d9e-88ec-0b4c186cb0fb, b95baadf-6386-4c48-af88-c3a90c6c1936
