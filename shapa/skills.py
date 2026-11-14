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
    shapa skill uproot NAME [--json]       # from a repo: move its skill to the global wiki
"""

from __future__ import annotations

import argparse
import json
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
        # activation is the operator's (CLI only), so it may touch a baseline version's tags
        db.retag(conn, v.doc_id, add=[STATUS_TAG + status], drop_prefix=STATUS_TAG, privileged=True)


def _inherited_stage(conn, prev: SkillVersion | None, privileged: bool) -> list[str]:
    """The stage a new version takes from the one before it, so the baseline survives an
    update. ``stage:0`` is carried only by the operator's own CLI; a lesson or an agent's
    version of a baseline skill is *proposed* for it."""
    if prev is None:
        return []
    n = db.stage_of(db._tags_of(conn, prev.doc_id))
    if n is None:
        return []
    return [f"{db.STAGE_PREFIX}{n}"] if n or privileged else [db.STAGE_PROPOSED]


def add_version(conn, name: str, description: str, body: str, *, new: bool = False,
                active: bool = False, source: str = "skill", privileged: bool = False) -> SkillVersion:
    """Write the next version of a skill. ``new`` starts a skill (refused when it
    exists), otherwise the skill must exist. ``active`` makes it the active
    version (the one before becomes ``previous``); otherwise it is ``pending``.
    *privileged* (the local CLI) lets a new version of a baseline skill stay on stage 0."""
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
                        privileged=privileged,
                        tags=[db.SKILL_TAG, f"{NAME_TAG}{name}", f"{VERSION_TAG}{number}",
                              f"{STATUS_TAG}{'pending'}",
                              *_inherited_stage(conn, existing[-1] if existing else None, privileged)])
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
    from shapa import stages

    olds = [v for v in vs if v.status == "active" and v.doc_id != target.doc_id]
    stage = db.stage_of(db._tags_of(conn, target.doc_id))
    if stage is None and olds:  # the baseline survives an activation
        stage = db.stage_of(db._tags_of(conn, olds[-1].doc_id))
    if stage == 0:  # refuse before changing anything
        left = sum(stages.record_size(conn, i) for i in stages.baseline_ids(conn)
                   if i not in {o.doc_id for o in olds} | {target.doc_id})
        if left + len(target.description) > db.BASELINE_BUDGET:
            raise db.LedgerError(f"activating {name} v{target.version} would take the baseline past "
                                 f"{db.BASELINE_BUDGET} characters (`shapa stage list`)")
    for v in olds:
        _set_status(conn, v, "previous")
    if stage is not None and db.stage_of(db._tags_of(conn, target.doc_id)) is None:
        # activating is the operator reviewing the version: the baseline it replaces passes to it
        db.retag(conn, target.doc_id, add=[f"{db.STAGE_PREFIX}{stage}"], drop=[db.STAGE_PROPOSED],
                 privileged=True)
    _set_status(conn, target, "active")
    return get(conn, name, target.version)


# --- uprooting a repo's skill to the global wiki ---------------------------------------------------

def _db_file(conn) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ""


def _same_file(a, b) -> bool:
    fa, fb = _db_file(a), _db_file(b)
    return bool(fa) and bool(fb) and Path(fa).resolve() == Path(fb).resolve()


def _carried_tags(tags) -> list[str]:
    """The tags a skill version takes along besides its four own: its stage (``stage:0`` never
    travels, it is *proposed* on the other side) and whatever else it carries."""
    out = []
    for t in tags:
        if t == db.SKILL_TAG or t.startswith((NAME_TAG, VERSION_TAG, STATUS_TAG)) or t == db.STAGE_PROPOSED:
            continue
        out.append(db.STAGE_PROPOSED if t == f"{db.STAGE_PREFIX}0" else t)
    return out


def uproot(repo, glob, name: str) -> dict:
    """Move the repo wiki's skill *name* to the global wiki, so every repo has it and it exists once.

    No global skill of that name: every version is recreated there with its number, status, body
    and tags. One already there: the repo's active version (else its latest) is filed as the next
    version, ``pending`` (nothing active changes; a body the global skill already holds files
    nothing). The repo's versions are deleted only after the global side reads back as expected, so
    a failed write loses nothing. ``stage:0`` becomes ``stage-proposed:0`` on the way. Local CLI only:
    it reaches the hosted store as ordinary puts and tombstones."""
    name = _name(name)
    if _same_file(repo, glob):
        raise db.LedgerError("this is the global wiki already: run uproot from a repo")
    mine = versions(repo, name)
    if not mine:
        return {"name": name, "moved": 0, "filed": [], "note": f"no skill {name!r} in this repo's wiki"}
    theirs = versions(glob, name)
    filed: list[int] = []
    if not theirs:
        for v in mine:
            doc = db.get_doc(repo, v.doc_id)
            db.add_doc(glob, doc.title, doc.body, source="uproot", system=True,
                       alias=f"{NAME_TAG}{name}.v{v.version}", privileged=True,
                       tags=[db.SKILL_TAG, f"{NAME_TAG}{name}", f"{VERSION_TAG}{v.version}",
                             f"{STATUS_TAG}{v.status}", *_carried_tags(doc.tags)])
            filed.append(v.version)
        got = [(v.version, v.status, v.body) for v in versions(glob, name)]
        ok = got == [(v.version, v.status, v.body) for v in mine]
    else:
        cand = next((v for v in reversed(mine) if v.status == "active"), mine[-1])
        if not any(t.body == cand.body for t in theirs):
            filed.append(add_version(glob, name, cand.description, cand.body, source="uproot",
                                     privileged=True).version)
        ok = any(t.body == cand.body for t in versions(glob, name))
    if not ok:
        raise db.LedgerError(f"the global wiki did not read back as written: the repo's {name!r} is kept")
    db.delete(repo, [v.doc_id for v in mine])
    return {"name": name, "moved": len(mine), "filed": filed, "into_existing": bool(theirs)}


def shadowing(repo, glob) -> list[str]:
    """Names a repo wiki's skill shares with a global one (the repo's copy hides the global's)."""
    return sorted(set(_all(repo)) & set(_all(glob)))


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
    ur = add("uproot", help="move this repo's skill to the global wiki, so every repo has it")
    ur.add_argument("name")
    ur.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cmd = args.cmd or "list"

    if cmd == "uproot":
        repo, glob = _connect(scoped_root("repo", args.cwd)), _connect(scoped_root("global", args.cwd))
        try:
            res = uproot(repo, glob, args.name)
        finally:
            repo.close()
            glob.close()
        if args.json:
            print(json.dumps(res, indent=2))
        elif not res["moved"]:
            print(res["note"])
        elif res["into_existing"]:
            print(f"{res['name']}: " + (f"filed v{res['filed'][0]} as pending on the global skill"
                                        if res["filed"] else "the global skill already holds this text")
                  + "; the repo copy is gone")
        else:
            print(f"{res['name']}: {res['moved']} version(s) moved to the global wiki; the repo copy is gone")
        return 0

    if cmd in ("list", "show"):
        scope = getattr(args, "scope", None)
        targets = [(scope, scoped_root(scope, args.cwd))] if scope else [
            (wr.kind, Path(wr.path)) for wr in _roots(args.cwd)
            if wr.kind in ("global", "repo") and db.exists(wr.path)]
        found = False
        in_global: set[str] = set()
        if cmd == "list" and any(label == "repo" for label, _ in targets):
            for label, root in targets:
                if label == "global":
                    gconn = _connect(root)
                    try:
                        in_global = set(_all(gconn))
                    finally:
                        gconn.close()
        for label, root in targets:
            conn = _connect(root)
            try:
                if cmd == "list":
                    for s in summaries(conn):
                        pend = f", pending v{', v'.join(map(str, s['pending']))}" if s["pending"] else ""
                        act = f"active v{s['active']}" if s["active"] else "none active"
                        print(f"{s['name']} [{label}] {act}{pend} - {s['description']}")
                        if label == "repo" and s["name"] in in_global:
                            print(f"  note: this repo's {s['name']} shadows the global skill of the same name; "
                                  f"`shapa skill uproot {s['name']}` moves it to global")
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
                        active=cmd == "add" or args.activate, source="cli", privileged=True)
        print(_line(v))
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    run(run_skill, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
