"""Opt-in memri importer - ``shapa upgrade PATH --import-memri FILE`` (never
runs as part of a plain upgrade).

A memri export is a JSON array of rows::

    {"id": "...", "type": "rule|issue|memory|persona", "title": "...",
     "body": "...", "tags": [...], "repo": "...", "created_at": "...",
     "superseded_by": "..."}

Each row's body is split into atomic pieces (:func:`split_body`) and each
piece becomes one v3 memory record (:mod:`shapa.memlog`) via the same
``make_record``/``append`` path everything else in the log uses - same
redaction, same content-derived id, same dedup. A row whose
``superseded_by`` names another row gets every one of its own resulting
records archived (never deleted) with that reason.

Idempotent: re-importing the same file produces the same ``(summary, body)``
for every piece, hence the same content-hash id, hence every record is an
exact duplicate the second time and nothing new is written.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from shapa import memlog, redact, validate
from shapa.score import _parse_ts

#: Row ``type`` -> the memory kind it defaults to when the piece's own text
#: doesn't trip a stronger keyword class (memlog.classify_kind's own
#: priority - gotcha > open_question > preference > outcome - still runs
#: first; this is only the fallback). An issue row whose piece reads like a
#: resolved outcome ("fixed", "shipped", ...) naturally classifies as
#: "outcome" instead of this default, since classify_kind tries that
#: keyword class before falling back to *default*.
TYPE_TO_KIND = {"rule": "preference", "issue": "gotcha", "memory": "fact", "persona": "preference"}

#: A single block longer than this, with >=2 sentences, splits into
#: sentence-sized pieces rather than staying one atomic record.
SENTENCE_SPLIT_CHARS = 220

#: Append in batches this big per :func:`shapa.memlog.append` call (its
#: default MAX_APPEND is a per-hook flood guard; a bulk importer passes its
#: own, much larger, limit - and chunking keeps each call's near-duplicate
#: scan (O(chunk x live-of-same-kind)) from growing unbounded).
CHUNK_SIZE = 500

_BLANK_LINE_RE = re.compile(r"\n\s*\n")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(\S.*)$")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def split_body(body: str, title: str) -> list[str]:
    """Split one memri row's body into atomic pieces.

    Paragraphs (blank-line separated) first. A body that is a single block
    with >=2 bullet lines splits into bullets; a single block over
    :data:`SENTENCE_SPLIT_CHARS` chars with >=2 sentences splits into
    sentences; otherwise the whole block is one piece. An empty body falls
    back to the row's own title (one piece), or no pieces at all."""
    body = (body or "").strip()
    if not body:
        title = _one_line(title)
        return [title] if title else []

    paras = [p.strip() for p in _BLANK_LINE_RE.split(body) if p.strip()]
    if len(paras) > 1:
        return paras

    block = paras[0] if paras else body
    bullets = [m.group(1).strip() for ln in block.splitlines() if (m := _BULLET_RE.match(ln))]
    if len(bullets) >= 2:
        return bullets

    if len(block) > SENTENCE_SPLIT_CHARS:
        sentences = [s.strip() for s in _SENTENCE_RE.split(block) if s.strip()]
        if len(sentences) >= 2:
            return sentences

    return [block]


@dataclass
class ImportStats:
    rows: int = 0
    pieces: int = 0
    written: int = 0
    duplicates: int = 0
    archived: int = 0
    redaction_hits: int = 0
    seconds: float = 0.0
    dry_run: bool = False


@dataclass
class _Piece:
    record: memlog.Record
    created: datetime
    archive_reason: str | None


def _repo_or_none(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _row_created(row: dict) -> datetime:
    return _parse_ts(row.get("created_at")) or datetime.now(timezone.utc)


def _build_pieces(rows: list, bucket: str) -> tuple[list[_Piece], ImportStats]:
    stats = ImportStats()
    pieces: list[_Piece] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stats.rows += 1
        rtype = str(row.get("type", "")).strip().lower()
        default_kind = TYPE_TO_KIND.get(rtype, "fact")
        title = str(row.get("title") or "")
        body = str(row.get("body") or "")
        row_tags = [str(t) for t in (row.get("tags") or []) if isinstance(t, str)]
        repo = _repo_or_none(row.get("repo"))
        row_id = str(row.get("id") or "")
        created = _row_created(row)
        superseded_by = row.get("superseded_by")
        archive_reason = f"memri-superseded-by:{superseded_by}" if superseded_by else None

        texts = split_body(body, title)
        for i, piece in enumerate(texts):
            stats.pieces += 1
            summary = title if i == 0 else piece
            _, hits_s = redact.scrub(_one_line(summary))
            _, hits_b = redact.scrub(_one_line(piece))
            stats.redaction_hits += hits_s + hits_b
            kind = memlog.classify_kind(piece, default=default_kind)
            tags = row_tags + memlog.extract_tags(piece)
            record = memlog.make_record(
                kind=kind, summary=summary, body=piece, tags=tags,
                source=f"memri:{row_id}#{i}", repo=repo, scope=bucket,
                created=memlog.now_iso(created),
            )
            if record is None:
                continue
            pieces.append(_Piece(record=record, created=created, archive_reason=archive_reason))
            if archive_reason:
                stats.archived += 1
    return pieces, stats


def import_memri(root, path, *, dry_run: bool = False) -> ImportStats:
    """Import the memri export at *path* into *root*'s memory log.

    With *dry_run*, nothing is written - the returned stats describe what
    would happen (``written``/``duplicates`` are 0; ``pieces``,
    ``redaction_hits`` and ``archived`` - the pieces of superseded rows -
    are computed without a write). After a real import ``archived`` counts
    the records actually archived."""
    import time

    t0 = time.monotonic()
    root = Path(root)
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a JSON array of rows")
    bucket = validate.bucket_of(root)

    pieces, stats = _build_pieces(rows, bucket)
    stats.dry_run = dry_run
    if dry_run or not pieces:
        stats.seconds = time.monotonic() - t0
        return stats

    by_month: dict[tuple[int, int], list[_Piece]] = {}
    for p in pieces:
        by_month.setdefault((p.created.year, p.created.month), []).append(p)

    written = duplicates = archived = 0
    for key, group in by_month.items():
        rep_dt = datetime(key[0], key[1], 15, tzinfo=timezone.utc)
        for i in range(0, len(group), CHUNK_SIZE):
            chunk = group[i:i + CHUNK_SIZE]
            live = [c.record for c in chunk if not c.archive_reason]
            if live:
                result = memlog.append(root, live, now=rep_dt, limit=len(live))
                written += len(result.written)
                duplicates += len(result.duplicates)
            # A superseded row is kept as history and archived in the same
            # write. It never dedups against or supersedes live memory (its
            # successor usually reads alike), and an identical live record
            # from another row is never archived on its behalf.
            dead = [c for c in chunk if c.archive_reason]
            if dead:
                result = memlog.append(
                    root, [c.record for c in dead], dedup=False, now=rep_dt, limit=len(dead),
                    archive_written={c.record.id: c.archive_reason for c in dead})
                written += len(result.written)
                duplicates += len(result.duplicates)
                archived += len(result.written)

    stats.written = written
    stats.duplicates = duplicates
    stats.archived = archived
    stats.seconds = time.monotonic() - t0
    return stats
