---
id: system-design
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 6
locus: output
uses: 0
---

# System Design — <your project>

> Template installed by `shapa init`. The concrete, buildable design: the level
> below [[architecture]] where interfaces, schemas, and flows are pinned down.
> Sibling docs: [[PRD]] · [[architecture]]. Schema: [[AGENTS]].

## Interfaces & contracts

APIs, function signatures, message shapes — the contracts other components rely
on. Breaking one of these is a breaking change; note it.

## Data model

Schemas, types, and invariants. What must always hold true of the data.

## Sequences

The important flows, step by step (request → … → response). One per critical
path.

## Failure modes

What can go wrong at each step and how the system degrades — the edges an agent
must respect when changing this code.
