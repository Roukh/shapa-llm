"""Keep the SessionStart hook's context under Claude Code's cap.

Claude Code truncates a hook's ``additionalContext`` at 10,000 characters (the
overflow goes to a file with a short preview). ``shapa bootstrap`` is wrapped here
so the string it emits never passes :data:`HOOK_CAP` (8,000), cutting whole parts
from the lowest priority up and saying what was left out. The wrapper only reads
the hook envelope the session start already printed, so it does not depend on how
the session text is built.

Priority, lowest first (cut first): agent tasks, projects and the operator's
tasks, topics, learned principles, the pending-action lines, baseline skills,
baseline rules. Everything before the first of those (the action directive, the
header, precedence, northstars, fires and their tree) and everything from the
write-back line on is kept.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys

HOOK_CAP = 8000
#: A part opens on a line matching its pattern and runs to the next part's opening line.
PARTS = (
    ("baseline", re.compile(r"^BASELINE \(")),
    ("skills", re.compile(r"^BASELINE SKILLS")),
    ("topics", re.compile(r"^TOPICS:")),
    ("projects", re.compile(r"^(PROJECTS:|The operator's open tasks:)")),
    ("agent", re.compile(r"^Agent tasks")),
    ("principles", re.compile(r"^context learned from corrections")),
    ("pending", re.compile(r"^(\d+ corrections? awaits?|\d+ skill versions? awaits?|drift:)")),
)
#: From the fetch note on, every line is kept: the skill gate and the write-back line are never cut.
TAIL = re.compile(r"^(Fetch on demand|SKILL GATE:|WRITE BACK:|</shapa-memory>)")
CUT_ORDER = ("agent", "projects", "topics", "principles", "pending", "skills", "baseline")


def cap(text: str, limit: int = HOOK_CAP) -> str:
    """*text* shortened to at most *limit* characters, whole parts at a time."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    head: list[str] = []
    parts: list[tuple[str, list[str]]] = []
    tail: list[str] = []
    for line in lines:
        if tail or TAIL.match(line):
            tail.append(line)
            continue
        kind = next((k for k, rx in PARTS if rx.match(line)), None)
        if kind is not None:
            parts.append((kind, [line]))
        elif parts:
            parts[-1][1].append(line)
        else:
            head.append(line)
    dropped: list[str] = []

    def size() -> int:
        return sum(len(x) + 1 for x in [*head, *(ln for _, p in parts for ln in p), *tail]) + 160

    for kind in CUT_ORDER:
        while size() > limit and any(k == kind for k, _ in parts):
            i = max(i for i, (k, _) in enumerate(parts) if k == kind)
            if dropped.count(kind) == 0:
                dropped.append(kind)
            parts.pop(i)
    note = (f"[session start cut to fit the {limit}-character hook limit; left out: "
            f"{', '.join(dropped)}. Everything: `shapa bootstrap --full`]") if dropped else ""
    body = [*head, *(ln for _, p in parts for ln in p)]
    out = "\n".join([*body, *([note] if note else []), *tail])
    if len(out) > limit:  # the kept parts alone are over: cut the tree's tail, keep the write-back line
        room = limit - len("\n".join(tail)) - len(note) - 8
        out = "\n".join(body)[:max(room, 0)].rsplit("\n", 1)[0] + "\n" + (note + "\n" if note else "") + "\n".join(tail)
    return out[:limit]


def run_bootstrap(argv: list[str]) -> None:
    """``shapa bootstrap`` with the hook envelope capped. Other formats pass through."""
    from shapa import bootstrap

    if "--format" in argv and argv[argv.index("--format") + 1:argv.index("--format") + 2] == ["agents-md"] \
            or any(a.startswith("--format=agents-md") for a in argv):
        bootstrap.main(argv)
        return
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            bootstrap.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
    raw = buf.getvalue()
    try:
        env = json.loads(raw)
        ctx = env["hookSpecificOutput"]["additionalContext"]
        if isinstance(ctx, str) and len(ctx) > HOOK_CAP:
            env["hookSpecificOutput"]["additionalContext"] = cap(ctx)
            raw = json.dumps(env) + "\n"
    except (ValueError, KeyError, TypeError):
        pass  # not the hook envelope: pass it through unchanged
    sys.stdout.write(raw)
    sys.stdout.flush()
    sys.exit(code)
