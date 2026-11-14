"""Records on the wire, and the rules for letting a remote record in.

The hosted store is untrusted input. Everything it sends passes
:func:`validate_record` before it can touch the database or a file: shape and
types, an id that matches its kind, bounded sizes, control characters
stripped, secrets redacted (``shapa.redact``). A record that fails is dropped
and counted, never half-applied.

Notes (kind ``N``) are the wiki's ``arch/`` and ``research/`` files plus root
``*.md``; a note's id is its path, and :func:`write_note` / :func:`remove_note`
re-check that path against the contract's pattern and against the real
directory (symlinks, ``..``) before any file is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from shapa import db
from shapa.redact import redact

MAX_RECORD_BYTES = 1_000_000
NOTE_RE = re.compile(r"^(arch/|research/)?[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$")
#: Harness instruction files never sync, in any folder, in any letter case.
NOTE_RESERVED = ("agents.md", "claude.md", "gemini.md", "placement.md")
KIND_ORDER = "FJTMRIDN"
ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
UID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SPECIAL_LINE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_BODY_STRIP_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ITEM_STATUSES = ("open", "claimed", "closed")
MAX_TAGS = 100
MAX_LINKS = 200


def is_note_id(note_id) -> bool:
    return (isinstance(note_id, str) and bool(NOTE_RE.match(note_id))
            and note_id.rsplit("/", 1)[-1].lower() not in NOTE_RESERVED)


def id_matches_kind(rec_id, kind) -> bool:
    if kind == "N":
        return is_note_id(rec_id)
    m = db.ID_RE.match(rec_id) if isinstance(rec_id, str) else None
    return bool(m) and m.group(1) == kind


def record_hash(rec: dict) -> str:
    """Local-only content hash (data, tags, links): what "modified" means."""
    blob = json.dumps([rec["data"], rec["tags"], rec["links"]], sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def record_hashes(recs: dict[str, dict]) -> dict[str, str]:
    return {i: record_hash(r) for i, r in recs.items()}


# --- local notes ---------------------------------------------------------------------------

def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_exact(path: Path) -> str:
    """The file's text with its line endings untouched (no newline translation)."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def snapshot_notes(wiki_dir: Path) -> dict[str, dict]:
    """Every syncable note file as a wire record (symlinks and non-UTF-8 or
    oversized files are left out)."""
    out: dict[str, dict] = {}
    for pattern in ("*.md", "arch/*.md", "research/*.md"):
        for path in sorted(wiki_dir.glob(pattern)):
            note_id = path.relative_to(wiki_dir).as_posix()
            if not is_note_id(note_id) or path.is_symlink() or not path.is_file():
                continue
            try:
                if _note_target(wiki_dir, note_id, create_dir=False) != path.resolve():
                    continue  # the folder or the file leads out of the wiki
            except (ValueError, OSError):
                continue
            try:
                if path.stat().st_size > MAX_RECORD_BYTES // 2:
                    continue
                body = _read_exact(path)
                updated = _iso_mtime(path)
            except (OSError, UnicodeDecodeError):
                continue
            out[note_id] = {"id": note_id, "uid": "", "kind": "N", "created": "", "updated": updated,
                            "rev": 0, "deleted": False, "device": "", "data": {"body": body},
                            "tags": [], "links": []}
    return out


def _note_target(wiki_dir: Path, note_id: str, *, create_dir: bool) -> Path:
    """The file for *note_id*, proven to sit directly in the wiki directory or
    its ``arch/``/``research/`` folder; ``ValueError`` otherwise."""
    if not is_note_id(note_id):
        raise ValueError("not a note id")
    base = wiki_dir.resolve()
    rel = Path(note_id)
    folder = base / rel.parent
    if create_dir and not folder.exists():
        folder.mkdir()
    real = folder.resolve()
    if real != base and real.parent != base:
        raise ValueError("note folder escapes the wiki")
    if real != base and real.name not in ("arch", "research"):
        raise ValueError("note folder is not allowed")
    target = real / rel.name
    if target.is_symlink():
        raise ValueError("note path is a symlink")
    return target


def write_note(wiki_dir: Path, note_id: str, body: str) -> None:
    """Atomically write a note (temp file + rename in the same folder)."""
    target = _note_target(wiki_dir, note_id, create_dir=True)
    fd, tmp = tempfile.mkstemp(prefix=".shapa-note.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def note_text(wiki_dir: Path, note_id: str) -> str | None:
    """The note file's text, ``None`` when absent, unreadable or unsafe."""
    try:
        target = _note_target(wiki_dir, note_id, create_dir=False)
        return _read_exact(target) if target.is_file() else None
    except (ValueError, OSError, UnicodeDecodeError):
        return None


def remove_note(wiki_dir: Path, note_id: str) -> bool:
    try:
        target = _note_target(wiki_dir, note_id, create_dir=False)
        target.unlink()
        return True
    except (ValueError, OSError):
        return False


# --- validating a record from the server ------------------------------------------------------

def _line(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(SPECIAL_LINE_RE.sub(" ", value).split())[:limit]


def _opt_line(value, limit: int) -> str | None:
    return None if value is None else _line(value, limit)


def _body(value) -> str | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_RECORD_BYTES:
        return None
    return _BODY_STRIP_RE.sub("", value)


def _data(kind: str, raw: dict) -> dict | None:
    """Normalized ``data`` for *kind*, or ``None`` if it is malformed."""
    body = _body(raw.get("body", ""))
    if body is None:
        return None
    body = redact(body)
    if kind == "N":
        return {"body": body}
    alias = _opt_line(raw.get("alias"), 200) or None
    if kind in db.MRI_KINDS:
        summary = _line(raw.get("summary"), db.SUMMARY_MAX)
        source, session = _line(raw.get("source", ""), 200), _line(raw.get("session", ""), 64)
        if not summary or source is None or session is None:
            return None
        return {"summary": redact(summary), "body": body, "alias": alias, "source": source,
                "session": session}
    title = _line(raw.get("title"), db.TITLE_MAX)
    if not title:
        return None
    if kind == "D":
        mime = raw.get("mime") or db.DEFAULT_MIME
        source = _line(raw.get("source", ""), 200)
        if not isinstance(mime, str) or not db._MIME_RE.match(mime.lower()) or source is None:
            return None
        return {"title": redact(title), "body": body, "alias": alias, "mime": mime.lower(),
                "source": source}
    parent = raw.get("parent")
    if parent is not None and not (isinstance(parent, str) and db.ID_RE.match(parent)
                                   and db.PARENT_KIND.get(kind) == parent[0]):
        return None
    status, reason, closed = raw.get("status", "open"), raw.get("closed_reason"), raw.get("closed")
    if status not in ITEM_STATUSES or (reason is not None and reason not in db.CLOSE_REASONS):
        return None
    if closed is not None and not (isinstance(closed, str) and ISO_RE.match(closed)):
        return None
    # `verify` is a shell command run by `shapa ledger close`: it never leaves a machine, so it
    # is never read from the server either.
    return {"title": redact(title), "body": body, "parent": parent, "status": status,
            "closed_reason": reason, "git_ref": _opt_line(raw.get("git_ref"), 200),
            "alias": alias, "claimed_by": _opt_line(raw.get("claimed_by"), 200), "closed": closed}


def _tags(raw) -> list[str] | None:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    tags = {t for t in (_line(x, 80) for x in raw[:MAX_TAGS] if isinstance(x, str)) if t}
    return sorted(tags)


def _links(raw) -> list[list[str]] | None:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    out = set()
    for item in raw[:MAX_LINKS]:
        if (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str)
                and item[1] in db.LINK_RELS
                and (db.ID_RE.match(item[0]) or is_note_id(item[0]))):
            out.add((item[0], item[1]))
    return sorted([dst, rel] for dst, rel in out)


def validate_record(raw) -> dict | None:
    """A normalized, redacted, bounded record from a server record, or
    ``None`` when it must be ignored."""
    if not isinstance(raw, dict):
        return None
    try:
        if len(json.dumps(raw)) > MAX_RECORD_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    rec_id, kind, rev, deleted = raw.get("id"), raw.get("kind"), raw.get("rev"), raw.get("deleted")
    if kind not in tuple(KIND_ORDER) or not id_matches_kind(rec_id, kind):
        return None
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0 or not isinstance(deleted, bool):
        return None
    created, updated = raw.get("created"), raw.get("updated", "")
    if kind == "N":
        if created not in ("", None):
            return None
        created = ""
    elif not (isinstance(created, str) and ISO_RE.match(created)):
        return None
    if not isinstance(updated, str) or not (ISO_RE.match(updated) or (deleted and updated == "")):
        return None
    uid = raw.get("uid")
    if kind == "N":
        if uid not in ("", None):
            return None
        uid = ""
    elif not (isinstance(uid, str) and UID_RE.match(uid)):
        return None
    base = {"id": rec_id, "uid": uid, "kind": kind, "created": created, "updated": updated, "rev": rev,
            "deleted": deleted, "device": _line(raw.get("device", ""), 80) or ""}
    if deleted:
        return {**base, "data": {}, "tags": [], "links": []}
    data, tags, links = (_data(kind, raw.get("data") if isinstance(raw.get("data"), dict) else {}),
                         _tags(raw.get("tags")), _links(raw.get("links")))
    if data is None or tags is None or links is None:
        return None
    try:
        # A stage is one digit on a rule, a memory or a skill version, never two, never on a task
        # or a plain document: a record that breaks it is refused here, so a server cannot put
        # an ill-formed stage (or a second one beside the baseline's) into the local database.
        db.check_stage_tags(tags, kind)
        db.check_tag_spelling(tags)
    except db.LedgerError:
        return None
    return {**base, "data": data, "tags": tags, "links": links}
