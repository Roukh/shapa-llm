---
id: write-path
type: reference
created: "2026-10-05T20:22:00Z"
consequence: 8
locus: output-meta
scope: repo
summary: Write path - Stop/SubagentStop capture and UserPromptSubmit correction distil a session into redacted M/I rows; shapa save is the explicit path.
---

# Write path

Turns a finished session, or an explicit call, into new memory/issue rows.
Capture and correction are automatic hooks that must never block the agent.

## Owns

- `shapa/capture.py` - Stop/SubagentStop hook: distils the session's final job report and the operator's own messages into M rows.
- `shapa/redact.py` - credential-shaped redaction, run before any hash, vector or write.
- `shapa/save.py` - `shapa save`, the explicit CLI write path (the MCP `save` tool's counterpart).
- `shapa/memlog.py` - record shaping (`make_record`) shared by capture and save; also the derived index that mirrors database rows for recall.
- `shapa/ledger.py`'s `correction_main`/`record_correction` - UserPromptSubmit hook that turns an operator correction into an I row.

## Interfaces in

| Caller | Shape |
|---|---|
| Stop/SubagentStop hook | stdin JSON (session transcript path) |
| UserPromptSubmit hook | stdin JSON `{"prompt": ...}` (correction path) |
| `shapa save` | CLI flags (`--summary`, `--body`, `--scope`) |
| MCP `save` | tool call |

## Interfaces out

- New M/I rows in `shapa.db` (`db.add_row`); hooks print nothing back to the agent.

## Invariants

- Capture writes only rows distilled from the operator's own messages and the session's final job report; it never stores the raw first prompt or raw tool output.
- Redaction runs before any hash, vector or write - a record can never carry a recognized secret shape.
- SubagentStop capture is skipped by default: a subagent's report is an intermediate product for the parent agent, not a session job report.
- A correction ("no", "wrong", "not like this" and the like) becomes an I row linked (`about`) to the work items claimed when it was written.
- `scope` is required on every write and never guessed (`global` or `repo`); `shapa save` errors clearly on a bad combination instead of swallowing it.
- Hooks never block: always exit 0, and never write inside this tool's own repo or installed package.
