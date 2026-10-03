---
id: placement
type: rule
created: "2026-09-29T00:00:00Z"
consequence: 9
locus: meta
uses: 0
summary: Placement gate for .shapa notes - global (changes behavior in any repo) or local (that project's own .shapa, never parked in global).
scope: global
status: active
---
Placement rule: global vs. local

Every `.shapa` note lands in exactly one wiki. `memory.py save` enforces this as a gate — it errors unless the agent passes exactly one of `--global` or `--repo` — because placement is a decision, not a default.

**GLOBAL** (`roukh-llm/.shapa`) — a rule or fact that changes agent behaviour in ANY repo: workflow, tooling, the harness itself, hooks, skills, git/gh conventions, operator preferences, accounts. Also global when the note spans 2+ repos rather than belonging to one.

**LOCAL** (`<repo>/.shapa`): only true or useful inside that one project. This applies to every project, whether or not it's in the shared workspace (open-trader, ghobz_projects, lern, and so on). A project's memories live only in that project's own `.shapa`, and never in the global wiki (operator, 2026-09-30). If the project isn't checked out locally, ask; don't park its notes here.

When unsure whether something is global or local, ask: would this still be true if the agent were working in a different repo tomorrow? Yes → global. No → local, in that project's own `.shapa`.
