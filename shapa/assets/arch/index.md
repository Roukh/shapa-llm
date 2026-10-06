---
id: index
type: reference
created: "2026-10-05T00:00:00Z"
consequence: 7
locus: output
summary: The system as a diagram - one row per box, one row per edge; each box has its own file in arch/.
---

# Architecture index

> Installed by `shapa init`. For the agent, not for humans: rows, not prose.
> One file per box (`arch/<box>.md`): purpose, owned paths, interfaces in and
> out, invariants. Gotchas are issue rows tagged with the box name, never arch
> text. Keep the box count small; a box nobody touches gets merged away.

## Boxes

| Box | File | Owns | Purpose |
|---|---|---|---|
| example | [[example]] | `src/example/` | one line |

## Edges

| From | To | What crosses |
|---|---|---|
| example | db | rows written per request |
