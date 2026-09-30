---
id: memory-shapa-integration
type: memory
created: "2026-07-12T23:03:44Z"
consequence: 8
locus: output-meta
uses: 3
last_used: 2026-07-12T23:45:02Z
summary: Codebase memories write to git via .shapa, not the shared memri table; roukh-llm's hook invokes shapa nodes directly behind a private-keyword screen.
scope: global
status: active
---
Memory/shapa integration

Codebase memories write to git via `.shapa`, not to the shared cross-project `memri` table: the roukh-llm hook invokes shapa nodes directly, gated by a public-repo private-keyword screen. Repo-scoped memory belongs in the repo's own git history — it travels with clones/forks and is visible in-editor — while `memri`/`write_memory` remain the correct path for cross-cutting, DB-native memory (rules, personas). The two paths are additive, not a migration of one into the other.

## Relationships

- [[skills-workflow-library]] — both compile durable artifacts out of session/build history
- [[shapa-git-memory-integration]] — architecture reference for the write path and the privacy gate
- [[architecture]] — this capability belongs in the repo's architecture doc
