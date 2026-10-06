"""The format-4 reconfiguration directive.

A wiki below the current format, or one ``shapa upgrade`` migrated mechanically
but nobody has restructured yet, gets a directive at the very top of every
session's bootstrap: dispatch one dedicated agent (``opus`` by default,
``$SHAPA_RECONFIGURE_MODEL`` to change it) with the shipped prompt
(:data:`PROMPT_ASSET`). Printing the prompt claims the job for
:data:`CLAIM_TTL_HOURS` so parallel sessions don't dispatch twice; the agent's
last step marks the wiki done, which ends the directive.

State: a ``reconfigure`` key in the database's ``meta`` table (``pending``
after a format-3 migration, ``done`` once restructured; a wiki born in format
4 has neither and needs nothing), and a gitignored claim file in the wiki.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shapa import db, migrate4, registry

META_KEY = "reconfigure"
CLAIM_FILENAME = ".shapa-reconfigure-claim"
CLAIM_TTL_HOURS = 6
MODEL_ENV = "SHAPA_RECONFIGURE_MODEL"
DEFAULT_MODEL = "opus"
PROMPT_ASSET = Path(__file__).resolve().parent / "assets" / "prompts" / "reconfigure.md"


def target(root) -> Path:
    """The wiki to restructure: in a linked worktree, the primary checkout's
    copy (the one whose database every worktree shares)."""
    return db.db_path(root).parent


def _meta(root) -> str | None:
    if not db.exists(root):
        return None
    conn = db.connect(root)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_state(root, state: str) -> None:
    conn = db.connect(root, create=True)
    try:
        with db.write(conn):
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (META_KEY, state))
    finally:
        conn.close()


def reasons(root) -> list[str]:
    """Why *root* still needs the restructure (empty: it doesn't)."""
    root = target(root)
    if not registry.is_wiki(root):
        return []
    fmt = registry.read_format(root)
    out = []
    if fmt < registry.CURRENT_FORMAT:
        out.append(f"format {fmt}, shapa is at {registry.CURRENT_FORMAT}")
    left = migrate4.leftovers(root) if fmt >= registry.CURRENT_FORMAT else []
    if left:
        out.append("format-3 files left: " + ", ".join(left[:4]) + (" ..." if len(left) > 4 else ""))
    if _meta(root) == "pending":
        out.append("migrated mechanically, not yet restructured")
    return out


def claimed(root, now: datetime | None = None) -> str | None:
    """The live claim on *root* (``"<who> at <when>"``), or ``None``."""
    path = target(root) / CLAIM_FILENAME
    try:
        text = path.read_text(encoding="utf-8").strip()
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None
    now = now or datetime.now(timezone.utc)
    if now - stamp > timedelta(hours=CLAIM_TTL_HOURS):
        return None
    return text or stamp.isoformat(timespec="minutes")


def claim(root, who: str = "") -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
    (target(root) / CLAIM_FILENAME).write_text(f"{who or 'a session'} at {stamp}\n", encoding="utf-8")


def mark_done(root) -> None:
    set_state(target(root), "done")
    try:
        (target(root) / CLAIM_FILENAME).unlink()
    except OSError:
        pass


def model() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def prompt(root) -> str:
    wiki = target(root)
    return PROMPT_ASSET.read_text(encoding="utf-8").replace("{wiki}", str(wiki)).replace(
        "{repo}", str(wiki.parent))


def directive_lines(wiki_paths) -> list[str]:
    """The session-start directive for every wiki in *wiki_paths* that needs
    the restructure. Shown above everything else in the bootstrap."""
    lines: list[str] = []
    seen: set[Path] = set()
    for path in wiki_paths:
        try:
            wiki = target(path)
            if wiki in seen:
                continue
            seen.add(wiki)
            why = reasons(wiki)
            if not why:
                continue
            busy = claimed(wiki)
        except Exception:
            continue  # never block session start
        if busy:
            lines.append(f"shapa: restructure of {wiki} in progress ({busy}); "
                         "do not dispatch another agent for it.")
            continue
        lines.append(
            f"ACTION FIRST, before the user's task: the wiki {wiki} needs the format-4 "
            f"restructure ({'; '.join(why)}). Dispatch one dedicated background agent on "
            f"model \"{model()}\" whose whole prompt is the output of "
            f"`shapa upgrade --reconfigure-prompt {wiki}` (running that command claims the job "
            "so parallel sessions don't duplicate it). Then carry on with the user's task; "
            "don't do the restructure yourself.")
    return lines
