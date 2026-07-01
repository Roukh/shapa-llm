"""Shared node parser - the contract every other module builds on.

A shapa node is a UTF-8 markdown file: a YAML frontmatter block delimited by
a line that is exactly ``---`` at the top and a matching ``---`` closer,
followed by a plain-prose body.

The frontmatter schema is small and fixed, so this module parses the YAML
subset it needs (scalars, inline ``[a, b]`` lists, and block ``- item`` lists)
with the standard library only - no PyYAML dependency, so the repo stays
clone-and-run.

Everything downstream (graph build, heartbeat, validator) imports `parse`
from here so there is exactly one parser and one notion of "body".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_DELIM = "---"
_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.*)$")


@dataclass
class Parsed:
    """Result of parsing a node file.

    Attributes:
        meta: parsed frontmatter mapping. ``connections`` is always a list of
            strings (possibly empty); other keys are strings.
        body: the body text with surrounding blank lines stripped, lines
            joined by ``\\n``.
        body_lines: list of ``(lineno, text)`` for each body line, where
            ``lineno`` is the original 1-based line number in the file.
            Leading and trailing all-blank padding lines are excluded so the
            validator does not flag the conventional blank line after ``---``
            or a trailing newline at EOF.
        body_start_line: original 1-based line number where the body begins
            (the first non-blank line after the closing delimiter), or 0 if
            the body is empty.
        error: a parse-error string if the frontmatter is malformed, else None.
    """

    meta: dict = field(default_factory=dict)
    body: str = ""
    body_lines: list[tuple[int, str]] = field(default_factory=list)
    body_start_line: int = 0
    error: str | None = None


def _coerce_scalar(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _parse_inline_list(raw: str) -> list[str]:
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    return [_coerce_scalar(part) for part in inner.split(",") if part.strip()]


def _parse_frontmatter(lines: list[str]) -> dict:
    """Parse the YAML subset used by node frontmatter.

    Supports: ``key: scalar``, ``key: [a, b]`` (inline list), and
    ``key:`` followed by indented ``- item`` block-list lines.
    """
    meta: dict = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = _KEY_RE.match(line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()
        if rest.startswith("[") and rest.endswith("]"):
            meta[key] = _parse_inline_list(rest)
            i += 1
        elif rest == "":
            # Possible block list on following indented "- item" lines.
            items: list[str] = []
            j = i + 1
            while j < n:
                im = _LIST_ITEM_RE.match(lines[j])
                if im is None:
                    break
                items.append(_coerce_scalar(im.group(1)))
                j += 1
            meta[key] = items if items else ""
            i = j
        else:
            meta[key] = _coerce_scalar(rest)
            i += 1
    return meta


def parse(path: str | Path) -> Parsed:
    """Parse a node file into frontmatter + body. See `Parsed`."""
    text = Path(path).read_text(encoding="utf-8")
    lines = text.split("\n")
    # Drop a single trailing empty element produced by a final newline.
    if lines and lines[-1] == "":
        lines = lines[:-1]

    if not lines or lines[0].rstrip() != _DELIM:
        # No frontmatter: the whole file is the body.
        meta: dict = {"connections": []}
        return _with_body(meta, lines, body_offset=0, error=None)

    close_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == _DELIM:
            close_idx = idx
            break
    if close_idx is None:
        return Parsed(meta={"connections": []}, error="unterminated frontmatter")

    meta = _parse_frontmatter(lines[1:close_idx])
    conns = meta.get("connections", [])
    if isinstance(conns, str):
        conns = [conns] if conns else []
    meta["connections"] = [str(c).strip() for c in conns if str(c).strip()]

    return _with_body(meta, lines, body_offset=close_idx + 1, error=None)


def _with_body(meta: dict, lines: list[str], body_offset: int, error: str | None) -> Parsed:
    raw = lines[body_offset:]
    # Trim leading and trailing all-blank lines, tracking original line numbers.
    start = 0
    while start < len(raw) and not raw[start].strip():
        start += 1
    end = len(raw)
    while end > start and not raw[end - 1].strip():
        end -= 1

    body_lines: list[tuple[int, str]] = []
    for offset in range(start, end):
        original_lineno = body_offset + offset + 1  # 1-based
        body_lines.append((original_lineno, raw[offset]))

    body = "\n".join(text for _, text in body_lines)
    body_start_line = body_lines[0][0] if body_lines else 0
    return Parsed(
        meta=meta,
        body=body,
        body_lines=body_lines,
        body_start_line=body_start_line,
        error=error,
    )
