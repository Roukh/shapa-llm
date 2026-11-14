"""``shapa skill`` - skills as versioned records, global or per project.

A skill version is a document (kind ``D``) in the wiki that owns the skill: the
global wiki for a skill every repo uses, a repo's own wiki for one that belongs
to it. Nothing here is a new record kind or a schema change; the tags carry the
state, and the record syncs like any document (``hosted.md``):

=================  =========================================================
tag                meaning
=================  =========================================================
``skill``          marks the document as a skill version (not a project document)
``skill:NAME``     which skill (a slug: ``a-z 0-9 -``, up to 64)
``v:N``            the version number, 1 upward
``status:S``       ``active`` (the one in use), ``pending`` (written, not yet
                   chosen) or ``previous`` (an earlier active one, kept)
=================  =========================================================

The body is a ``SKILL.md``: a frontmatter block with the name and a one-line
description, then the text. Every version is kept; ``activate`` moves which one
is active (so a rollback is an activation).

A memory, rule or issue that names a skill, by a ``skill:NAME`` tag or an
``about`` link to one of its versions, files a *pending* new version whose body
is the latest version plus the lesson, every lesson line quoted. Nothing here
ever activates a version by itself.

CLI::

    shapa skill [list] [--scope global|repo]
    shapa skill show NAME [--version N] [--scope ...]
    shapa skill add NAME --scope ... --description TEXT [--body TEXT | --file PATH | stdin]
    shapa skill update NAME --scope ... [--description TEXT] [--body ...] [--activate]
    shapa skill activate NAME [--version N] --scope ...
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from shapa import db
from shapa.docs import _add_body_flags, _body_from
from shapa.ledger import CommandError, _connect, _roots, _subcommand_adder, run, scoped_root

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DESCRIPTION_MAX = 1024
LESSON_BODY_MAX = 1500
MAX_LESSON_SKILLS = 3   # skills one row may file a lesson for
MAX_PENDING = 5         # pending versions of one skill; lessons past it are not filed
STATUSES = ("active", "pending", "previous")
NAME_TAG = "skill:"
VERSION_TAG = "v:"
STATUS_TAG = "status:"
_FRONT_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


@dataclass
class SkillVersion:
    name: str
    version: int
    status: str
    description: str
    body: str
    doc_id: str
    created: str
    updated: str


def _name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise db.LedgerError(f"a skill name is a slug: a-z, 0-9 and -, up to 64 (got {name!r})")
    return name


def _description(text) -> str:
    return db.one_line(text, DESCRIPTION_MAX)


def _split(text: str) -> tuple[dict[str, str], str]:
    """``(frontmatter fields, body)`` of a ``SKILL.md`` text."""
    m = _FRONT_RE.match(text or "")
    if not m:
        return {}, (text or "").strip()
    fields = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_][\w-]*", k.strip()):
            fields[k.strip()] = v.strip()
    return fields, text[m.end():].strip()


def _compose(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def _from_doc(d: db.Doc) -> SkillVersion | None:
    tags = set(d.tags)
    if db.SKILL_TAG not in tags:
        return None
    name = next((t[len(NAME_TAG):] for t in sorted(tags) if t.startswith(NAME_TAG)), "")
    version = next((t[len(VERSION_TAG):] for t in sorted(tags) if t.startswith(VERSION_TAG)), "")
    status = next((t[len(STATUS_TAG):] for t in sorted(tags) if t.startswith(STATUS_TAG)), "pending")
    if not NAME_RE.match(name) or not version.isdigit() or status not in STATUSES:
        return None  # a record that only looks like a skill (a synced one, hand-tagged)
    fields, body = _split(d.body)
    return SkillVersion(name, int(version), status, _description(fields.get("description", "")),
                        body, d.id, d.created, d.updated)


def versions(conn: sqlite3.Connection, name: str) -> list[SkillVersion]:
    """Every version of a skill, oldest first."""
    out = [v for v in (_from_doc(d) for d in db.documents(conn)) if v and v.name == name]
    return sorted(out, key=lambda v: (v.version, v.created))


def _all(conn) -> dict[str, list[SkillVersion]]:
    out: dict[str, list[SkillVersion]] = {}
    for d in db.documents(conn):
        v = _from_doc(d)
        if v:
            out.setdefault(v.name, []).append(v)
    return {k: sorted(vs, key=lambda v: (v.version, v.created)) for k, vs in out.items()}


def get(conn, name: str, version: int | None = None) -> SkillVersion | None:
    """One version: *version* (a pending one included), else the active one. A skill with
    no active version is not served: its pending versions are read by number."""
    vs = versions(conn, name)
    if version is not None:
        return next((v for v in vs if v.version == version), None)
    active = [v for v in vs if v.status == "active"]
    return active[-1] if active else None


def summaries(conn) -> list[dict]:
    out = []
    for name, vs in sorted(_all(conn).items()):
        cur = next((v for v in reversed(vs) if v.status == "active"), vs[-1])
        out.append({"name": name, "description": cur.description,
                    "active": next((v.version for v in reversed(vs) if v.status == "active"), None),
                    "pending": [v.version for v in vs if v.status == "pending"],
                    "versions": len(vs)})
    return out


def as_dict(v: SkillVersion, body: bool = False) -> dict:
    out = {"name": v.name, "version": v.version, "status": v.status,
           "description": v.description, "id": v.doc_id}
    if body:
        out["body"] = v.body
    return out


def _set_status(conn, v: SkillVersion, status: str) -> None:
    if v.status != status:
        db.retag(conn, v.doc_id, add=[STATUS_TAG + status], drop_prefix=STATUS_TAG)


def add_version(conn, name: str, description: str, body: str, *, new: bool = False,
                active: bool = False, source: str = "skill") -> SkillVersion:
    """Write the next version of a skill. ``new`` starts a skill (refused when it
    exists), otherwise the skill must exist. ``active`` makes it the active
    version (the one before becomes ``previous``); otherwise it is ``pending``."""
    name = _name(name)
    existing = versions(conn, name)
    if new and existing:
        raise db.LedgerError(f"skill {name!r} exists: use `shapa skill update {name}` for a new version")
    if not new and not existing:
        raise db.LedgerError(f"no skill {name!r}: use `shapa skill add {name}` to start it")
    fields, text = _split(str(body or ""))
    desc = _description(description) or _description(fields.get("description", ""))
    if not desc and existing:
        desc = existing[-1].description
    if not desc:
        raise db.LedgerError("a skill needs a description: what it does and when to use it")
    if not text:
        raise db.LedgerError("a skill needs a body")
    number = max((v.version for v in existing), default=0) + 1
    full = _compose(name, desc, text)
    if len(full.encode("utf-8")) > db.DOC_BODY_MAX:
        raise db.LedgerError(f"a skill is at most {db.DOC_BODY_MAX} bytes")
    doc_id = db.add_doc(conn, f"{name} v{number}", full, source=source, system=True,
                        alias=f"{NAME_TAG}{name}.v{number}",
                        tags=[db.SKILL_TAG, f"{NAME_TAG}{name}", f"{VERSION_TAG}{number}",
                              f"{STATUS_TAG}{'pending'}"])
    version = get(conn, name, number)
    if active:
        version = activate(conn, name, number)
    return version


def activate(conn, name: str, version: int | None = None) -> SkillVersion:
    """Make one version active: *version*, or the newest pending one. The version
    that was active becomes ``previous``; nothing is deleted."""
    name = _name(name)
    vs = versions(conn, name)
    if not vs:
        raise db.LedgerError(f"no skill {name!r}")
    if version is None:
        pending = [v for v in vs if v.status == "pending"]
        if not pending:
            raise db.LedgerError(f"no pending version of {name!r} to activate")
        target = pending[-1]
    else:
        target = next((v for v in vs if v.version == int(version)), None)
        if target is None:
            raise db.LedgerError(f"no version {version} of {name!r}")
    for v in vs:
        if v.status == "active" and v.doc_id != target.doc_id:
            _set_status(conn, v, "previous")
    _set_status(conn, target, "active")
    return get(conn, name, target.version)


# --- lessons from rows --------------------------------------------------------------------------

def _named_skills(conn, row_id: str) -> list[str]:
    """Skills a row names: a ``skill:NAME`` tag, or an ``about`` link to a version."""
    names: list[str] = []
    for (t,) in conn.execute("SELECT tag FROM tags WHERE id = ? AND tag LIKE 'skill:%'", (row_id,)):
        n = t[len(NAME_TAG):]
        if NAME_RE.match(n) and n not in names:
            names.append(n)
    for (dst,) in conn.execute("SELECT dst FROM links WHERE src = ? AND rel = 'about'", (row_id,)):
        d = db.get_doc(conn, dst) if db.kind_of(dst) == "D" else None
        v = _from_doc(d) if d else None
        if v and v.name not in names:
            names.append(v.name)
    return names


def _quote(text: str, limit: int) -> str:
    text = text.strip()[:limit]
    return "\n".join("> " + line for line in text.splitlines() if line.strip())


def lesson_from_row(conn, row_id: str) -> list[SkillVersion]:
    """File a pending version for each skill the row names (once per row). The
    new body is the skill's latest version plus the lesson, quoted line by line
    so it can open no heading and no frontmatter of its own. Never activates."""
    row = db.get_row(conn, row_id)
    if row is None:
        return []
    out = []
    for name in _named_skills(conn, row_id)[:MAX_LESSON_SKILLS]:
        vs = versions(conn, name)
        if not vs:
            continue
        base = vs[-1]
        marker = f"[{row.id}]"
        if any(marker in v.body for v in vs):
            continue
        if sum(1 for v in vs if v.status == "pending") >= MAX_PENDING:
            continue  # the review queue is full: new lessons wait until the operator clears it
        lesson = f"\n\n## Lesson {marker} ({db.KIND_NAMES[row.kind]}, pending review)\n\n"
        lesson += _quote(row.summary, 200)
        if row.body.strip():
            lesson += "\n>\n" + _quote(row.body, LESSON_BODY_MAX)
        out.append(add_version(conn, name, base.description, base.body + lesson, source=f"lesson:{row.id}"))
    return out


# --- the CLI -------------------------------------------------------------------------------------

def _line(v: SkillVersion) -> str:
    return f"{v.name} v{v.version} {v.status}"


def run_skill(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa skill", description="Skills as versioned records, "
                                "global or per project.")
    p.add_argument("--cwd", default=None)
    sub = p.add_subparsers(dest="cmd")
    add = _subcommand_adder(sub)
    scope_kw = dict(choices=["repo", "global"])
    ls = add("list")
    ls.add_argument("--scope", default=None, **scope_kw)
    sh = add("show")
    sh.add_argument("name")
    sh.add_argument("--version", type=int)
    sh.add_argument("--scope", default=None, **scope_kw)
    a = add("add", help="start a skill: version 1, active")
    a.add_argument("name")
    a.add_argument("--scope", required=True, **scope_kw)
    a.add_argument("--description", default="")
    _add_body_flags(a)
    up = add("update", help="a new version (pending unless --activate)")
    up.add_argument("name")
    up.add_argument("--scope", required=True, **scope_kw)
    up.add_argument("--description", default="")
    up.add_argument("--activate", action="store_true")
    _add_body_flags(up)
    ac = add("activate", help="make the newest pending (or --version N) version the active one")
    ac.add_argument("name")
    ac.add_argument("--version", type=int)
    ac.add_argument("--scope", required=True, **scope_kw)
    args = p.parse_args(argv)
    cmd = args.cmd or "list"

    if cmd in ("list", "show"):
        scope = getattr(args, "scope", None)
        targets = [(scope, scoped_root(scope, args.cwd))] if scope else [
            (wr.kind, Path(wr.path)) for wr in _roots(args.cwd)
            if wr.kind in ("global", "repo") and db.exists(wr.path)]
        found = False
        for label, root in targets:
            conn = _connect(root)
            try:
                if cmd == "list":
                    for s in summaries(conn):
                        pend = f", pending v{', v'.join(map(str, s['pending']))}" if s["pending"] else ""
                        act = f"active v{s['active']}" if s["active"] else "none active"
                        print(f"{s['name']} [{label}] {act}{pend} - {s['description']}")
                    continue
                v = get(conn, args.name, args.version)
                if v is not None:
                    found = True
                    print(f"# {_line(v)} [{label}] {v.doc_id}\n# {v.description}\n\n{v.body}")
            finally:
                conn.close()
        if cmd == "show" and not found:
            raise CommandError(f"no skill {args.name!r} version {args.version}" if args.version else
                               f"no active skill {args.name!r} (a pending version is read with --version N)")
        return 0

    conn = _connect(scoped_root(args.scope, args.cwd), create=cmd == "add")
    try:
        if cmd == "activate":
            print(_line(activate(conn, args.name, args.version)))
            return 0
        body = _body_from(args, allow_stdin=True)
        if body is None:
            raise CommandError("a body is required: --body, --file, or piped stdin")
        v = add_version(conn, args.name, args.description, body, new=cmd == "add",
                        active=cmd == "add" or args.activate, source="cli")
        print(_line(v))
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    run(run_skill, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
