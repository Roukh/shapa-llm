---
id: architecture
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 7
locus: output
uses: 0
---

# Architecture — <your project>

> Template installed by `shapa init`. Describe how the system is built so an
> agent can navigate the codebase without re-deriving it every session.
> Sibling docs: [[PRD]] · [[system-design]]. Schema: [[AGENTS]].

## Overview

The shape of the system in one diagram or paragraph: the major components and
how they talk to each other.

## Components

For each major module/service: its responsibility, its public interface, and
what it depends on. Keep responsibilities single and boundaries explicit.

## Data & state

Where state lives, the source of truth, and how data flows through the system.

## Key decisions

The load-bearing choices and *why* they were made — the context a future agent
needs before changing them. Record reversals here too.
