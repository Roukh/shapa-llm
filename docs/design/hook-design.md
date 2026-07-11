---
id: hook-design
type: reference
created: "2026-06-30T20:00:00Z"
consequence: 6
locus: output
uses: 0
---

# Hook Design — Automated MemRI Capture

> **STATUS (TWO HOOKS):**
> - **FETCH hook (BUILT):** `shapa/fetch.py`, registered by `install.sh` on `UserPromptSubmit`. Surfaces the most relevant, highest-scored notes from the connected wiki root at the start of every prompt and calls `record_use` on each surfaced note. Sandbox-verified; not yet run on a live config.
> - **CAPTURE hook (HEURISTIC VERSION BUILT):** `shapa/capture.py`, wired by `install.sh` on `Stop`/`SubagentStop`. It writes a heuristic memory note per session (stdlib only, no LLM salience judgement). This document specifies the *fuller, LLM-distilled* capture design that would layer on top; that richer version is not yet built.
>
> See [[memri-spec]] §8 for the full built-vs-deferred breakdown.
> See also: [[PRD]] · [[memri-spec]] · [[MACRO]].
>
> Doc source: https://code.claude.com/docs/en/hooks.md

---

## 1. Goal (this document: the CAPTURE hook)

The capture hook is the second of two hooks in the shapa system. The first — the **fetch hook** (`shapa/fetch.py`, `UserPromptSubmit`) — reads relevant notes at the start of each prompt. The capture hook handles the *write* side: every time an agent finishes work — main agent or subagent — the system reflects on what happened and writes one or more MemRI nodes into the connected wiki root. A heuristic version is built (`shapa/capture.py`); this document specifies the fuller LLM-distilled design. This closes the "always-meta" discipline at the infrastructure level: the agent no longer has to remember to do it, and no session ends without a node.

The capture hook is **write-only and always exits 0**. It never blocks the agent from proceeding, never injects memory back into context, and never re-runs in response to its own writes.

---

## 2. Event Registration

There is no single Claude Code event for "any agent finished." Two events must be registered:

| Event | Fires when | Matcher support |
|-------|-----------|-----------------|
| `Stop` | Main agent ends a turn | No matcher field (fires unconditionally) |
| `SubagentStop` | Any subagent finishes | Supports a matcher; empty string = all subagents |
| `SessionEnd` (optional) | Session is torn down | Supports a matcher; use `"clear"` for teardown sweep |

**Do NOT register on `SessionStart`.** Memory is read back per-prompt via `UserPromptSubmit` (the fetch hook), not at session startup. Registering the capture hook on `SessionStart` would add startup latency for no benefit and conflates it with the fetch hook's responsibility.

The `SubagentStop` event with an empty matcher catches every subagent — tool-use agents, parallel agents, and orchestrated sub-tasks alike. `Stop` catches the orchestrator itself. Together they cover every agent boundary.

`SessionEnd` with matcher `"clear"` is optional: it provides a teardown sweep to catch any session that exits without a proper `Stop` (e.g. due to a crash or forced quit). If included, the extractor must be idempotent — writing a duplicate node for a session already covered by `Stop` is harmless but wasteful.

---

## 3. settings.json Shape

Hooks are registered in the `hooks` key of `~/.claude/settings.json` (user-level) or the project-level `.claude/settings.json`. User-level is recommended so the capture hook fires across all projects using shapa.

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/shapa-llm/hooks/capture.py",
            "timeout": 60
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/shapa-llm/hooks/capture.py",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

`Stop` does not support a `matcher` field — the outer object's `matcher` key is accepted by the schema but ignored; the hook fires on every stop. `SubagentStop` honours the matcher; an empty string matches all subagents.

`timeout` is set to 60 seconds. The hook reads a transcript `.jsonl` file and writes a few markdown files — well within budget. If the hook exceeds the timeout it is killed; set higher only if transcript parsing proves slow on very long sessions.

---

## 4. install.sh — Deep-Merge Pattern

`install.sh` must **deep-merge** the new hook entries into the existing `settings.json` rather than overwriting the file. A naive overwrite would destroy any hooks already registered by the user or other tools.

The correct pattern uses `jq` with `+=` to append to the existing array (or initialise it if the key is absent):

```bash
#!/usr/bin/env bash
set -euo pipefail

SETTINGS="${HOME}/.claude/settings.json"
CMD="$(realpath "$(dirname "$0")/hooks/capture.py")"

# Create settings.json if it does not exist.
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

jq --arg cmd "$CMD" '
  .hooks.Stop += [
    {
      "matcher": "",
      "hooks": [
        {"type": "command", "command": $cmd, "timeout": 60}
      ]
    }
  ]
  | .hooks.SubagentStop += [
    {
      "matcher": "",
      "hooks": [
        {"type": "command", "command": $cmd, "timeout": 60}
      ]
    }
  ]
' "$SETTINGS" > "${SETTINGS}.tmp" && mv "${SETTINGS}.tmp" "$SETTINGS"

echo "shapa capture hook installed → $CMD"
```

Key properties of this approach:

- `+=` on an array appends; if the key is absent `jq` initialises it as `[]` before appending, so no pre-check needed.
- The file is written atomically via a temp file and `mv` to avoid leaving a partial JSON file if the process is interrupted.
- Existing hooks under `Stop` or `SubagentStop` from other tools survive unchanged.

A future `uninstall.sh` would use `jq 'del(.hooks.Stop[] | select(.hooks[].command == $cmd))'` to remove only the shapa entries.

---

## 5. Hook stdin — What the Hook Receives

Claude Code delivers a JSON object to the hook's stdin on each invocation. The fields differ slightly by event.

**Stop fields:**

| Field | Type | Description |
|-------|------|-------------|
| `hook_event_name` | string | `"Stop"` |
| `session_id` | string | Unique session identifier |
| `transcript_path` | string | Absolute path to the session transcript `.jsonl` file |
| `cwd` | string | Working directory at the time of the stop |
| `stop_hook_active` | boolean | True when the stop-hook blocking cap has been reached (see §6) |

**SubagentStop fields:**

| Field | Type | Description |
|-------|------|-------------|
| `hook_event_name` | string | `"SubagentStop"` |
| `session_id` | string | Parent session identifier |
| `transcript_path` | string | Path to the subagent's transcript `.jsonl` |
| `cwd` | string | Working directory |
| `agent_id` | string | Unique identifier for this subagent instance |
| `agent_type` | string | Type of subagent (e.g. `"tool"`, `"parallel"`) |

The transcript at `transcript_path` is a newline-delimited JSON file (one JSON object per line). Each line is a turn in the conversation — user messages, assistant messages, tool calls, tool results, etc. The extractor reads this file to reconstruct what happened during the session.

---

## 6. Loop Safety

The hook exits 0 unconditionally. This makes it a **non-blocking** hook: it runs after the agent stops, reports nothing back to the agent, and cannot cause the agent to loop.

The `stop_hook_active` field (present on `Stop` events) becomes `true` after 8 consecutive blocking returns from a `Stop` hook — Claude Code's cap to prevent runaway loops via `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`. Because the capture hook always exits 0, it can never trigger this cap. The field is irrelevant for this use case but should be checked defensively: if `stop_hook_active` is `true` the hook can still run normally — it just means some other hook in the chain hit the cap.

**Re-entrancy:** the hook writes files to the connected wiki root. Writing markdown files does not trigger any Claude Code event (no `PostToolUse` on filesystem writes made by hooks), so no re-entrant loop is possible. A `stop_hook_active` guard (`if stop_hook_active: sys.exit(0)`) is optional but harmless if included for defence-in-depth.

---

## 7. Extractor Design

The capture hook (`hooks/capture.py`) has one job: read a transcript, classify what happened, and write MemRI node files.

### Transcript reading

Read `transcript_path` line by line, parsing each line as JSON. Reconstruct the sequence of assistant turns, tool calls, tool results, and any error messages. The goal is to answer: what task was attempted, what was produced, what succeeded, and what went wrong.

### Classification

From the reconstructed session, classify into one or more nodes:

- **Always** create at least one `memory` node summarising the session (what was done, what the outcome was, which files or systems were touched).
- **Conditionally** create a `rule` node if the session established a stable pattern that should govern future sessions (e.g. "always validate nodes before writing", "use sorted collections before random picks").
- **Conditionally** create an `issue` node if the session revealed a problem, contradiction, or unresolved gap.

### Link selection

Connect each new note to the existing graph with Obsidian `[[wikilinks]]` in its body. Link to the note's type ([[memory]], [[rule]], or [[issue]]) and to 2-4 related notes or topics by their id (there is no central index). A note that links to nothing (and that nothing links to) is an orphan and will be pruned at the next heartbeat, so check the wiki root for related notes to link before writing.

### Privacy guard before writing (REQUIRED)

Captured nodes are the user's private memory and must never be published. Privacy is **structural**: the wiki is an external directory (`$SHAPA_MEMORY` or the path `shapa init` recorded) that lives outside any git repository, so notes are never tracked and there is nothing to gitignore. The hook **must** resolve its target via `config.resolve(root)` and write only there — never inside the shapa repo or the installed package directory. As defence in depth it may verify the resolved path is not inside a git working tree before writing; writing a node anywhere inside a tracked repository is a privacy violation and a hard stop.

### Validation before writing

Call `shapa.validate.validate_node()` on the note before writing it to the wiki root. If the validator returns violations, fix the frontmatter (id must equal the stem, valid type/locus, consequence 1-10, non-negative uses) and re-validate. Do not write invalid notes. The validator is the contract; the hook must honour it.

### Writing notes

Write each note to `<wiki_root>/<id>.md` where `id` is a kebab-case slug derived from the session content (e.g. `memory-<date>-<short-description>`). The filename stem must match the `id` frontmatter field exactly, and the body should carry the `[[wikilinks]]` chosen above.

### Orphan sweep (optional)

After writing new notes, optionally invoke `shapa heartbeat` to prune any orphans that the new notes' link choices may have left unreachable. This keeps the graph clean immediately rather than waiting for the next manual heartbeat.

---

## 8. What Remains Deferred After This Design

This document specifies the complete design. The following concrete work items remain:

1. `hooks/capture.py` — the extractor script itself (reads transcript, classifies, writes nodes, validates, optionally runs heartbeat).
2. `install.sh` — the merge script shown in §4.
3. Integration test against a fixture transcript `.jsonl` to verify classification and node output.
4. Optional: `SessionEnd` teardown hook registration.

These correspond to **Milestone 5** in [[PRD]].
