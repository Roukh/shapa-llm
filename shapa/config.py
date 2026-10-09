"""Where shapa keeps memory.

The user's memory (the "wiki") is EXTERNAL to the (public) tool and can live
anywhere. A wiki is a directory named ``shapa`` holding the ``AGENTS.md`` marker.
``shapa init`` scaffolds one at a repo root; every later command - and the
Claude Code hooks - resolve a wiki the same way. Resolution order (highest
priority first):

1. ``$SHAPA_MEMORY`` if set (explicit per-invocation override).
2. The nearest ``shapa/`` wiki discovered by walking up from the cwd (so a
   session in a project targets that project's wiki automatically).
3. The persisted pointer written by ``shapa init --global [DIR]``
   (``~/.shapa/config.json``) - plain ``shapa init DIR`` never writes it.
4. ``~/.shapa/memory`` otherwise (the default).

This keeps private notes out of the tool's repo entirely - memory never lives
inside the installed/cloned tool.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ENV_VAR = "SHAPA_MEMORY"
DEFAULT_DIR = Path.home() / ".shapa" / "memory"
#: Pointer file written by ``shapa init --global`` to remember the global wiki.
CONFIG_FILE = Path.home() / ".shapa" / "config.json"
#: The legacy visible dirname a wiki could live in at a repo root, and the
#: file that marks it. A bare ``shapa init`` no longer targets this name -
#: its default is the hidden ``.shapa`` in `WIKI_DIRNAMES` below - but a
#: wiki already scaffolded under this name keeps resolving, and `discover()`
#: still respects one if it finds it.
WIKI_DIRNAME = "shapa"
WIKI_MARKER = "AGENTS.md"
#: Every dirname `discover()` recognizes as a wiki, checked in this order at
#: each ancestor directory — the hidden dot-folder first (the current
#: convention, and `shapa init`'s own default), then the legacy visible name
#: for backward compatibility with wikis created before this option existed.
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


def scrub_terms() -> list[str]:
    """Terms ``shapa commit`` replaces in every text column before staging
    a wiki database, read from the pointer file's optional ``scrub_terms``
    list (additive with that command's own ``--scrub`` flag)."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    terms = data.get("scrub_terms") if isinstance(data, dict) else None
    return [t for t in terms if isinstance(t, str) and t] if isinstance(terms, list) else []


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
    """Persist *path* as the global wiki (written by ``shapa init --global``).

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


# ---------------------------------------------------------------------------
# Multi-root resolution (reads fan out across every wiki in scope; writes
# stay single-target - see shapa/capture.py's ``--scope``). Additive: the
# single-root API above (``discover``/``memory_dir``/``resolve``) is
# untouched, so ``init``/``where``/every existing single-root caller keeps
# working unchanged. See docs/design's shapa-backend-spec.md §4.1.
# ---------------------------------------------------------------------------

#: Directory under the global wiki holding per-repo notes for a repo that has
#: no wiki of its own (`kind="external"` below). No hardcoded repo list - the
#: repo name is read off the git checkout's own directory name.
EXTERNAL_DIRNAME = "external"


@dataclass(frozen=True)
class WikiRoot:
    """One wiki directory a read should search.

    ``kind`` says why it is in scope: ``"repo"`` (this checkout's own wiki),
    ``"external"`` (a bucket for a repo with no wiki of its own, filed under
    the global wiki's ``external/<repo>/``), or ``"global"`` (the
    cwd-independent default/shared wiki). ``repo`` is the checkout's
    directory name, set for ``"repo"`` and ``"external"``, ``None`` for
    ``"global"``.
    """

    path: Path
    kind: Literal["repo", "external", "global"]
    repo: str | None = None


def global_root() -> Path:
    """The cwd-independent global wiki: ``$SHAPA_MEMORY`` > the ``shapa init``
    pointer > ``~/.shapa/memory``. Unlike :func:`memory_dir`, this never
    prefers a repo-local wiki - it is always the *global* one, the anchor
    every :func:`wiki_roots` result falls back to."""
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    pointer = _pointer()
    if pointer is not None:
        return pointer
    return DEFAULT_DIR


def _git_toplevel(start: Path) -> Path | None:
    """Walk up from *start* to the nearest ancestor containing ``.git``
    (worktree or plain checkout), without shelling out to ``git`` - a repo
    name is just that directory's basename, so no hardcoded repo list is
    needed anywhere in this module."""
    for d in (start, *start.parents):
        try:
            if (d / ".git").exists():
                return d
        except OSError:
            continue
    return None


def wiki_roots(start: str | Path | None = None) -> list[WikiRoot]:
    """Every wiki a read should search, most-specific first.

    1. ``$SHAPA_MEMORY`` set -> a single ``kind="global"`` root (the explicit
       override escape hatch used by ``--root``/CI; the hooks/MCP path never
       sets this itself).
    2. Else, a repo-local wiki found by :func:`discover` from *start*
       (default: cwd) -> ``kind="repo"``, *unless* its resolved path is the
       same directory as :func:`global_root` (a repo whose own wiki *is* the
       global wiki gets exactly one entry below, not two).
    3. Else, if *start* sits inside a git checkout with no wiki of its own,
       and ``<global_root>/external/<repo-name>/`` exists on disk ->
       ``kind="external"``.
    4. The global wiki is always appended last, with ``kind="global"``,
       unless step 2 already added that same path (dedup).

    Missing directories are tolerated throughout - this returns *paths to
    search*, not a guarantee any of them exist; callers already treat a
    non-existent/empty root as "no notes" (see ``fetch.select``).
    """
    env = os.environ.get(ENV_VAR)
    if env:
        return [WikiRoot(path=Path(env).expanduser(), kind="global")]

    try:
        here = Path(start).resolve() if start is not None else Path.cwd().resolve()
    except OSError:
        here = None

    g_root = global_root()
    try:
        g_resolved = g_root.resolve()
    except OSError:
        g_resolved = g_root

    roots: list[WikiRoot] = []
    seen: set[Path] = set()

    repo_name: str | None = None
    if here is not None:
        git_root = _git_toplevel(here)
        if git_root is not None:
            repo_name = git_root.name

    local = discover(here) if here is not None else None
    if local is not None:
        try:
            local_resolved = local.resolve()
        except OSError:
            local_resolved = local
        if local_resolved != g_resolved:
            roots.append(WikiRoot(path=local_resolved, kind="repo", repo=repo_name))
            seen.add(local_resolved)
    elif repo_name is not None:
        external = g_root / EXTERNAL_DIRNAME / repo_name
        if external.is_dir():
            try:
                ext_resolved = external.resolve()
            except OSError:
                ext_resolved = external
            roots.append(WikiRoot(path=ext_resolved, kind="external", repo=repo_name))
            seen.add(ext_resolved)

    if g_resolved not in seen:
        roots.append(WikiRoot(path=g_root, kind="global"))

    return roots


def find_sibling_repo(name: str, start: str | Path | None = None) -> Path | None:
    """Find another repo's checkout by *name* - no hardcoded repo list
    anywhere (spec §10 decision 4's ``--applies-to``/``scope: external``).

    Looks for a directory literally named *name* that is itself a git
    checkout (has ``.git``), one level above *start*'s own git checkout -
    the flat multi-repo workspace layout ``wiki_roots()``'s ``external/``
    bucket already assumed (or, if *start* is not itself inside a git
    checkout, one level above *start*). Returns ``None`` - never guesses,
    never falls back to the global wiki - if no such directory exists.
    """
    if not name:
        return None
    try:
        here = Path(start).resolve() if start is not None else Path.cwd().resolve()
    except OSError:
        return None
    git_root = _git_toplevel(here)
    workspace = git_root.parent if git_root is not None else here
    candidate = workspace / name
    try:
        if candidate.is_dir() and (candidate / ".git").exists():
            return candidate.resolve()
    except OSError:
        return None
    return None
