---
id: architecture
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 7
locus: output
uses: 13
last_used: 2026-07-12T23:45:02Z
summary: The one live architecture reference for roukh-llm - shared skills library, runtime scripts, two native hooks, install flow.
scope: global
status: active
---

# Architecture — roukh-llm

> This is the one live architecture reference for roukh-llm. Schema:
> [[AGENTS]]. Agent-facing rules live in the repo's own `AGENTS.md`/
> `CLAUDE.md` (not a wiki node); `claude-contract` (archived,
> `status: superseded`) documents a build-loop stack this repo no longer
> runs.

## Overview

roukh-llm is a shared skills-and-hooks library for Claude Code and Codex, not
a running service. `sh install.sh` runs `scripts/install.py`, which symlinks
`skills/` into each harness's skill directory, merges two native hooks into
Claude's `.claude/settings.json` and Codex's `hooks.json`, and renders the
Claude output style from [[response-style]]. There is **no brain API, job or
task queue, registration gate, research-checkpoint, or mandatory
review-loop service** — the earlier flow-feat build→review→merge loop, its
DB-backed jobs queue, the in-repo MCP server, `claude-runner`, the Hermes
agent runtime, the Telegram bridge, and VPS install/ops were all built during
an earlier push and later removed (see Key decisions).

## Components

- **`skills/`** — the skill library, by category (`clerk/`, `engineering/`,
  `firecrawl/`, `hermes/`, `in-progress/`, `misc/`, `productivity/`,
  `roukh-skill/`, `skill-restructure/`, `workflow/`). See
  [[skills-architecture]].
- **`scripts/install.py`** (invoked by `install.sh`) — the sole installer.
  Symlinks every `skills/**/SKILL.md` directory into `.claude/skills/`,
  `.agents/skills/`, and (with `--user`) `~/.agents/skills/` for Codex; merges
  its two managed hook entries into `.claude/settings.json` / Codex's
  `hooks.json` (tagged, so a re-install replaces its own entries rather than
  duplicating); writes the Claude output style from `.shapa/response-style.md`.
  `--check` verifies without writing.
- **`scripts/runtime/guard.py`** — the `PreToolUse` hook
  (`Bash|Edit|Write|MultiEdit|NotebookEdit|apply_patch`): local write-scope and
  destructive-command checks (git worktree root + `ROUKH_ALLOWED_ROOTS`),
  supplementing the native sandbox.
- **`scripts/runtime/memory.py`** — the `SessionStart` hook (`bootstrap`,
  bounded output) plus a `search`/`save` CLI onto the `.shapa` markdown wiki
  (this wiki, plus any repo's own `<repo>/.shapa`) — durable notes are files,
  not database rows. Also gives direct, read-only CLI access to the brain
  DB's `projects` table (`ROUKH_DB_ENV` / `roukh-brain/.env`); the retired
  `memri` table is no longer read or written.
- **`.shapa/`** — this durable-memory wiki, a stdlib-only Python engine.
  Schema: [[AGENTS]]; placement rule: [[placement]].

## Data & state

No application database. The only durable state this repo touches: `.shapa`
markdown files (this wiki plus optional per-repo wikis) and, read-only, the
brain DB's `projects` table via `memory.py`. `.claude/settings.json` is the
one `.claude/*` path that's git-tracked; Claude Code's config dir points at
this repo's `.claude/` — see [[claude-config-symlink]] for the mechanics.

## Key decisions

- **The service stack was cut.** flow-feat (the autonomous build→review→merge
  loop), the DB-backed jobs queue, the in-repo MCP server, `claude-runner`,
  the Hermes agent runtime, the Telegram bridge, and VPS install/ops all
  existed at points in this repo's history and were removed; the repo
  returned to being a skills+hooks library only. [[claude-dir-architecture]]
  documents that retired stack; `loop-lifecycle-v2-spec` (archived) is its
  as-built record. Do not use either as a guide to current behavior.
- **Memory moved from a DB table (`memri`) to git-tracked `.shapa` notes** —
  see [[memri-migration]], in flight as of the current top fires
  ([[agenda]]).
