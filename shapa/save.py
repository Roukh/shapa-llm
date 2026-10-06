"""``shapa save`` - the explicit, operator/agent-invoked write path.

The CLI counterpart to the MCP ``save`` tool (``shapa/mcp.py``, Slice 6) and
what used to be roukh-llm's ``memory.py save --global``/``--repo`` gate
(spec §7): ``shapa save --summary ... --body ...`` for a human, a script, or
a harness with no MCP client to reach directly.

``scope`` is never guessed, but it is not always required on the command
line either - see :func:`infer_scope`. Two of the three CLI scopes match
the MCP tool exactly (``global``/``repo``); the third, ``external``, is a
CLI-only convenience (spec §10 decision 4): it writes into a *different*
repo's own wiki, found by name via :func:`shapa.config.find_sibling_repo`
- never the global wiki, and never silently. The note that lands there is
written with ``scope: repo`` (matching its physical bucket - decision 6
retired ``external`` as a *stored* frontmatter value; here it only ever
describes which repo's wiki the write is headed to).

Unlike ``capture.py``'s Stop-hook contract (auto-invoked, must never block
the agent), this is a manual, explicit write: a bad ``--scope``/
``--applies-to`` combination surfaces as a clear, nonzero-exit error
instead of being swallowed.

CLI::

    shapa save --summary "..." --body "..." [--scope global|repo|external]
               [--applies-to REPO] [--type memory|rule|issue]
               [--tags a,b,c] [--id explicit-id] [--root DIR]
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shapa import config
from shapa.validate import validate_node

#: Every scope this CLI accepts. ``external`` is CLI-only (see module
#: docstring) - the note ends up stored as ``scope: repo`` regardless.
CLI_SCOPES = {"global", "repo", "external"}
VALID_NOTE_TYPES = {"memory", "rule", "issue"}
MAX_SUMMARY_CHARS = 160  # F04

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:60] if slug else f"note-{uuid.uuid4().hex[:8]}"


def _is_safe_note_id(note_id: str) -> bool:
    """Reject anything that could turn ``root / f"{note_id}.md"`` into a
    path outside ``root`` before any write happens - same guard as
    ``shapa.mcp``'s ``_is_safe_note_id``, a caller-supplied ``id`` reaching
    a filesystem write is attacker/agent-controlled input either way."""
    if not note_id or note_id in (".", ".."):
        return False
    if "/" in note_id or "\\" in note_id or "\x00" in note_id:
        return False
    return True


def infer_scope(start: str | Path | None, applies_to: str | None) -> str:
    """The default scope when ``--scope`` is not given on the command line -
    unambiguous, never a guess:

    - ``external`` if ``--applies-to`` was given (that flag has no other
      meaning, so its presence alone settles it).
    - Else ``repo`` if *start* already sits inside a repo-local wiki
      (:func:`shapa.config.discover` finds one).
    - Else ``global``.
    """
    if applies_to:
        return "external"
    return "repo" if config.discover(start) is not None else "global"


def save_row(root, scope: str, note_type: str, summary: str, body: str, tags, alias=None) -> dict:
    """Format 4: a memory/rule/issue is a row in the wiki's database, never a
    note file. *alias* keeps a caller-supplied slug findable by ``get``."""
    from shapa import db

    kind = {"memory": "M", "rule": "R", "issue": "I"}[note_type]
    conn = db.connect(root)
    try:
        rid, created = db.add_row(conn, kind, summary, body, tags=tags or [], alias=alias or None,
                                  source="save")
    except db.LedgerError as exc:
        return {"error": str(exc)}
    finally:
        conn.close()
    out = {"id": rid, "path": str(db.db_path(root)), "scope": scope}
    if not created:
        out["warnings"] = [f"identical text already stored as {rid}"]
    return out


def save_note(
    scope: str | None,
    summary: str,
    body: str,
    *,
    note_type: str = "memory",
    tags: list[str] | None = None,
    id: str | None = None,  # noqa: A002 - matches the MCP `save` tool's public name
    applies_to: str | None = None,
    start: str | Path | None = None,
) -> dict[str, Any]:
    """Write one note. Returns ``{"id", "path", "scope", ...}`` on success,
    or ``{"error": "..."}`` on any ordinary bad-input case - this function
    never raises for those; an unreadable/unwritable filesystem still
    raises OSError, same as every other write path in this codebase."""
    resolved_scope = scope.strip() if scope else infer_scope(start, applies_to)
    if resolved_scope not in CLI_SCOPES:
        return {"error": f"scope must be one of {sorted(CLI_SCOPES)} (got {resolved_scope!r})"}

    if resolved_scope == "external" and not applies_to:
        return {
            "error": "scope 'external' requires --applies-to <repo> - shapa save "
                     "never guesses which repo an external note belongs to "
                     "(shapa-backend-spec.md §10 decision 4)",
        }
    if applies_to and resolved_scope != "external":
        return {
            "error": f"--applies-to only applies with scope 'external' (got "
                     f"scope {resolved_scope!r})",
        }

    summary = (summary or "").strip()
    if not summary:
        return {"error": "summary is required"}
    if "\n" in summary or len(summary) > MAX_SUMMARY_CHARS:
        return {"error": f"summary must be a single line, <={MAX_SUMMARY_CHARS} chars (F04)"}

    body = (body or "").strip()
    if not body:
        return {"error": "body is required"}

    if note_type not in VALID_NOTE_TYPES:
        return {"error": f"type must be one of {sorted(VALID_NOTE_TYPES)} (got {note_type!r})"}

    tags = tags or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return {"error": "tags must be a list of strings"}

    # ---- resolve the target root, and the scope actually STORED in the
    # note (an "external" write always stores scope: repo - decision 6
    # retired "external" as a note-level value; it only ever describes
    # *which* repo's wiki this write is headed to). ---------------------
    if resolved_scope == "global":
        root = config.global_root()
        stored_scope = "global"
        collision_start = start
    elif resolved_scope == "external":
        repo_path = config.find_sibling_repo(applies_to, start)
        if repo_path is None:
            return {
                "error": f"repo {applies_to!r} was not found locally - shapa save "
                         "never falls back to the global wiki for an external "
                         "write (shapa-backend-spec.md §10 decision 4)",
            }
        root = config.discover(repo_path) or (repo_path / ".shapa")
        stored_scope = "repo"
        collision_start = repo_path
    else:  # "repo"
        found = config.discover(start)
        if found is None:
            return {
                "error": "scope 'repo' requires a repo-local wiki, and none was "
                         f"found from {start!r} - run `shapa init` there first; a "
                         "write never creates one implicitly "
                         "(shapa-backend-spec.md §10 decision 4)",
            }
        root = found
        stored_scope = "repo"
        collision_start = start

    from shapa import db

    if db.exists(root):
        return save_row(root, stored_scope, note_type, summary, body, tags, alias=(id or "").strip())

    note_id = (id or "").strip() or _slugify(summary)
    if not _is_safe_note_id(note_id):
        return {
            "error": f"invalid id {note_id!r} - ids may not contain '/', '\\\\', a "
                     "NUL byte, or be exactly '.' or '..' (path-traversal guard)",
        }
    path = root / f"{note_id}.md"
    if path.exists():
        return {
            "error": f"a note with id '{note_id}' already exists at {path} - pass "
                     "a different id, or edit it directly to update it",
        }
    # An id colliding elsewhere in the target's own wiki_roots() would be an
    # F09 cross-root collision the moment a read merges that root back in -
    # refuse it here, same guard shapa.mcp.tool_save applies.
    for wr in config.wiki_roots(collision_start):
        other_root = Path(wr.path)
        try:
            same = other_root.resolve() == root.resolve()
        except OSError:
            same = other_root == root
        if same:
            continue
        if (other_root / f"{note_id}.md").exists():
            return {
                "error": f"id '{note_id}' already exists in the {wr.kind} wiki "
                         f"({wr.path}) - choose a different id to avoid an F09 "
                         "cross-root collision",
            }

    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_lines = [
        "---",
        f"id: {note_id}",
        f"type: {note_type}",
        f'created: "{now}"',
        "consequence: 5",
        "locus: output",
        "uses: 0",
        f'summary: "{summary}"',
        f"scope: {stored_scope}",
    ]
    if tags:
        meta_lines.append("tags: [" + ", ".join(tags) + "]")
    meta_lines.append("status: active")
    meta_lines.append("---")
    path.write_text("\n".join(meta_lines) + "\n" + body + "\n", encoding="utf-8")

    result = validate_node(path)
    if not result.valid:
        path.unlink(missing_ok=True)
        errors = "; ".join(v.message for v in result.errors)
        return {"error": f"note failed validation and was not saved: {errors}"}

    out: dict[str, Any] = {"id": note_id, "path": str(path), "scope": stored_scope}
    if resolved_scope == "external":
        out["applies_to"] = applies_to
    if result.warnings:
        out["warnings"] = [v.message for v in result.warnings]
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="shapa save",
        description="Write one note explicitly (the CLI counterpart to the MCP "
                     "`save` tool). Never guesses a scope - see placement.md.",
    )
    parser.add_argument(
        "--scope", choices=sorted(CLI_SCOPES), default=None,
        help="Where the note is headed. Omit to infer: 'repo' if cwd is inside "
             "a repo-local wiki, else 'global'. 'external' (needs --applies-to) "
             "writes into a DIFFERENT repo's own wiki.",
    )
    parser.add_argument(
        "--applies-to", default=None, metavar="REPO",
        help="Repo name to write into. Implies --scope external.",
    )
    parser.add_argument("--summary", required=True, help="One line, <=160 chars.")
    parser.add_argument("--body", required=True, help="The note's full body.")
    parser.add_argument("--type", dest="note_type", default="memory",
                        choices=sorted(VALID_NOTE_TYPES))
    parser.add_argument("--tags", default=None, help="Comma-separated tags.")
    parser.add_argument("--id", default=None, help="Explicit id (default: slugified summary).")
    parser.add_argument("--root", default=None,
                        help="Directory to resolve scope from (default: cwd).")
    args = parser.parse_args(argv)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

    result = save_note(
        args.scope, args.summary, args.body,
        note_type=args.note_type, tags=tags, id=args.id,
        applies_to=args.applies_to, start=args.root,
    )
    if "error" in result:
        print(f"shapa save: {result['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"saved '{result['id']}' (scope: {result['scope']}) -> {result['path']}")
    for w in result.get("warnings", []):
        print(f"  warning: {w}")
    sys.exit(0)


if __name__ == "__main__":
    main()
