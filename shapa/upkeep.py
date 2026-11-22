"""Session-start upkeep: the work a repo wiki owes before any other work starts.

Two things put a block at the top of a session start in a repo (the ``<shapa-action>`` head, with the
restructure directive of :mod:`shapa.reconfigure`): the repo wiki is behind this shapa (its shipped docs
differ from the ones this version installs), or open agent tasks in the global wiki are addressed to this
repo (tag ``repo:KEY``, KEY = :func:`shapa.hierarchy.repo_key`). The block tells the agent to dispatch ONE
subagent for all of it before it does anything else, so the upkeep starts by itself at the next session.

Cheap and silent: two small file reads and one indexed read-only query, no schema work, no write. A locked
or unreadable database, an absent wiki or any error is nothing printed; nothing to do prints nothing.
``SHAPA_UPKEEP=0`` turns it off. A wiki whose format marker is behind is the restructure directive's: it
leads there, so this module stays out of its way (one dispatch, not two).
"""

from __future__ import annotations

import os
import re
import shlex
import sqlite3
from pathlib import Path

from shapa import db, hierarchy, registry

OFF_ENV = "SHAPA_UPKEEP"
BUSY_S = 1.0  # the session start's lock budget (bootstrap.SESSION_BUSY_MS)
MAX_TASKS = 5
ASSETS = Path(__file__).resolve().parent / "assets"
SHIPPED_DOCS = ("AGENTS.md", "placement.md")  # validate.MANAGED_DOCS, which upgrade rewrites verbatim
_TASK_ID = re.compile(r"T[0-9]{1,9}")


def off() -> bool:
    return os.environ.get(OFF_ENV) == "0"


def behind(wiki) -> bool:
    """Does *wiki* (format current) carry docs other than the ones this shapa ships?"""
    if registry.read_format(wiki) != registry.CURRENT_FORMAT:
        return False  # older: the restructure directive owns it; newer: an older shapa never touches it
    for name in SHIPPED_DOCS:
        try:
            if (Path(wiki) / name).read_text(encoding="utf-8") != (ASSETS / name).read_text(encoding="utf-8"):
                return True
        except OSError:
            return False  # no shipped copy to compare, or no doc to compare with
    return False


def open_tasks(global_wiki, key: str) -> list[str]:
    """The ids of the (at most :data:`MAX_TASKS`) open ``agent_task`` items of the global wiki tagged
    ``repo:<key>``, oldest first. Read-only; any database trouble is ``[]``."""
    path = db.db_path(global_wiki)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_S)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT i.id FROM tags r JOIN tags o ON o.id = r.id AND o.tag = ? "
            "JOIN items i ON i.id = r.id WHERE r.tag = ? AND i.kind = 'T' AND i.status != 'closed' "
            "ORDER BY i.created, i.id LIMIT ?",
            ("agent_task", db.REPO_PREFIX + key, MAX_TASKS)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [r[0] for r in rows if _TASK_ID.fullmatch(r[0])]  # ids only: a title is never put into the block


def _shell_path(wiki) -> str:
    """*wiki* as one shell word on one line. A checkout directory is named by whoever made it (an unpacked
    archive can hold a newline and a closing tag), so no character that could end the block or the line
    reaches the session text."""
    return shlex.quote("".join(c if c.isprintable() and c not in "<>" else "?" for c in str(wiki)))


def directive_lines(wiki_roots) -> list[str]:
    """The upkeep directive for the repo wiki among *wiki_roots* (``[]`` when there is none, or nothing is
    owed). Shown inside ``<shapa-action>`` above everything else."""
    if off():
        return []
    repo = next((wr for wr in wiki_roots if wr.kind == "repo"), None)
    if repo is None:
        return []
    wiki = db.db_path(repo.path).parent
    work: list[str] = []
    if behind(wiki):
        work.append(f"`shapa upgrade {_shell_path(wiki)}` and every judgment item it prints")
    glob = next((wr for wr in wiki_roots if wr.kind == "global"), None)
    ids = open_tasks(glob.path, hierarchy.repo_key(repo.path)) if glob else []
    if ids:
        work.append(f"the open agent tasks of this repo, {', '.join(ids)}: read each with `shapa get ID`, "
                    "do it, then close it with `shapa ledger close ID --scope global`")
    if not work:
        return []
    return ["ACTION FIRST, before the user's task: dispatch ONE background subagent for this repo's upkeep: "
            + "; then ".join(work) + ". Do not do it yourself; carry on with the user's task."]
