---
id: skills-workflow-library
type: memory
created: "2026-07-12T23:03:44Z"
consequence: 8
locus: output-meta
uses: 1
last_used: 2026-07-12T23:34:46Z
summary: Workflow-mining v1 mines merged-PR task history for repeated multi-file change shapes and turns them into invocable skills; build history becomes playbooks.
scope: global
status: active
---
Skills & workflow library

Workflow-mining v1 mines real merged-PR task history for repeated multi-file change shapes and turns them into invocable skills (`session-hook-wiring`, `installer-hardening`) — build history becomes reusable playbooks. Status: met (evidence: PR #172, #174; commit `12fb5f68`). Both mined skills still exist in the current skill set.

## Relationships

- [[agent-services-mcp-runner-hermes]] — the mined skills are executed by these agent runtimes
- [[memory-shapa-integration]] — both compile durable artifacts out of session/build history
- [[architecture]] — this capability belongs in the repo's architecture doc
