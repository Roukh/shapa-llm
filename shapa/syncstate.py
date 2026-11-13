"""Where the hosted-remote client keeps its files, and how it writes them.

One directory (``~/.shapa``, or ``$SHAPA_REMOTE_DIR``) holds:

- ``remote.json`` - ``{url, device, device_id}``; no secret.
- ``remote.token`` - the device token, mode 0600, the only place it lives.
- ``sync/<sha1 of the resolved db path>.json`` - one wiki's sync state
  ``{version, key, url, cursor, base: {id: {rev, uid, updated, hash}}, retry}``.
- ``sync/<sha1>.lock`` - the per-wiki lock two sessions queue on.

Every write is atomic (temp file + rename). A missing, truncated or corrupt
state file reads as "never synced": the next sync re-derives everything from
the server, so a damaged file costs a round trip, never data.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: no cross-process lock
    fcntl = None

ENV_DIR = "SHAPA_REMOTE_DIR"
CONFIG_NAME = "remote.json"
TOKEN_NAME = "remote.token"
STATE_VERSION = 2
LOCK_POLL = 0.05
MAX_RETRY = 5000


def remote_dir() -> Path:
    env = os.environ.get(ENV_DIR)
    return Path(env).expanduser() if env else Path.home() / ".shapa"


@dataclass
class Credentials:
    url: str
    device: str
    device_id: str
    token: str = ""

    def __repr__(self) -> str:  # the token never reaches a log or a traceback
        return f"Credentials(url={self.url!r}, device={self.device!r}, device_id={self.device_id!r})"


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Write *text* to *path* via a temp file in the same directory, created
    with *mode* from the start (a token is never world-readable, not even
    briefly)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_credentials(creds: Credentials, rdir: Path | None = None) -> None:
    rdir = rdir or remote_dir()
    rdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(rdir / TOKEN_NAME, creds.token + "\n", 0o600)
    atomic_write(rdir / CONFIG_NAME, json.dumps(
        {"url": creds.url, "device": creds.device, "device_id": creds.device_id}, indent=2) + "\n")


def load_credentials(rdir: Path | None = None) -> Credentials | None:
    """The stored login, or ``None`` when absent, unreadable or incomplete
    (including a missing token file)."""
    rdir = rdir or remote_dir()
    try:
        cfg = json.loads((rdir / CONFIG_NAME).read_text(encoding="utf-8"))
        token = (rdir / TOKEN_NAME).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or not token:
        return None
    url, device, device_id = cfg.get("url"), cfg.get("device"), cfg.get("device_id")
    if not all(isinstance(v, str) and v for v in (url, device, device_id)):
        return None
    return Credentials(url, device, device_id, token)


def clear_credentials(rdir: Path | None = None) -> bool:
    rdir = rdir or remote_dir()
    gone = False
    for name in (CONFIG_NAME, TOKEN_NAME):
        try:
            (rdir / name).unlink()
            gone = True
        except OSError:
            pass
    return gone


# --- per-wiki sync state ----------------------------------------------------------------

def state_path(rdir: Path, db_file) -> Path:
    digest = hashlib.sha1(str(Path(db_file).resolve()).encode("utf-8")).hexdigest()
    return rdir / "sync" / f"{digest}.json"


def _valid_base(base) -> bool:
    if not isinstance(base, dict):
        return False
    for key, ent in base.items():
        if not (isinstance(key, str) and isinstance(ent, dict)):
            return False
        if not (isinstance(ent.get("rev"), int) and not isinstance(ent.get("rev"), bool)):
            return False
        if not all(isinstance(ent.get(f), str) for f in ("uid", "updated", "hash")):
            return False
    return True


def fresh_state(key: str, url: str) -> dict:
    return {"version": STATE_VERSION, "key": key, "url": url, "cursor": 0, "base": {},
            "last_sync": "", "retry": []}


def load_state(path: Path, key: str, url: str) -> dict:
    """The stored state when it is intact and belongs to this wiki key and
    server; otherwise a fresh one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fresh_state(key, url)
    if (not isinstance(data, dict) or data.get("version") != STATE_VERSION
            or data.get("key") != key or data.get("url") != url
            or not isinstance(data.get("cursor"), int) or isinstance(data.get("cursor"), bool)
            or data["cursor"] < 0 or not _valid_base(data.get("base"))):
        return fresh_state(key, url)
    data.setdefault("last_sync", "")
    retry = data.get("retry")
    data["retry"] = ([r for r in retry if isinstance(r, str)][:MAX_RETRY]
                     if isinstance(retry, list) else [])
    return data


def save_state(path: Path, state: dict) -> None:
    atomic_write(path, json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")


@contextmanager
def wiki_lock(path: Path, wait: float):
    """Hold the wiki's lock, polling up to *wait* seconds; yields whether it
    was obtained. Without ``fcntl`` it always yields True."""
    if fcntl is None:
        yield True
        return
    lock_file = path.with_suffix(".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_file, "a")
    got = False
    try:
        end = time.monotonic() + max(0.0, wait)
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= end:
                    break
                time.sleep(LOCK_POLL)
        yield got
    finally:
        if got:
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
