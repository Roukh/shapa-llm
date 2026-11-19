---
id: read-without-reading
type: reference
created: "2026-10-08T00:00:00Z"
consequence: 6
locus: output-meta
scope: repo
summary: Can an agent obey a rule without the rule taking context? On a hosted model, no; five mechanisms that come close, ranked by window cost.
---

# Read without reading (research 2026-10-08)

## Question

Operator, 2026-10-09: compress or encrypt rules across features and databases, decrypt one only when it is needed, without the decrypt costing context ("read without reading"). Can an agent use a rule that is not in its window?

## Method

Premise check of the cited model work (DeepSeek), then a survey of the ways an agent harness can apply a rule at less than its full token cost, each scored on main-window cost, whether it works on Claude today, and whether it is model-independent.

## Findings

- On a hosted model, no: the tokens in the window are the model's only input, so anything it acts on is in its window at that moment (high confidence).
- Encryption makes it worse: ciphertext is incompressible and tokenizes at about 1 token per 1-2 characters, and the model cannot run AES in its head (it would need ciphertext, key and working in context). Encryption at rest, decrypted by shapa before injection, protects secrets and does nothing for context size.
- DeepSeek-V4.1-Flash reuses the KV cache across layers in one forward pass and stores it in FP4 (890 bytes a token of global KV, about 1/4 of V4-Flash): it shrinks one request's attention state, not a store of facts shared across sessions. Engram (hashed N-gram tables inside the transformer, O(1) lookup, replacing about 20% of MoE parameters) is the closer analogue and lives in the weights. Both need model internals; neither is reachable through the Claude or OpenAI APIs.

| # | Mechanism | Main-window cost | On Claude today | Model-independent | Limit |
|---|---|---|---|---|---|
| 1 | Enforce outside the model: a hook checks each tool call or output against the rule and denies with the rule's text only on a violation | 0 until a violation | yes | yes | only rules expressible as a check on an action |
| 2 | Read in another context: a side call (subagent or cheap model in a hook) reads the rules plus the proposed action and returns a verdict or the 1-2 rules that apply | the verdict only | yes | yes | extra latency and tokens per check, outside the main window |
| 3 | Provider prompt cache: rules as a stable cached prefix | full | yes | per provider | cuts price (cache reads 0.1x input on Anthropic) and latency, not window use |
| 4 | Compressed text the model reads natively: token pruning (LLMLingua-2, about 2-5x), dense rule notation, optical compression | 1/2 to 1/5 | yes | yes | adherence can drop; optical gain on Claude unmeasured (an image costs width x height / 750 tokens) |
| 5 | Latent compression: gist tokens, ICAE, xRAG, KV-cache injection, LoRA or fine-tuning | near 0 | no | no | needs open weights and self-hosting, redone per model |

- Open: the operator's second idea, more dimensions on each memory row for sharper fetches, was held for their reply.

## Sources

- DeepSeek-V4.1-Flash: https://arxiv.org/abs/2609.19969
- Engram: https://github.com/deepseek-ai/Engram , https://arxiviq.substack.com/p/conditional-memory-via-scalable-lookup
- DeepSeek-OCR, optical context compression: https://arxiv.org/abs/2510.18234 (from model knowledge, not re-fetched)
- LLMLingua-2: https://arxiv.org/abs/2403.12968 (from model knowledge, not re-fetched)
- Anthropic vision and prompt-caching pricing pages (from model knowledge, not re-fetched)

## Date

2026-10-08 (feature F21, "Rules stay in force as a session grows").
