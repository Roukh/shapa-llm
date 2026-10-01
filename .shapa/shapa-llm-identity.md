---
id: shapa-llm-identity
type: memory
created: "2026-06-30T17:33:00Z"
consequence: 7
locus: output-meta
scope: repo
summary: shapa-llm is the public open-source shapa tool repo - zero DB coupling at runtime, but governed like every repo; anything committed here goes public on push.
---

What this repo is, how it relates to the roukh-llm brain, and what that means for every change made here. Peers: [[shapa-init-and-discovery]], [[shapa-engine-safety]].

**The tool.** shapa = Self-Healing Autonomous Persistent Agent: a markdown-graph *operational* memory for an LLM agent. A note's value is its effect on the consuming LLM, not topic coverage. Types are MemRI (memory, rule, issue) plus reference. The core is pure stdlib; `[semantic]` (model2vec) and `[mcp]` are optional extras.

**Runtime vs governance.** "Standalone" describes the runtime only: shapa has zero DB/infra coupling, and brain-DB ↔ wiki coherence must be an external integration, never shapa core (operator, 2026-07-10). It does not exempt the repo from governance. An early call that shapa-llm was exempt was corrected by the operator on 2026-07-10: shapa-llm is registered as a repo under subcomponent workspace (project ORRERY) with its own repo-level macro.

**Public remote.** `github.com/Roukh/shapa-llm`, branch `main`, git identity Roukh. The repo is public, so everything tracked here, including this `.shapa/`, is published on push. Keep secrets, client names and other projects' details out of these notes.

**Superseded designs** (do not resurrect): the v0.6 model where memory lived only outside the repo (`~/.shapa/memory`, repo `wiki/` holding just `arch/`), the `uses` frontmatter counter, `sentence-transformers` embeddings, and the 2026-07-10 "one repo-root wiki, no global/project split" decision. The current model is global plus repo dual-wiki reads with scope-gated writes. Source: Claude Code auto-memory of 2026-06-30 and 2026-07-10, curated 2026-10-01.

[[memory]]
