"""Where shapa keeps memory.

The user's memory (the "wiki") is EXTERNAL to the (public) tool and can live
anywhere. ``shapa init [DIR]`` *connects* a wiki by recording its path in a
small pointer file, so every later command - and the Claude Code hooks - resolve
the same location. Resolution order (highest priority first):

1. ``$SHAPA_MEMORY`` if set (explicit per-invocation override).
2. The persisted pointer written by ``shapa init`` (``~/.shapa/config.json``).
3. ``~/.shapa/memory`` otherwise (the default).

This keeps private notes out of the tool's repo entirely - memory never lives
inside the installed/cloned tool.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV_VAR = "SHAPA_MEMORY"
DEFAULT_DIR = Path.home() / ".shapa" / "memory"
#: Pointer file written by ``shapa init`` to remember the connected wiki.
CONFIG_FILE = Path.home() / ".shapa" / "config.json"


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
