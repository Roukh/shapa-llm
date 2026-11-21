"""Keep the SessionStart hook's output valid and under Claude Code's cap.

Claude Code truncates a hook's ``additionalContext`` at 10,000 characters. ``shapa bootstrap`` sizes its own
text under :data:`HOOK_CAP` (``bootstrap.build_focused``), so this wrapper is a guarantee, not a layout:

* the text over the cap is shortened by the mempm block's own trimming (memories first, then mistakes,
  principles last: ``bootstrap.mempm_block``); when no entry fits, the block goes whole (heading and JSON,
  never a cut string); when the rest alone is still over, whole lines go from the bottom up, keeping the
  first line through PRECEDENCE, the SKILL GATE and the write-back line;
* a hook never blocks a session: whatever the session start raises, the wrapper prints a valid envelope
  and exits 0.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys

HOOK_CAP = 8000
#: The lines a cut never takes: from the start through PRECEDENCE, and these from the gate on.
KEEP_PREFIXES = ("PRECEDENCE:", "SKILL GATE:", "WRITE BACK:", "</shapa-memory>")
_FALLBACK = ("<shapa-memory>\nshapa session start could not be built; run `shapa bootstrap` by hand to see why.\n"
             "WRITE BACK: `shapa save --type decision|idea|memory|rule|issue --summary TEXT` (MCP: `save`).\n"
             "</shapa-memory>")


def _mempm_at(lines: list[str]) -> int:
    """Index of the heading line of the mempm block (the JSON is the next line), or -1."""
    for i, line in enumerate(lines[:-1]):
        if line.startswith("MEMPM (") and lines[i + 1].startswith('{"P":'):
            return i
    return -1


def _trim_mempm(lines: list[str], limit: int) -> list[str]:
    """Shorten the mempm block so the text fits *limit*, or take the block out whole when no entry fits."""
    from shapa import bootstrap

    i = _mempm_at(lines)
    if i < 0:
        return lines
    mem = json.loads(lines[i + 1])
    if not (isinstance(mem, dict) and all(isinstance(mem.get(k), list) for k in bootstrap.MEMPM_KEYS)):
        raise ValueError("not a mempm object")
    others = len("\n".join([*lines[:i], *lines[i + 2:]])) + 1      # the text without the block, one joint kept
    block, _ = bootstrap.mempm_block(mem, limit - others)
    return [*lines[:i], *(block.split("\n") if block else []), *lines[i + 2:]]


def _drop_lines(lines: list[str], limit: int) -> list[str]:
    """Whole lines from the bottom up until the text fits; the first line through PRECEDENCE and the gate and
    write-back lines stay."""
    last_head = next((i for i, line in enumerate(lines) if line.startswith("PRECEDENCE:")), 0)
    out = list(lines)
    for i in range(len(out) - 1, last_head, -1):
        if len("\n".join(out)) <= limit:
            break
        if not out[i].startswith(KEEP_PREFIXES):
            del out[i]
    return out


def cap(text: str, limit: int = HOOK_CAP) -> str:
    """*text* shortened to at most *limit* characters, never in the middle of a string."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    try:
        lines = _trim_mempm(lines, limit)
    except Exception:
        i = _mempm_at(lines)                       # a block that cannot be read goes whole
        if i >= 0:
            del lines[i:i + 2]
    out = "\n".join(lines)
    if len(out) > limit:
        out = "\n".join(_drop_lines(lines, limit))
    if len(out) > limit:                           # only the kept lines are left and they do not fit
        out = _FALLBACK
    return out


def cap_envelope(raw: str, limit: int = HOOK_CAP) -> str:
    """The hook envelope *raw* (JSON text) with its context capped; anything else comes back unchanged."""
    try:
        env = json.loads(raw)
        ctx = env["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        return raw
    if not isinstance(ctx, str) or len(ctx) <= limit:
        return raw
    env["hookSpecificOutput"]["additionalContext"] = cap(ctx, limit)
    return json.dumps(env) + "\n"


def _envelope(context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}) + "\n"


def run_bootstrap(argv: list[str]) -> None:
    """``shapa bootstrap`` with the hook envelope capped. Other formats pass through. Never raises: a session
    start that fails prints a valid envelope and exits 0, because a hook must not block a session."""
    agents_md = ("--format" in argv and argv[argv.index("--format") + 1:argv.index("--format") + 2] == ["agents-md"]
                 or any(a.startswith("--format=agents-md") for a in argv))
    buf = io.StringIO()
    code = 0
    try:
        from shapa import bootstrap

        if agents_md:
            bootstrap.main(argv)
            return
        with contextlib.redirect_stdout(buf):
            try:
                bootstrap.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 0
        raw = buf.getvalue()
        try:
            raw = cap_envelope(raw)
        except Exception:
            raw = _envelope(_FALLBACK)
    except SystemExit:
        raise
    except BaseException:
        raw = _envelope(_FALLBACK) if not agents_md else buf.getvalue()
        code = 0
    sys.stdout.write(raw)
    sys.stdout.flush()
    sys.exit(code)
