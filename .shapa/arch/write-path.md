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
- `shapa/ledger.py`'s `correction_main`/`record_correction` - UserPromptSubmit hook that turns an operator correction into an I row, notes every operator message's frustration signal (never the text), then hands the row to `learn.on_correction`.
- `shapa/learn.py` - after the row: repeat detection against the repo and global wikis' issues and rules (tags, a `related` link, counts, a violation of a learned principle), then `shapa learn pending|apply|dismiss|merge|principles`, the only writers of principles (rule rows tagged `principle`). A principle for a skill is a rule tagged `skill:NAME`; the existing lesson mechanism files a pending version and only `shapa skill activate` makes it live.

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
- A correction ("no", "wrong", "not like this" and the like) becomes an I row linked (`about`) to the work items claimed when it was written. Only the hook files it with source `correction` (a `--text` filing is `correction:manual`; `shapa row add` refuses both reserved sources, normalised for case, spaces and zero-width characters). No source gives a correction authority: nothing in `learn` activates a skill version. A short statement made while a ledger item is claimed is filed the same way tagged `soft` (not a question, slash command or bare approval; 10 unapplied per session) and counts in no measure until applied; `learn dismiss` deletes it. The prompt path waits at most 1 s on a lock and skips silently.
- A repeat is never a new principle: the earlier row counts it (`repeats:N`, or `violations:N` on a principle), a principle revised 3 times and violated again is `churn` and refuses a 4th revision, a learned correction never reappears in `learn pending`, and corrections about the learning system are applied at most 3 a week.
- A principle goes where the caller says (`--into R<n>` merges, evidence appended; `--new` adds; neither with candidates present is exit 2 listing them), at most 60 per wiki, never into a churned one, and never beside one it is near (`--distinct-from R<n>` claims a different subject; `learn unchurn R<n>` is the operator's reset; `blocked` is tagged only on a detected repeat of a churned principle). It is one flattened, masked line wherever printed (an instruction-like phrase only adds a warning), and a contradiction is filed beside the rule linked `conflicts`. The tags the loop writes are reserved (`db.RESERVED_ROW_TAGS`): `row add|tag`, `save` and the local MCP refuse them; a synced principle keeps them as data but only a principle in this device's `p_local` ranks by its counters.
- Repeat detection reads a derived index in `.shapa-index.db` (`l_chunks`, `l_tok`, `l_vec`; a rowid watermark; the hook path catches up at most 1,500 rows a call, the sweep and `learn pending` the rest), plus a mirror of other repos' unlearned corrections in the global wiki's index; nothing in it is synced.
- `scope` is required on every write and never guessed (`global` or `repo`); `shapa save` errors clearly on a bad combination instead of swallowing it.
- Hooks never block: always exit 0, and never write inside this tool's own repo or installed package.
