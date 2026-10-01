---
id: placement
type: reference
created: "2026-09-30T00:00:00Z"
consequence: 7
locus: output
---

# placement.md — where a new note belongs

> Installed by `shapa init` alongside `AGENTS.md`. Read this before writing
> any new note, or before calling the MCP `save` tool - `scope` is a
> required, agent-made decision. There is no default; a write with no
> `scope` is refused rather than guessed (shapa-backend-spec.md §4.1).

## The decision rule

Ask one question about what you are about to write:

> **Would this still be true in a different repo, tomorrow?**

- **Yes** → `scope: global`. It belongs in the global wiki - a fact, rule,
  or preference that holds regardless of which project you're in (an
  operator preference, a cross-project workflow rule, a standing fact about
  the operator or the environment).
- **No, it's true only here** → `scope: repo`. It belongs in this repo's own
  wiki - something specific to this codebase, this project's conventions,
  or a decision that would be actively wrong advice in another repo.

There is no third option. `external` (a per-repo staging area inside the
global wiki, for a repo with no wiki of its own) was retired by operator
decision (shapa-backend-spec.md §10 decision 4/6): every project's memories
live only in that project's own `.shapa`, full stop. If the repo you're
writing about doesn't have its own wiki yet, that's a signal to run
`shapa init` there - not to park the note in the global wiki as a
workaround.

## Two traps this rule exists to catch

1. **"It's about repo X, so it must be repo-scoped."** Not necessarily -
   ask the actual question above, not the subject-matter question. "Always
   ask before force-pushing" is true everywhere; "this repo's CI needs
   `--legacy-peer-deps`" is true only here.
2. **"I'm not sure, so I'll make it global - more places will see it."**
   Wrong direction. An uncertain or narrow fact placed in the global wiki
   pollutes every *other* project's context budget on every session start.
   When genuinely unsure, prefer `repo` - a repo-scoped note that turns out
   to generalize can be promoted later; a global note that turns out to be
   repo-specific quietly misleads every other project until someone notices.

## What this does not decide

This is about *where a note lives*, not what `type` it is (`memory` / `rule`
/ `issue` - see `AGENTS.md` §2) or how to word it (§3.1's `summary`/`tags`
fields). Placement and content are independent decisions.
