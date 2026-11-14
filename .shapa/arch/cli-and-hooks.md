---
id: cli-and-hooks
type: reference
created: "2026-10-05T20:22:00Z"
consequence: 7
locus: output-meta
scope: repo
summary: shapa.cli dispatches every subcommand; maintain/heartbeat/score/validate/upgrade are the self-healing and format tooling; mcp.py is the vendor-neutral surface.
---

# CLI and hooks

The unified `shapa` command, and the harness hook wiring that calls it
automatically instead of a human typing it.

## Owns

- `shapa/cli.py` - subcommand dispatch (`bootstrap`, `fetch`, `capture`, `save`, `maintain`, `heartbeat`, `score`, `validate`, `serve`, `mcp`, `upgrade`, `get`, `status`, plus `ledger`/`row`/`correction`/`doc`).
- `shapa/docs.py` - `shapa doc add|edit|rm|list|merge` (documents, kind D; `add` takes `--project` and `--merge`: one document per project); MCP `save` and `shapa save --type` take `document` and `idea`.
- `shapa/focus.py`, `shapa/skills.py` - `shapa focus ...` and `shapa skill ...`; MCP tools `focus` and `skill` in `shapa/mcp.py`. `shapa bootstrap` prints the focus by default and `--full` the old overview.
- `shapa/maintain.py` - prune orphans/stale, auto-merge near-duplicates, resolve contradicting rules (`claude -p`, on demand), lean-shape reporting, legacy-counter backfill.
- `shapa/heartbeat.py`, `shapa/score.py` - the maintenance walk and the value-ranking formula it and the read path both use.
- `shapa/validate.py`, `shapa/frontmatter.py`, `shapa/nodes.py` - frontmatter/lean-shape checks for the files that remain (`arch/`, `research/`).
- `shapa/upgrade.py` - brings a wiki to the current format; wraps `migrate4.step_database` as one of its mechanical steps.
- `shapa/status.py` - `shapa doctor`/`status`.
- `shapa/mcp.py` - the MCP stdio server.
- `shapa/remote.py`, `remotesync.py`, `remotehttp.py`, `sync.py`, `syncapply.py`, `syncrec.py`, `syncstate.py`, `dbsync.py` - `shapa remote` (login, sync, status, devices, logout, instructions), the opt-in client of the hosted store; contract in `hosted.md`.

## Interfaces in

- `python -m shapa <subcommand>` (or the installed `shapa` entry point).
- Hook stdin JSON from the harness: SessionStart, UserPromptSubmit, Stop, SubagentStop, PostToolUse.

## Interfaces out

- Stdout text or JSON per subcommand; `hookSpecificOutput` envelopes for the hook-facing subcommands.

## Invariants

- `shapa maintain` only ever prunes, merges or resolves on an explicit operator invocation, or the Stop hook's own `--prune`; an agent session never runs a non-dry-run heartbeat or `maintain --prune` directly against a live wiki.
- `shapa validate --all-roots` checks file frontmatter (`arch/`, `research/`) and the lean caps (40 live root notes, 12 `arch/` files, 250 KB); ledger items and MRI rows are validated by the database layer, not this path.
- `shapa upgrade [--all] [--check]` is idempotent; a wiki marked ahead of the installed shapa is never touched; exits 1 while any wiki in scope is behind.
- `shapa mcp` is the vendor-neutral surface non-Claude-Code harnesses reach for `search`/`get`/`save`; it falls back to a stdlib JSON-RPC shim without the `mcp` SDK extra.
- `shapa remote sync --hook` (SessionStart/SessionEnd) is held to 8 seconds in a worker thread, swallows every error, always exits 0 and prints at most one line; not logged in, it does nothing.
- `capture` and `maintain`, run as hooks, never block a session (always exit 0) and never write inside this tool's own repo or installed package.
