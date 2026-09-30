---
id: roukh-llm-test-hangs
type: issue
created: "2026-07-13T03:19:17.518833+00:00"
consequence: 3
locus: meta
uses: 0
summary: Two hook test files hang from clean main; Node 22-vs-24 AbortSignal flake in mcp-server; never hand-derive epoch timestamps in audit briefs.
scope: global
status: active
---
# roukh-llm: test-suite hangs and verification-precision gotchas

- **destructive-guard / roukh-skill-gate hangs.** `scripts/hooks/tests/destructive-guard.test.sh` (hangs mid "Group 12: truncate false-positives") and `scripts/hooks/tests/roukh-skill-gate.test.sh` hang indefinitely partway through their own test groups, reproduced from a clean primary checkout on `main` — not caused by any specific PR. Root cause not investigated; likely a stray backgrounded process or an open stdin read. Every other bash hook test file in `scripts/hooks/tests/` passes cleanly. If a full test sweep hangs rather than fails, check these two files before assuming it's your change.
  Source: memri 08e2e346-9a6e-41c7-bdcf-1b51f934d37e

- **Node version mismatch flakes an AbortSignal test.** mcp-server's client.test.ts ("callBrainApi does not hang forever on a stalled fetch", a 25ms timeout wired via AbortSignal against a stalled-fetch mock) failed deterministically (3/3 repro) on local Node v22.23.1 with a fresh `npm ci`, while CI pins Node 24 — a Node 22 vs 24 AbortSignal/event-loop timing difference, not a real regression. If it resurfaces: pin the local dev Node to match CI exactly, or rewrite the test with fake timers instead of a wall-clock race.
  Source: memri 90b73112

- **Pin ISO timestamps, never hand-derived epoch.** Audit run `wf_a24ad89f` (open-trader) pinned a clean-restart clock as unix `1785856730` for ISO `2026-08-03T17:58:50Z` — the correct epoch is `1785779930`, a 21.3-hour error that produced roughly a dozen false-positive "pre-clock" findings. Rule: pin ISO date strings only in audit briefs, or if a numeric epoch is genuinely needed, pin it alongside the exact query that derived it so it can be checked — never hand-convert a date to epoch seconds and trust the conversion.
  Source: memri 68109cc8-4d14-4421-a729-cd6976a8e53d
