"""Which wikis exist, and which format each one is at.

Two small pieces of bookkeeping that ``shapa upgrade`` (``shapa/upgrade.py``)
and the SessionStart hook (``shapa/bootstrap.py``) share - kept in their own
stdlib-only module so the hook path never imports the upgrade engine.

- **Format marker** - ``<wiki>/.shapa-format`` holds one integer: the wiki
  format the wiki was last brought fully current at by ``shapa upgrade``.
  It is wiki content (tracked in git), not a cache. A wiki with no marker
  predates it and is :data:`LEGACY_FORMAT`.
- **Registry** - ``~/.shapa/wikis.json`` (``$SHAPA_REGISTRY`` overrides)
  lists every wiki shapa has seen, so ``shapa upgrade --all`` can reach
  wikis outside the cwd. ``init``, ``bootstrap``, ``fetch`` and ``upgrade``
  register. Writes hold an exclusive ``flock`` across read-modify-write and
  land via a temp file + ``os.replace``, so concurrent sessions can neither
  corrupt the file nor drop each other's entries.

See shapa-backend-spec.md §11.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: atomic replace only
    fcntl = None

from shapa import config

#: The wiki format this shapa writes and checks against. Bump it whenever a
#: release changes what a conformant wiki looks like (schema, lean shape,
#: the shipped AGENTS.md) - every wiki then reads as behind until
#: ``shapa upgrade`` brings it current.
#:   1 - pre-marker wikis (schema v1, counters in frontmatter)
#:   2 - schema v2 + lean shape (spec §10 decisions 6/7)
#:   3 - v3 memory records: memory/ JSONL log, session notes converted
#:   4 - one database per wiki (shapa.db, tracked): the work ledger
#:       (features/jobs/tasks) and memory/rule/issue rows; notes, checklist,
#:       ideas, agenda and the memory log migrate into it
CURRENT_FORMAT = 4
LEGACY_FORMAT = 1
FORMAT_FILENAME = ".shapa-format"

REGISTRY_ENV_VAR = "SHAPA_REGISTRY"
DEFAULT_REGISTRY = Path.home() / ".shapa" / "wikis.json"


# --- format marker -----------------------------------------------------------

def is_wiki(path) -> bool:
    """True when *path* is a directory holding the ``AGENTS.md`` marker."""
    try:
        return (Path(path) / config.WIKI_MARKER).is_file()
    except OSError:
        return False


def read_format(root) -> int:
    """The format recorded in *root*'s marker; :data:`LEGACY_FORMAT` when the
    marker is missing or unreadable."""
    try:
        return int((Path(root) / FORMAT_FILENAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return LEGACY_FORMAT


def write_format(root, fmt: int = CURRENT_FORMAT) -> bool:
    """Write *fmt* into *root*'s marker. Returns whether the file changed."""
    path = Path(root) / FORMAT_FILENAME
    text = f"{fmt}\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")
    return True


def behind_notice(roots) -> str:
    """One context line naming every wiki in *roots* whose marker is behind
    :data:`CURRENT_FORMAT`, or ``""``. Marker-only (one tiny read per root)
    so it is safe on the SessionStart path; ``shapa upgrade --check`` does
    the full conformance scan."""
    behind = []
    for root in roots:
        path = Path(getattr(root, "path", root))
        if not is_wiki(path):
            continue
        fmt = read_format(path)
        if fmt < CURRENT_FORMAT:
            behind.append(f"{path} is format {fmt}<{CURRENT_FORMAT}")
    if not behind:
        return ""
    noun = "wiki" if len(behind) == 1 else "wikis"
    return f"shapa: {noun} {'; '.join(behind)} - run the shapa-upgrade skill"


# --- registry ----------------------------------------------------------------

def registry_path() -> Path:
    env = os.environ.get(REGISTRY_ENV_VAR)
    return Path(env).expanduser() if env else DEFAULT_REGISTRY


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> dict[str, dict]:
    data = json.loads(text)
    wikis = data.get("wikis") if isinstance(data, dict) else None
    if not isinstance(wikis, dict):
        raise ValueError("registry has no 'wikis' mapping")
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in wikis.items()}


def load() -> dict[str, dict]:
    """``{resolved wiki path: entry}``; ``{}`` when missing or unreadable."""
    try:
        return _parse(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _write_atomic(path: Path, wikis: dict[str, dict]) -> None:
    payload = json.dumps({"version": 1, "wikis": dict(sorted(wikis.items()))}, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _update(mutate) -> list[str]:
    """Run *mutate(wikis) -> changed paths* under the lock; write only when
    something changed. A corrupt registry is moved aside (never silently
    overwritten) and rebuilt from empty."""
    path = registry_path()
    with _locked(path):
        try:
            wikis = _parse(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            wikis = {}
        except (OSError, ValueError):
            try:
                os.replace(path, path.with_name(path.name + ".corrupt"))
            except OSError:
                pass
            wikis = {}
        changed = mutate(wikis)
        if changed:
            _write_atomic(path, wikis)
        return changed


def _resolve(p) -> str:
    try:
        return str(Path(p).expanduser().resolve())
    except OSError:
        return str(Path(p).expanduser())


def register(paths, via: str) -> list[str]:
    """Add every wiki among *paths* (path-likes or ``WikiRoot``s) that is not
    registered yet. Non-wikis are skipped. Never raises - this runs inside
    hooks - and never writes when nothing is new. Returns the added paths."""
    candidates = []
    for p in paths:
        p = getattr(p, "path", p)
        if is_wiki(p):
            candidates.append(_resolve(p))
    if not candidates:
        return []
    known = load()
    if all(c in known for c in candidates):
        return []  # the common case: no lock, no write

    def mutate(wikis):
        added = []
        for c in candidates:
            if c not in wikis:
                wikis[c] = {"added": _now(), "via": via}
                added.append(c)
        return added

    try:
        return _update(mutate)
    except Exception:
        return []


def unregister(paths) -> list[str]:
    """Drop *paths* from the registry. Returns the paths actually removed."""
    targets = {_resolve(p) for p in paths}

    def mutate(wikis):
        gone = [p for p in list(wikis) if p in targets]
        for p in gone:
            del wikis[p]
        return gone

    return _update(mutate)
