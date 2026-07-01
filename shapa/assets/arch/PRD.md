---
id: PRD
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 7
locus: output
uses: 0
---

# shapa — PRD

> **shapa** = **S**elf-**H**ealing **A**utonomous **P**ersistent **A**gent.
> Status: reframed · 2026-06-30 · standalone open-source repo.

Related design docs: [[memri-spec]] · [[hook-design]] · [[MACRO]]. (The arch docs are a separate cluster from the operational memory graph.)

---

## 1. What shapa is

shapa is an **internal LLM tool**: the **self-healing, persistent operational memory of an LLM agent**. The wiki is the agent's memory of *how to operate* — not a knowledge base about external topics.

Every node is a **note-to-self**, one of three kinds (the **MemRI** framework):

- **Memory** — what happened or what was learned in a session.
- **Rule** — a standing instruction on operation, promoted from a memory.
- **Issue** — a flagged problem, contradiction, or gap to resolve.

Nodes are captured **live at the end of an agent's work** ("always meta"): the agent reflects on the session just finished and writes nodes about its own operation.

**Scoping principle (non-negotiable):** shapa does **not** research or accumulate knowledge about topics. A node exists only if it affects the **performance of the LLM that uses this wiki as memory**. The unit of value is *effect-on-the-consuming-LLM*. Anything that does not change how well the agent operates does not belong in the wiki.

## 2. Problem

LLM agents are effectively stateless across sessions. They relearn the same operational lessons, repeat corrected mistakes, and lose hard-won rules the moment the context window closes. Document-RAG tools retrieve *topic* knowledge but capture none of this *operational self-knowledge*, and a naive memory store rots: stale rules linger, notes orphan, contradictions accumulate, and low-value clutter dilutes the agent's context.

shapa is operational memory that **maintains itself**, so the signal stays high and the agent actually gets better at operating over time.

## 3. How it functions

A persistent, self-maintaining memory graph:

1. **Capture** — at the end of any agent's work, distill the session into MemRI nodes (write-only for now).
2. **Connect** — nodes link through **`[[wikilinks]]` written in the body**. Two files are connected when one links to the other.
3. **Heal** — a **heartbeat** keeps the graph healthy. First rule: a node connected to nothing (an orphan) is **pruned**. More health rules will follow.
4. **Score** *(built)* — each node carries a **value score = its consequence for the consuming LLM**, computed from `locus_weight × (consequence/10) × freshness × use_factor`. Used to rank what to surface and what to retain versus prune.
5. **Serve** *(built)* — the fetch hook (`shapa/fetch.py`, wired by `install.sh` on `UserPromptSubmit`) surfaces the most relevant, highest-scored notes at the start of each prompt and calls `record_use` on each. Scoring is live: the `uses` counter is now incremented from real reads.

## 4. Principles

- **Value = effect on the consuming LLM's performance.** No topic research for its own sake.
- **Notes-to-self only** — memory, rule, or issue. Nothing else is a node.
- **Uniform format** — all wiki files (the `arch/` design docs and the root operational notes) share one frontmatter schema (`id`, `type`, `created`, `consequence`, `locus`, `uses`); the body is free markdown; connections are `[[wikilinks]]` in the body.
- **Self-healing** — the memory prunes and maintains itself; humans do not babysit it.
- **Honest and safe** — pruning is deterministic and connectivity-based; capture never fabricates; no silent destructive actions.
- **Private by default** — the wiki is external to the repo (connected by `shapa init`), so a user's notes are never git-tracked at all; the tool ships only the engine and the `arch/` design-doc templates. Privacy is structural, not gitignore-based.

## 5. What is built (current state)

- The MemRI file format and the rules document (`shapa/assets/AGENTS.md`, installed into the wiki root by `shapa init`).
- The engine (pure Python stdlib, zero runtime deps): frontmatter parser, **wikilink graph builder** (`nodes.py`), **heartbeat** (random-walk pulse over the wikilink graph + orphan prune), **validator** (frontmatter-schema: F01–F03, S01–S03), **scoring** (`score.py`).
- The `shapa` CLI (`cli.py`) incl. `init` — connects an external wiki, installs the design docs, and records the path; the external-wiki privacy model (`config.py`). Test suite + fixtures. `install.sh` (finds the Claude config and wires the fetch/capture/maintain hooks against the connected wiki; verified in a sandbox, not yet run on a live config).

## 6. What is next

- **Capture hook** — Stop/SubagentStop hook that writes notes at the end of agent work (see [[hook-design]]). `install.sh` wires the heartbeat (Stop) and the fetch read hook (UserPromptSubmit); the capture hook is not yet built.
- **More heartbeat rules** — health checks beyond orphan-pruning, informed by the score.

## 7. Success criteria

1. A finished session yields valid MemRI nodes that capture operational lessons, each of which plausibly affects how the agent works.
2. The heartbeat keeps the graph healthy — orphans pruned, connected nodes retained — **deterministically and verifiably** (covered by tests).
3. Once scoring lands, every node carries a value score reflecting its consequence to the consuming LLM, and retention/surfacing decisions use it.
4. A user's memory stays private; clones ship an empty wiki; the engine runs with zero runtime dependencies.
5. No fabricated capture and no destructive surprises — every prune traces to the orphan rule.

## 8. Scope boundaries

**In scope:** operational-memory capture, maintenance, and scoring for an LLM agent; the self-healing graph; the engine; the capture hook.

**Out of scope:** topic research or general knowledge accumulation; raw-source ingestion; RAG-over-documents; any external database coupling; a GUI (Obsidian is the viewer, the agent is the writer).

## 9. History

An earlier, broader vision — a research-grade knowledge base with *divergence-from-path* and *hallucination-grounding* meters over immutable raw sources — was explored first and then **pruned** when shapa was reframed as an LLM agent's operational memory (there are no raw sources; the nodes *are* the memory). Those design docs (`architecture.md`, `grading-spec.md`, `self-heal-loop.md`) were removed to keep `arch/` coherent; they remain in git history. The node-scoring model (see [[memri-spec]]) replaced the old meter design.
