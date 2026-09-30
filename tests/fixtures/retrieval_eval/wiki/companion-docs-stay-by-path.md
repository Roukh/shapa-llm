---
id: companion-docs-stay-by-path
type: rule
created: "2026-07-13T02:25:27.69401+00:00"
consequence: 7
locus: meta
uses: 0
summary: By-path tool-contract docs (skill/plugin companion .md sidecars) are exempt from wiki-migration placement policies.
scope: global
status: active
---
# Companion Docs Stay By Path

A blanket "all markdown lives in the wiki" placement policy must exempt files loaded **by path** as part of a tool contract, even though they look like ordinary docs:

- Skill companion docs — `skills/**/*.md` sidecars a `SKILL.md` loads for progressive disclosure (e.g. `BLIND-SPOTS.md`, `WORKFLOWS.md`, `ADR-FORMAT.md`, `DEEPENING.md`). Migrating these into a wiki breaks the skill that reads them by path.
- Plugin companions — `plugins/**/*.md`, same reasoning as `SKILL.md` itself but a different directory root; a glob that only covers `skills/**/SKILL.md` misses these entirely.
- Data/rules sidecars read by code, not prose meant for humans (e.g. a `task-rules.md` a script parses) — classify as data, not narrative to migrate.

Rule: when writing a placement/allowlist policy for a wiki like `.shapa`, widen any glob-based exemption to the whole directory class (`skills/**/*.md`, `plugins/**/*.md`), and check whether a "stray markdown" file is actually a by-path tool-contract load before moving it.


Source: memri 83f23ec0-d8e6-4bc2-81ee-b0ca7e9b84e7
