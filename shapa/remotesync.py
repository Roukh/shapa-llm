"""Which wikis ``shapa remote sync`` touches, and how, including the hook mode.

A repo wiki's key is the lowercase slug of its primary checkout's directory
name (``--key`` overrides it); the global wiki's key is ``global``. The origin
sent when a wiki is registered is ``host/owner/repo`` read from the checkout's
``origin`` URL, with any credentials in the URL dropped.

``--hook`` is the SessionStart/SessionEnd form: stdin is the harness hook JSON,
the whole run is held to :data:`HOOK_BUDGET` seconds (it runs in a worker
thread the caller stops waiting for), every error is swallowed, nothing is
raised, and at most one line is printed.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

from shapa import config, db, registry, sync, syncstate
from shapa.remotehttp import Client, clean_text

HOOK_BUDGET = 8.0
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ORIGIN_RE = re.compile(r"^\s*url\s*=\s*(\S+)\s*$", re.M)


def slug(name: str) -> str:
    """The lowercase slug of *name*, or ``""`` when nothing usable is left (the
    caller then asks for ``--key`` instead of guessing)."""
    out = _SLUG_RE.sub("-", name.lower()).strip("-._")[:64].rstrip("-._")
    return out if KEY_RE.match(out) else ""


def is_global(root) -> bool:
    try:
        return Path(root).resolve() == config.global_root().resolve()
    except OSError:
        return False


def folder_name(root) -> str:
    return db.db_path(root).parent.parent.name


def wiki_key(root, override: str | None = None, rdir: Path | None = None) -> str | None:
    """The server key of *root*: ``global``; the key passed with ``--key``; the key
    kept from an earlier ``--key``; else the slug of the checkout folder, but only
    when the repo has a git origin (two unrelated repos can share a folder name).
    ``None`` means the user has to pass ``--key`` once."""
    if is_global(root):
        return "global"
    if override and KEY_RE.match(override):
        return override
    kept = syncstate.named_key(syncstate.state_path(rdir or syncstate.remote_dir(), db.db_path(root)))
    if kept and KEY_RE.match(kept):
        return kept
    return slug(folder_name(root)) or None if git_origin(root) else None


def git_origin(root) -> str:
    """``host/owner/repo`` from the checkout's ``origin`` URL; ``""`` if none."""
    cfg = db.db_path(root).parent.parent / ".git" / "config"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    section = re.search(r'\[remote "origin"\](.*?)(?=^\[|\Z)', text, re.S | re.M)
    m = _ORIGIN_RE.search(section.group(1)) if section else None
    if not m:
        return ""
    url = m.group(1)
    url = re.sub(r"^[a-z+]+://", "", url)
    url = re.sub(r"^[^@/]*@", "", url)           # drop user[:password]@
    url = url.replace(":", "/", 1) if re.match(r"^[^/]+:(?!\d+/)", url) else url
    url = re.sub(r"\.git$", "", url).strip("/")
    return url if re.fullmatch(r"[A-Za-z0-9._-]+(?::\d+)?(/[A-Za-z0-9._~-]+)+", url) else ""


def wikis_in_scope(cwd, everything: bool) -> list[Path]:
    """The wiki directories to sync: this repo's and the global one, or every
    registered wiki with ``--all`` (the global wiki always included)."""
    if everything:
        found = [Path(p) for p in registry.load() if registry.is_wiki(p)]
        found.append(config.global_root())
    else:
        found = [wr.path for wr in config.wiki_roots(cwd) if wr.kind in ("repo", "global")]
    out, seen = [], set()
    for p in found:
        try:
            real = Path(p).resolve()
        except OSError:
            continue
        if real in seen or not registry.is_wiki(real):
            continue
        seen.add(real)
        out.append(real)
    return out


def run_sync(client: Client, rdir: Path, roots: list[Path], *, key: str | None = None,
             dry_run: bool = False, if_changed: bool = False, wait: float = 30.0
             ) -> list[sync.SyncResult]:
    results = []
    for root in roots:
        k = wiki_key(root, key, rdir)
        if k is None:
            results.append(sync.SyncResult(
                key=folder_name(root), status="needs-key",
                message="this repo has no git origin, so it has no automatic wiki key; "
                        "run `shapa remote sync --key KEY` once (the key is kept)"))
            continue
        try:
            res = sync.sync_wiki(root, client=client, rdir=rdir, key=k, name=k,
                                 kind="global" if is_global(root) else "repo",
                                 origin="" if is_global(root) else git_origin(root),
                                 dry_run=dry_run, if_changed=if_changed, wait=wait,
                                 named=bool(key) and not is_global(root))
        except Exception as exc:  # one wiki's bug must not stop the others
            res = sync.SyncResult(key=k, status="error",
                                  message=f"unexpected {type(exc).__name__}")
        results.append(res)
    return results


def summary_line(results: list[sync.SyncResult]) -> str:
    sent = sum(r.sent_put + r.sent_del for r in results if r.status == "ok")
    got = sum(r.applied for r in results if r.status == "ok")
    if not (sent or got):
        return ""
    return f"shapa remote: synced {len(results)} wiki(s) - sent {sent}, received {got}"


def _hook_cwd(raw: str) -> str | None:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return None
    cwd = data.get("cwd") if isinstance(data, dict) else None
    return cwd if isinstance(cwd, str) and cwd else None


def run_hook(args, creds, *, transport=None, stdin=None) -> int:
    """The hook form. Always returns 0; prints at most one line."""
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
    except Exception:
        raw = ""
    if creds is None:
        return 0
    deadline = time.monotonic() + HOOK_BUDGET
    box: dict[str, str] = {}

    def work() -> None:
        try:
            client = Client(creds.url, creds.token, transport=transport, deadline=deadline)
            cwd = _hook_cwd(raw) or args.cwd
            results = run_sync(client, syncstate.remote_dir(), wikis_in_scope(cwd, args.all),
                               key=args.key, if_changed=args.if_changed,
                               wait=max(0.0, deadline - time.monotonic()))
            box["line"] = summary_line(results)
        except Exception:
            pass

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(HOOK_BUDGET)
    line = box.get("line", "")
    if line:
        try:
            print(clean_text(line, 200))
        except Exception:
            pass
    return 0
