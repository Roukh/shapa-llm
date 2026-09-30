---
id: ui-library-registry-method
type: rule
created: "2026-06-01T21:37:18.735953+00:00"
consequence: 7
locus: output
uses: 0
summary: Never build UI from scratch - adapt from a project's docs/component-registry.json visual_sources; feature_references are UX inspiration only.
scope: global
status: active
---
# UI Library Registry Method

Rule: never build UI components from scratch — LLMs produce structurally correct but visually mediocre UI from nothing. Always adapt from a proven implementation instead.

Method: each project keeps its own registry at `docs/component-registry.json` (monorepos: `docs/<subproject>/component-registry.json`), schema:
```
{ visual_sources: [{ name, access: local|firecrawl|mcp, path, components[] }],
  feature_references: [{ name, url, notes }] }
```
- visual_source = clone/copy the actual component code and adapt (tokens, data model, props — not a rewrite).
- feature_reference = UX-pattern inspiration only, never copy code.

roukh-skill Agent 0 reads this file before any UI build; if missing, create it first. Do not hardcode project-specific library lists into roukh-skill's own SKILL.md — the skill defines the method, the per-project registry file holds the data. If no visual_source matches a needed surface: flag it as a library gap, build from scratch as last resort, then add the new entry to the registry.

(Historical, ghobz_dashboard-specific: bundui/shadcn-ui-kit-dashboard was cloned in full — 100+ components already installed under `components/ui/`; animate-ui.com for motion primitives via firecrawl-scrape; shadcn MCP for base primitives; attio.com as the CRM UX gold-standard feature reference, not a visual source.)

Source: memri 5d8f45fa-302f-430b-9c36-c45a11b7872e 5a4e278b-01f0-46bd-8778-1c409d45abc4 2a061bdf-96e9-4856-805d-a6a8f2eefe53 9e97d70f-e9c5-4835-b21a-43b2949611ae
