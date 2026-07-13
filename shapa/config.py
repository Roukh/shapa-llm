"""Where shapa keeps memory.

The user's memory (the "wiki") is EXTERNAL to the (public) tool and can live
anywhere. A wiki is a directory named ``shapa`` holding the ``AGENTS.md`` marker.
``shapa init`` scaffolds one at a repo root; every later command - and the
Claude Code hooks - resolve a wiki the same way. Resolution order (highest
priority first):

1. ``$SHAPA_MEMORY`` if set (explicit per-invocation override).
2. The nearest ``shapa/`` wiki discovered by walking up from the cwd (so a
   session in a project targets that project's wiki automatically).
3. The persisted pointer written by ``shapa init DIR`` (``~/.shapa/config.json``).
4. ``~/.shapa/memory`` otherwise (the default).

This keeps private notes out of the tool's repo entirely - memory never lives
inside the installed/cloned tool.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV_VAR = "SHAPA_MEMORY"
DEFAULT_DIR = Path.home() / ".shapa" / "memory"
#: Pointer file written by ``shapa init`` to remember a default wiki.
CONFIG_FILE = Path.home() / ".shapa" / "config.json"
#: Directory name a wiki lives in at a repo root, and the file that marks it.
#: This remains the implicit default target for a bare ``shapa init`` (unchanged,
#: so existing wikis/users are unaffected by the dot-folder addition below).
WIKI_DIRNAME = "shapa"
WIKI_MARKER = "AGENTS.md"
#: Every dirname `discover()` recognizes as a wiki, checked in this order at
#: each ancestor directory — the hidden dot-folder first (the current
#: convention for new wikis), then the legacy visible name for backward
#: compatibility with wikis created before this option existed. Additive only:
#: `WIKI_DIRNAME` above is untouched, so `shapa init`'s own default is unchanged.
WIKI_DIRNAMES = (".shapa", WIKI_DIRNAME)


def discover(start: str | Path | None = None) -> Path | None:
    """Return the nearest repo-root wiki at or above *start* (default: cwd).

    A wiki is a directory named one of :data:`WIKI_DIRNAMES` (checked in that
    order — dot-folder preferred) containing the :data:`WIKI_MARKER` file.
    Walking up from the cwd (like git finding ``.git``) lets a global hook
    target whichever project the session runs in. The marker requirement
    means shapa's own ``shapa/`` *package* directory - which has no
    ``AGENTS.md`` - is never mistaken for a wiki.
    """
    try:
        here = Path(start).resolve() if start is not None else Path.cwd().resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        for dirname in WIKI_DIRNAMES:
            wiki = d / dirname / WIKI_MARKER
            try:
                # A permission-denied ancestor must not crash the CLI: pre-3.13
                # ``Path.is_file()`` re-raises EACCES (the 3.13 pathlib rewrite
                # started swallowing it), and ``memory_dir`` is called at import.
                found = wiki.is_file()
            except OSError:
                continue
            if found:
                return (d / dirname).resolve()
    return None


def _pointer() -> Path | None:
    """Return the memory path recorded by ``shapa init``, if any and readable."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    memory = data.get("memory") if isinstance(data, dict) else None
    # Only a non-empty string is a valid path; anything else (number, list, a
    # hand-edited mistake) falls through to the default rather than crashing.
    return Path(memory).expanduser() if isinstance(memory, str) and memory else None


def memory_dir() -> Path:
    """Return the configured memory directory (not necessarily existing)."""
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    local = discover()
    if local is not None:
        return local
    pointer = _pointer()
    if pointer is not None:
        return pointer
    return DEFAULT_DIR


def set_memory_dir(path) -> Path:
    """Persist *path* as the connected wiki (written by ``shapa init``).

    Returns the resolved absolute path. Does not touch ``$SHAPA_MEMORY``: an
    explicit env override always wins over this pointer at resolution time.
    """
    resolved = Path(path).expanduser().resolve()
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps({"memory": str(resolved)}, indent=2) + "\n", encoding="utf-8"
    )
    return resolved


def resolve(root=None) -> Path:
    """Use *root* if given (expanding ``~``), else the configured memory directory."""
    return Path(root).expanduser() if root is not None else memory_dir()
