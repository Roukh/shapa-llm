"""``shapa doc`` - documents (kind ``D``) in a wiki's database.

A document is a titled body kept whole (a spec, a runbook, a research dump):
unlike a memory it is never swept for staleness, and recall shows only its
title until ``shapa get D<n>`` (or its alias) prints the text. Tags and links
work as for rows (``shapa row tag|link``).

CLI::

    shapa doc add TITLE --scope global|repo [--body TEXT | --file PATH | stdin]
                        [--alias A] [--tag T ...] [--mime TYPE]
                        [--project KEY] [--merge]
    shapa doc edit ID [--title T] [--body TEXT | --file PATH] [--alias A] [--mime TYPE]
    shapa doc rm ID...
    shapa doc list [--scope global|repo]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shapa import db
from shapa.ledger import CommandError, _connect, _roots, _subcommand_adder, run, scoped_root

MAX_BODY_BYTES = 1_000_000  # the contract's record limit


def _read_capped(stream, label: str) -> str:
    raw = stream.read(MAX_BODY_BYTES + 1)
    data = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    if len(data.encode("utf-8")) > MAX_BODY_BYTES:
        raise CommandError(f"{label} is larger than {MAX_BODY_BYTES} bytes")
    return data


def _body_from(args, *, allow_stdin: bool) -> str | None:
    """The body from ``--body``, ``--file`` or (for ``add``) piped stdin;
    ``None`` when none was given."""
    if args.body is not None and args.file:
        raise CommandError("give --body or --file, not both")
    if args.body is not None:
        return args.body
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
                return _read_capped(fh, args.file).strip()
        except OSError as exc:
            raise CommandError(f"cannot read {args.file}: {exc.strerror or exc}") from exc
    if allow_stdin and not sys.stdin.isatty():
        return _read_capped(sys.stdin, "stdin").strip()
    return None


def _root_holding(doc_id: str, scope, cwd) -> Path:
    if scope:
        return scoped_root(scope, cwd)
    for wr in _roots(cwd):
        if not db.exists(wr.path):
            continue
        conn = db.connect(wr.path)
        try:
            if db.get_doc(conn, doc_id):
                return Path(wr.path)
        finally:
            conn.close()
    raise CommandError(f"no wiki in scope holds document {doc_id}")


def _add_body_flags(parser) -> None:
    parser.add_argument("--body", default=None)
    parser.add_argument("--file", default=None, help="read the body from this file")
    parser.add_argument("--mime", default=None, help=f"default {db.DEFAULT_MIME}")


def run_doc(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa doc", description="Documents in a wiki's database.")
    p.add_argument("--cwd", default=None)
    sub = p.add_subparsers(dest="cmd")
    add = _subcommand_adder(sub)
    ls = add("list")
    ls.add_argument("--scope", choices=["repo", "global"], default=None)
    a = add("add")
    a.add_argument("title")
    a.add_argument("--scope", required=True, choices=["repo", "global"],
                   help="placement.md: would this still be true in a different repo tomorrow?")
    a.add_argument("--alias")
    a.add_argument("--tag", action="append", default=[])
    a.add_argument("--source", default="agent")
    a.add_argument("--project", default=None,
                   help="the project or subproject this document belongs to (default: this wiki's "
                        "own); a project keeps one document")
    a.add_argument("--merge", action="store_true",
                   help="when the project already has its document, append this one to it as a "
                        "section instead of refusing")
    _add_body_flags(a)
    ed = add("edit")
    ed.add_argument("id")
    ed.add_argument("--title")
    ed.add_argument("--alias")
    ed.add_argument("--scope", choices=["repo", "global"], default=None)
    _add_body_flags(ed)
    rm = add("rm")
    rm.add_argument("ids", nargs="+")
    rm.add_argument("--scope", choices=["repo", "global"], default=None)
    mg = add("merge", help="fold a duplicate project document into the project's one document "
                           "(a second device raced it): SRC is appended to DST as a section, then removed")
    mg.add_argument("src")
    mg.add_argument("dst")
    mg.add_argument("--scope", choices=["repo", "global"], default=None)
    args = p.parse_args(argv)
    cmd = args.cmd or "list"

    if cmd == "list":
        scope = getattr(args, "scope", None)
        targets = [scoped_root(scope, args.cwd)] if scope else [
            Path(wr.path) for wr in _roots(args.cwd) if db.exists(wr.path)]
        for root in targets:
            conn = _connect(root)
            try:
                print(f"# {root}")
                for d in db.documents(conn):
                    alias = f" [{d.alias}]" if d.alias else ""
                    print(f"{d.id}{alias} {d.title}")
                for key, ids in db.project_overages(conn).items():
                    what = f"project {key!r}" if key else "this wiki's project"
                    print(f"! {what} is over the limit: {len(ids)} documents ({', '.join(ids)}), "
                          f"a project keeps one - fold them with `shapa doc merge {ids[-1]} {ids[0]}`")
            finally:
                conn.close()
        return 0
    if cmd == "add":
        body = _body_from(args, allow_stdin=True)
        if body is None:
            raise CommandError("a body is required: --body, --file, or piped stdin")
        conn = _connect(scoped_root(args.scope, args.cwd), create=True)
        try:
            known = {d.id for d in db.documents(conn)}
            new_id = db.add_doc(conn, args.title, body, tags=args.tag, alias=args.alias,
                                mime=args.mime, source=args.source, project=args.project,
                                merge=args.merge)
            print(new_id if new_id not in known else f"{new_id} (merged as a section)")
        finally:
            conn.close()
        return 0
    root = _root_holding(args.id if cmd == "edit" else args.src if cmd == "merge" else args.ids[0],
                         args.scope, args.cwd)
    conn = _connect(root)
    try:
        if cmd == "merge":
            print(db.merge_docs(conn, args.src, args.dst))
        elif cmd == "edit":
            d = db.update_doc(conn, args.id, title=args.title, body=_body_from(args, allow_stdin=False),
                              alias=args.alias, mime=args.mime)
            print(f"{d.id} {d.title}")
        else:
            print(f"removed {db.delete(conn, [i for i in args.ids if db.get_doc(conn, i)])}")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    run(run_doc, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
