"""Format 3 -> 4: move a wiki's notes, checklist and memory log into its
database (``shapa upgrade`` runs :func:`step_database`).

What moves, and what does not:

- Root notes of type ``memory``/``rule``/``issue`` become M/R/I rows. The
  note's id is kept as the row's alias (``shapa get <old-id>`` still
  works), its frontmatter tags become tags, and a ``[[wikilink]]`` between
  two migrated notes becomes a ``related`` link. The note file is removed.
- ``checklist.md``: each section with open items becomes a feature, each
  open item a job under it (old ID kept as alias, verify command kept).
  Finished items are dropped - git history keeps them.
- ``ideas.md``: each entry still ``open`` or ``doing`` becomes a memory
  (an idea is something the operator said); the rest are dropped.
- ``memory/*.jsonl``: a live record captured from an operator message
  becomes a memory (an issue if it corrects the agent); records distilled
  from agent reports are dropped. The log directory is removed.
- ``agenda.md`` is removed (the database's open features are the agenda);
  so is the old ``.shapa-vectors.json`` cache.
- Reference notes, ``arch/``, ``research/`` and everything else stay files.

Idempotent: a wiki that already has a database is left alone.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from shapa import db, frontmatter, memlog
from shapa.nodes import CONVENTION_IDS, extract_links, is_excluded_path

ROW_KIND = {"memory": "M", "rule": "R", "issue": "I"}
_SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_IDEA_RE = re.compile(r"^-\s+(?P<date>\d{4}-\d{2}-\d{2})\s+·\s+(?P<status>\w+)\s+·\s+(?P<text>.+)$")
_LINK_TEXT_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_CHECKLIST_ITEM = re.compile(
    r"^- \[(?P<mark>[ ~x])\] \*\*(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)\*\* (?P<rest>.*)$")
_VERIFY = re.compile(r"\s+—\s+verify:\s+(?:`(?P<cmd>[^`]+)`|(?P<manual>manual))")
_STATE = re.compile(r"\s+—\s+(?:claimed|done)\s+\d{4}-\d{2}-\d{2}.*$")


def _plain(text: str) -> str:
    return _LINK_TEXT_RE.sub(lambda m: m.group(1), text).strip()


def _root_notes(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.glob("*.md")):
        if is_excluded_path(p.relative_to(root).parts) or p.stem in CONVENTION_IDS:
            continue
        out.append(p)
    return out


def pending(root: Path) -> bool:
    """Whether :func:`step_database` has anything to do: no database yet, or
    a format-3 memory log written after the migration (an older shapa's
    capture hook still running against this wiki)."""
    return not db.exists(root) or bool(memlog.log_files(root))


def _migrate_notes(conn, root: Path, removed: list[str]) -> None:
    links: list[tuple[str, set[str]]] = []
    alias_to_id: dict[str, str] = {}
    for p in _root_notes(root):
        parsed = frontmatter.parse(p)
        if parsed.error:
            continue
        ntype = str(parsed.meta.get("type", "")).strip().lower()
        if ntype not in ROW_KIND:
            continue
        alias = str(parsed.meta.get("id") or p.stem)
        body = parsed.body.strip()
        summary = str(parsed.meta.get("summary") or "").strip()
        if not summary:
            first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), alias)
            summary = first
        tags = [str(t) for t in (parsed.meta.get("tags") or []) if str(t).strip()]
        rid, _ = db.add_row(conn, ROW_KIND[ntype], _plain(summary), body, tags=tags,
                            alias=alias if not db.get_row(conn, alias) else None,
                            source=f"note:{alias}", now=None)
        alias_to_id[alias] = rid
        links.append((rid, extract_links(parsed.body)))
        p.unlink()
        removed.append(p.name)
    for rid, targets in links:
        for t in targets:
            other = alias_to_id.get(t)
            if other and other != rid:
                db.link(conn, rid, other, "related")


def _migrate_checklist(conn, root: Path, removed: list[str]) -> None:
    path = root / "checklist.md"
    if not path.is_file():
        return
    section, feature = None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _SECTION_RE.match(line)
        if m:
            section, feature = _plain(m.group("title")), None
            continue
        item = _CHECKLIST_ITEM.match(line)
        if not item or item["mark"] == "x":
            continue
        rest = _STATE.sub("", item["rest"])
        verify = _VERIFY.search(rest)
        cmd = verify["cmd"] if verify and verify["cmd"] else None
        text = _plain(rest[:verify.start()] if verify else rest)
        if feature is None:
            feature = db.add_item(conn, "F", section or "checklist")
        jid = db.add_item(conn, "J", text, parent=feature, verify=cmd, alias=item["id"])
        if verify and verify["manual"]:
            db.update_item(conn, jid, body="verify: manual (an operator decision)")
    path.unlink()
    removed.append("checklist.md")


def _migrate_ideas(conn, root: Path, removed: list[str]) -> None:
    path = root / "ideas.md"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _IDEA_RE.match(line.strip())
        if not m or m["status"].lower() not in ("open", "doing"):
            continue
        text = _plain(m["text"])
        db.add_row(conn, "M", text, f"Idea logged {m['date']}: {text}", tags=["idea"],
                   source="ideas.md")
    path.unlink()
    removed.append("ideas.md")


def _migrate_memlog(conn, root: Path, removed: list[str]) -> None:
    from shapa import ledger

    if not memlog.log_files(root):
        return
    view = memlog.read_log(root)
    for rec in view.active():
        # Operator messages only: captured requests, and format-2 session
        # notes (which recorded the operator's requests).
        if not rec.source.startswith(("capture:request", "upgrade:memory-session")):
            continue
        kind = "I" if ledger.is_correction(f"{rec.summary} {rec.body}") else "M"
        db.add_row(conn, kind, rec.summary, rec.body, tags=rec.tags, source=rec.source,
                   session=rec.session)
    shutil.rmtree(memlog.log_dir(root), ignore_errors=True)
    removed.append(f"{memlog.MEMORY_DIRNAME}/")


def step_database(root: Path, dry_run: bool) -> list[str]:
    if not pending(root):
        return []
    if db.exists(root):
        # Already format 4: only a stray memory log to fold in.
        if dry_run:
            return [f"{db.DB_FILENAME} <- {memlog.MEMORY_DIRNAME}/"]
        removed: list[str] = []
        conn = db.connect(root)
        try:
            _migrate_memlog(conn, root, removed)
        finally:
            conn.close()
        return [f"{db.DB_FILENAME} <- {', '.join(removed)}"]
    if dry_run:
        out = [p.name for p in _root_notes(root)
               if str(frontmatter.parse(p).meta.get("type", "")).lower() in ROW_KIND]
        out += [n for n in ("checklist.md", "ideas.md", "agenda.md") if (root / n).is_file()]
        if memlog.log_files(root):
            out.append(f"{memlog.MEMORY_DIRNAME}/")
        return [f"{db.DB_FILENAME} <- {', '.join(out) or 'new'}"]
    removed: list[str] = []
    conn = db.connect(root, create=True)
    try:
        _migrate_notes(conn, root, removed)
        _migrate_checklist(conn, root, removed)
        _migrate_ideas(conn, root, removed)
        _migrate_memlog(conn, root, removed)
    finally:
        conn.close()
    for name in ("agenda.md", ".shapa-vectors.json"):
        if (root / name).is_file():
            (root / name).unlink()
            removed.append(name)
    return [f"{db.DB_FILENAME} <- {', '.join(removed) or 'new'}"]
