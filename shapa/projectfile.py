"""``shapa project`` - a project's one document, kept in a fixed shape.

A project is the tag ``project:KEY``; its one document (the lean gate of :func:`db.add_doc`) is
written from ``assets/project-template.md``: Issues, Questions, Implementations, then (under a
rule and ``### Architecture:``) Problem, Solution, Diff, advantages, villains, loop_market and
loop_product, each a numbered list. Agents add to a section one numbered line at a time through
:func:`add_item`; nothing else edits the structure, so the document stays readable and a hostile
item can never forge a heading (an item is a single ``N. text`` line).

Tasks, ideas, rules, memories and skills tied to the project carry the same ``project:KEY`` tag
(and an ``about`` link to the document); :func:`show` gathers them.

CLI::

    shapa project new KEY [--title T] [--wiki global|repo]
    shapa project add KEY SECTION TEXT [--wiki global|repo]
    shapa project show KEY [--wiki global|repo] [--json]
    shapa project list [--wiki global|repo] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from shapa import db
from shapa.ledger import CommandError, _connect, _subcommand_adder, run, scoped_root
from shapa.redact import redact

TEMPLATE_PATH = Path(__file__).resolve().parent / "assets" / "project-template.md"
#: The sections of the template, in its order. The keys are the contract's; the headings are
#: the template's own spelling (read from the asset, never repeated here).
SECTIONS = ("issues", "questions", "implementations", "problem", "solution", "diff",
            "advantages", "villains", "loop_market", "loop_product")
ARCHITECTURE_FROM = "problem"
ITEM_MAX = 500
LIST_CAP = 50
SECTION_ITEMS_SHOWN = 100
OPEN_TASKS_IN_LIST = 12
ARCH_MARK = "### Architecture:"
RULE = "---"
_ITEM_RE = re.compile(r"^(\d{1,9})\.[ \t]*(.*)$")
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def template() -> str:
    """The template text, byte for byte (the one asset; the Worker holds the same text)."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _headings() -> dict[str, str]:
    """``{section key: its heading line}`` as the template spells them."""
    out: dict[str, str] = {}
    for line in template().splitlines():
        key = line.rstrip(":").strip().lower()
        if line.endswith(":") and key in SECTIONS:
            out[key] = line
    return out


def section_key(name: str) -> str:
    key = " ".join(str(name or "").split()).lower().replace(" ", "_").rstrip(":")
    if key not in SECTIONS:
        raise db.LedgerError(f"unknown section {str(name)[:40]!r}: one of {', '.join(SECTIONS)}")
    return key


def clean_item(text: str) -> str:
    """One masked line of at most :data:`ITEM_MAX` characters, or an error: an item with a line
    break or a control character is refused, never flattened (it could be an attempt to open a
    heading of its own)."""
    text = str(text if text is not None else "")
    if _BAD_CHARS.search(text.replace("\t", " ")):
        raise db.LedgerError("a project item is one line: no line breaks or control characters")
    text = " ".join(text.split())
    if not text:
        raise db.LedgerError("a project item needs text")
    if len(text) > ITEM_MAX:
        raise db.LedgerError(f"a project item is at most {ITEM_MAX} characters (got {len(text)})")
    masked = " ".join(redact(text).split())
    return masked


def _is_heading(line: str, key: str) -> bool:
    return line.strip().lower() == f"{key}:"


def _locate(lines: list[str]) -> dict[str, int]:
    """The first heading line of each section found in *lines*."""
    found: dict[str, int] = {}
    for i, line in enumerate(lines):
        if line[:1] in (" ", "\t"):
            continue  # an indented line is never a heading
        for key in SECTIONS:
            if key not in found and _is_heading(line, key):
                found[key] = i
    return found


def _section_end(lines: list[str], start: int, heads: dict[str, int]) -> int:
    stops = [i for i in heads.values() if i > start]
    stops += [i for i, line in enumerate(lines) if i > start and line.strip() in (RULE, ARCH_MARK)]
    return min(stops, default=len(lines))


def _items(lines: list[str], start: int, end: int) -> list[tuple[int, int, str]]:
    out = []
    for i in range(start + 1, end):
        m = _ITEM_RE.match(lines[i])
        if m:
            out.append((i, int(m.group(1)), m.group(2).strip()))
    return out


def sections(body: str) -> dict[str, list[str]]:
    """``{section key: [item text, ...]}`` of a project document (empty placeholders skipped)."""
    lines = (body or "").split("\n")
    heads = _locate(lines)
    return {key: [t for _, _, t in _items(lines, heads[key], _section_end(lines, heads[key], heads)) if t]
            for key in SECTIONS if key in heads}


def add_to_body(body: str, section: str, text: str) -> tuple[str, bool]:
    """``(new body, changed)``: append *text* as the next numbered item of *section* (filling the
    empty placeholder first), create the section at its template position when it is missing, and
    change nothing when the section already holds that text."""
    key = section_key(section)
    text = clean_item(text)
    lines = (body or "").split("\n")
    heads = _locate(lines)
    if key in heads:
        start = heads[key]
        items = _items(lines, start, _section_end(lines, start, heads))
        if any(t == text for _, _, t in items):
            return body, False
        blank = next((i for i, _, t in items if not t), None)
        if blank is not None:
            n = next(n for i, n, _ in items if i == blank)
            lines[blank] = f"{n}. {text}"
        else:
            n = max((n for _, n, _ in items), default=0) + 1
            at = (items[-1][0] if items else start) + 1
            lines.insert(at, f"{n}. {text}")
    else:
        block = [_headings()[key], f"1. {text}", ""]
        later = [heads[k] for k in SECTIONS[SECTIONS.index(key) + 1:] if k in heads]
        marks = [i for i, line in enumerate(lines) if line.strip() == ARCH_MARK]
        at = min(later, default=None)
        if key in SECTIONS[:SECTIONS.index(ARCHITECTURE_FROM)]:
            # a top section goes before the rule that opens the architecture part
            if marks and (at is None or at > marks[0]):
                at = marks[0]
                j = at - 1
                while j >= 0 and lines[j].strip() == "":
                    j -= 1
                if j >= 0 and lines[j].strip() == RULE:
                    at = j
        elif not marks:
            block = [RULE, "", ARCH_MARK, "", *block]
        if at is None:
            while lines and lines[-1].strip() == "":
                lines.pop()
            lines += ["", *block] if lines else block
        else:
            lines[at:at] = block
    new = "\n".join(lines)
    if len(new.encode("utf-8")) > db.DOC_BODY_MAX:
        raise db.LedgerError(f"the project document would pass {db.DOC_BODY_MAX} bytes")
    return new, True


# --- the document in the database ------------------------------------------------------------------

def _doc_of(conn: sqlite3.Connection, key: str) -> db.Doc:
    doc = db.project_doc(conn, key) if key else None
    if doc is None:
        raise db.LedgerError(f"no project {key!r} here: `shapa project new {key}` creates it")
    return doc


def _key(key: str) -> str:
    k = db.clean_project(key)
    if not k or k != str(key):  # exactly the key's own spelling: no case folding, no stray space
        raise db.LedgerError("a project key is lowercase letters, digits and . _ / - (up to 80)")
    return k


def _set_body(conn: sqlite3.Connection, doc_id: str, body: str) -> None:
    title = conn.execute("SELECT title FROM docs WHERE id = ?", (doc_id,)).fetchone()[0]
    with db.write(conn):
        conn.execute("UPDATE docs SET body = ?, hash = ?, updated = ? WHERE id = ?",
                     (body, db.content_hash(title, body), db.now_iso(), doc_id))


def new_project(conn: sqlite3.Connection, key: str, title: str | None = None) -> tuple[str, bool]:
    """Create the project's document from the template; ``(id, created)``, idempotent."""
    key = _key(key)
    held = db.project_doc(conn, key)
    if held is not None:
        return held.id, False
    doc_id = db.add_doc(conn, (title or "").strip() or key, template(), project=key, source="project",
                        tags=[db.PROJECT_PREFIX + key])
    _set_body(conn, doc_id, template())  # add_doc trims the text; the template is kept byte for byte
    return doc_id, True


def add_item(conn: sqlite3.Connection, key: str, section: str, text: str) -> dict:
    """Append *text* to *section* of project *key*; ``{id, section, changed}``."""
    doc = _doc_of(conn, _key(key))
    with db.write(conn):  # read and write under one lock: two appenders never both take number 3
        body = conn.execute("SELECT body FROM docs WHERE id = ?", (doc.id,)).fetchone()[0]
        new, changed = add_to_body(body, section, text)
        if changed:
            conn.execute("UPDATE docs SET body = ?, hash = ?, updated = ? WHERE id = ?",
                         (new, db.content_hash(doc.title, new), db.now_iso(), doc.id))
    return {"id": doc.id, "section": section_key(section), "changed": changed}


def _capped(rows: list[dict], total: int, more: dict, name: str) -> list[dict]:
    if total > LIST_CAP:
        more[name] = total - LIST_CAP
    return rows[:LIST_CAP]


def _members(conn: sqlite3.Connection, key: str) -> dict:
    tag = db.PROJECT_PREFIX + key
    more: dict[str, int] = {}
    out: dict = {}
    # Each list reads its first LIST_CAP rows through the tag index (never the whole membership),
    # and each total comes from tag_counts, the per-tag count the database keeps.
    from shapa import tasks

    totals = {k: int(n) for k, n in conn.execute("SELECT k, n FROM tag_counts WHERE tag = ?", (tag,))} \
        if db._has_table(conn, "tag_counts") else {}

    def total(letter: str, fallback_sql: str) -> int:
        if letter in totals or db._has_table(conn, "tag_counts"):
            return totals.get(letter, 0)
        return int(conn.execute(fallback_sql, (tag,)).fetchone()[0])

    def note(name: str, shown: int, n: int) -> None:
        if n > shown:
            more[name] = n - shown

    rows = conn.execute("SELECT i.id, i.title, i.status FROM tags t JOIN items i ON i.id = t.id "
                        "WHERE t.tag = ? AND t.id >= 'T' AND t.id < 'U' AND i.kind = 'T' "
                        "AND i.status != 'closed' ORDER BY i.created, CAST(substr(i.id, 2) AS INTEGER) LIMIT ?", (tag, LIST_CAP)).fetchall()
    if len(rows) < LIST_CAP:  # closed tasks follow the open ones
        rows += conn.execute("SELECT i.id, i.title, i.status FROM tags t JOIN items i ON i.id = t.id "
                             "WHERE t.tag = ? AND t.id >= 'T' AND t.id < 'U' AND i.kind = 'T' "
                             "AND i.status = 'closed' ORDER BY i.created, CAST(substr(i.id, 2) AS INTEGER) LIMIT ?",
                             (tag, LIST_CAP - len(rows))).fetchall()
    owners = dict(conn.execute(f"SELECT id, tag FROM tags WHERE tag IN ('human_task', 'agent_task') "
                               f"AND id IN ({','.join('?' * len(rows))})", [r[0] for r in rows]).fetchall()) if rows else {}
    out["tasks"] = [{"id": r[0], "title": r[1], "status": r[2],
                     "owner": db.OWNER_TAGS.get(owners.get(r[0], ""), "")} for r in rows]
    note("tasks", len(rows), total("T", "SELECT count(*) FROM tags WHERE tag = ? AND id >= 'T' AND id < 'U'"))
    idea = ("EXISTS (SELECT 1 FROM tags x WHERE x.id = m.id AND x.tag = 'idea')")
    out["rules"] = [{"id": r[0], "summary": r[1]} for r in conn.execute(
        "SELECT m.id, m.summary FROM tags t JOIN mri m ON m.id = t.id WHERE t.tag = ? AND t.id >= 'R' "
        "AND t.id < 'S' AND m.kind = 'R' ORDER BY m.created DESC, CAST(substr(m.id, 2) AS INTEGER) LIMIT ?", (tag, LIST_CAP))]
    note("rules", len(out["rules"]), total("R", "SELECT count(*) FROM tags WHERE tag = ? AND id >= 'R' AND id < 'S'"))
    mem = ("FROM tags t JOIN mri m ON m.id = t.id WHERE t.tag = ? AND t.id >= 'M' AND t.id < 'N' "
           "AND m.kind = 'M' AND ")
    out["ideas"] = [{"id": r[0], "summary": r[1]} for r in conn.execute(
        f"SELECT m.id, m.summary {mem}{idea} ORDER BY m.created DESC, CAST(substr(m.id, 2) AS INTEGER) LIMIT ?", (tag, LIST_CAP))]
    out["memories"] = [{"id": r[0], "summary": r[1]} for r in conn.execute(
        f"SELECT m.id, m.summary {mem}NOT {idea} ORDER BY m.created DESC, CAST(substr(m.id, 2) AS INTEGER) LIMIT ?", (tag, LIST_CAP))]
    n_ideas = (conn.execute(f"SELECT count(*) {mem}{idea}", (tag,)).fetchone()[0]
               if len(out["ideas"]) >= LIST_CAP or out["ideas"] else 0)
    note("ideas", len(out["ideas"]), n_ideas)
    note("memories", len(out["memories"]), max(0, total("M", "SELECT count(*) FROM tags WHERE tag = ? "
                                                        "AND id >= 'M' AND id < 'N'") - n_ideas))
    out["skills"] = []
    for i, title in conn.execute("SELECT d.id, d.title FROM tags t JOIN docs d ON d.id = t.id WHERE t.tag = ? "
                                 "AND t.id >= 'D' AND t.id < 'E' AND EXISTS (SELECT 1 FROM tags s WHERE "
                                 "s.id = d.id AND s.tag = 'skill') ORDER BY d.id LIMIT ?", (tag, LIST_CAP)).fetchall():
        status = conn.execute("SELECT tag FROM tags WHERE id = ? AND tag >= 'status:' AND tag < 'status;' LIMIT 1",
                              (i,)).fetchone()
        out["skills"].append({"id": i, "title": title, "status": status[0][len("status:"):] if status else ""})
    if len(out["skills"]) >= LIST_CAP:
        note("skills", LIST_CAP, conn.execute(
            "SELECT count(*) FROM tags t WHERE t.tag = ? AND t.id >= 'D' AND t.id < 'E' AND EXISTS "
            "(SELECT 1 FROM tags s WHERE s.id = t.id AND s.tag = 'skill')", (tag,)).fetchone()[0])
    return out, more


def show(conn: sqlite3.Connection, key: str, *, features: list[dict] | None = None) -> dict:
    """The project read: its document sections and every record tied to it (each list capped at
    :data:`LIST_CAP`; ``more`` counts the rest). *features* are the open features of a repo wiki
    that has the key (the caller finds it)."""
    key = _key(key)
    doc = _doc_of(conn, key)
    members, more = _members(conn, key)
    secs = sections(doc.body)
    shown = {}
    for name, items in secs.items():
        shown[name] = items[:SECTION_ITEMS_SHOWN]
        if len(items) > SECTION_ITEMS_SHOWN:
            more[f"section:{name}"] = len(items) - SECTION_ITEMS_SHOWN
    feats = list(features or [])
    if len(feats) > LIST_CAP:
        more["features"] = len(feats) - LIST_CAP
    out = {"key": key, "title": doc.title, "document": {"id": doc.id, "sections": shown},
           **members, "features": feats[:LIST_CAP]}
    if more:
        out["more"] = more
    return out


def project_keys(conn: sqlite3.Connection) -> list[str]:
    """Every project that has a document, by key."""
    if db._has_table(conn, "tag_counts"):
        tags = [r[0] for r in conn.execute("SELECT tag FROM tag_counts WHERE tag >= 'project:' AND "
                                           "tag < 'project;' AND k = 'D' ORDER BY tag")]
    else:
        tags = [r[0] for r in conn.execute("SELECT DISTINCT tag FROM tags WHERE tag >= 'project:' AND "
                                           "tag < 'project;' AND id >= 'D' AND id < 'E' ORDER BY tag")]
    return [t[len(db.PROJECT_PREFIX):] for t in tags
            if db.project_doc_id(conn, t[len(db.PROJECT_PREFIX):]) is not None]


def open_task_count(conn: sqlite3.Connection, key: str) -> int:
    return conn.execute("SELECT count(*) FROM tags t JOIN items i ON i.id = t.id WHERE t.tag = ? "
                        "AND t.id >= 'T' AND t.id < 'U' AND i.status != 'closed'",
                        (db.PROJECT_PREFIX + key,)).fetchone()[0]


def listing(conn: sqlite3.Connection, *, limit: int | None = None) -> list[dict]:
    """``[{key, title, open_tasks}]``: with *limit*, the projects with the most tasks first."""
    keys = project_keys(conn)
    # a project's tasks: the tag count less its closed ones (swept after a merge, so few); both
    # come from one range read, not one count per project
    weight = {t[len(db.PROJECT_PREFIX):]: int(n) for t, n in conn.execute(
        "SELECT tag, n FROM tag_counts WHERE tag >= 'project:' AND tag < 'project;' AND k = 'T'")} \
        if db._has_table(conn, "tag_counts") else None
    if weight is None:
        return [{"key": k, "title": (db.get_doc(conn, db.project_doc_id(conn, k)) or db.Doc(
                    "", None, k, "", "", "", "", "", "", [])).title, "open_tasks": open_task_count(conn, k)}
                for k in (keys[:limit] if limit else keys)]
    if limit is not None and len(keys) > limit:
        keys = sorted(keys, key=lambda k: (-weight.get(k, 0), k))[:limit]
    closed: dict[str, int] = {}
    if any(weight.get(k) for k in keys):
        for t, n in conn.execute("SELECT p.tag, count(*) FROM items i JOIN tags p ON p.id = i.id "
                                 "WHERE i.kind = 'T' AND i.status = 'closed' AND p.tag >= 'project:' "
                                 "AND p.tag < 'project;' GROUP BY p.tag"):
            closed[t[len(db.PROJECT_PREFIX):]] = int(n)
    titles = {}
    for k in keys:
        doc_id = db.project_doc_id(conn, k)
        row = conn.execute("SELECT title FROM docs WHERE id = ?", (doc_id,)).fetchone() if doc_id else None
        titles[k] = row[0] if row else k
    return [{"key": k, "title": titles[k], "open_tasks": max(0, weight.get(k, 0) - closed.get(k, 0))}
            for k in keys]


def repo_features(roots, key: str) -> list[dict]:
    """The open features of the repo wiki named *key* among *roots* (empty without one)."""
    from shapa import tasks

    out: list[dict] = []
    for root in roots:
        if tasks.wiki_label(root) != key or tasks.wiki_label(root) == "global":
            continue
        conn = db.connect(root)
        if conn is None:
            continue
        try:
            out += [{"id": f.id, "title": f.title} for f in db.items(conn, status="live", kind="F")]
        finally:
            conn.close()
    return out


# --- the CLI ------------------------------------------------------------------------------------

def _print_show(data: dict) -> None:
    print(f"# {data['key']} - {data['title']} ({data['document']['id']})")
    for name, items in data["document"]["sections"].items():
        if items:
            print(f"{name}:")
            for n, t in enumerate(items, 1):
                print(f"  {n}. {t}")
    for name in ("tasks", "ideas", "rules", "memories", "skills", "features"):
        rows = data.get(name) or []
        if rows:
            print(f"{name}:")
            for r in rows:
                extra = f" [{r['status']}]" if r.get("status") else ""
                print(f"  {r['id']}{extra} {r.get('title') or r.get('summary') or ''}")
    for name, n in data.get("more", {}).items():
        print(f"  ... {n} more {name}")


def run_project(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa project", description="A project's one document and "
                                "everything tied to it (the project:KEY tag).")
    p.add_argument("--cwd", default=None)
    sub = p.add_subparsers(dest="cmd")
    add = _subcommand_adder(sub)
    wiki_kw = dict(choices=["global", "repo"], default="global",
                   help="the wiki that holds the project (default global)")
    nw = add("new", help="create the project's document from the template (idempotent)")
    nw.add_argument("key")
    nw.add_argument("--title", default=None)
    nw.add_argument("--wiki", **wiki_kw)
    ad = add("add", help="append one numbered line to a section of the project's document")
    ad.add_argument("key")
    ad.add_argument("section", help=", ".join(SECTIONS))
    ad.add_argument("text")
    ad.add_argument("--wiki", **wiki_kw)
    sh = add("show", help="the document's sections and the tasks, ideas, rules, memories and "
                          "skills tied to the project")
    sh.add_argument("key")
    sh.add_argument("--wiki", **wiki_kw)
    sh.add_argument("--json", action="store_true")
    ls = add("list", help="the projects with their open tasks")
    ls.add_argument("--wiki", **wiki_kw)
    ls.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cmd = args.cmd or "list"
    conn = _connect(scoped_root(getattr(args, "wiki", "global"), args.cwd), create=cmd == "new")
    try:
        if cmd == "new":
            doc_id, created = new_project(conn, args.key, args.title)
            print(doc_id if created else f"{doc_id} (the project already has its document)")
        elif cmd == "add":
            r = add_item(conn, args.key, args.section, args.text)
            print(f"{r['id']} {r['section']}" + ("" if r["changed"] else " (already there)"))
        elif cmd == "show":
            from shapa import tasks

            data = show(conn, args.key, features=repo_features(tasks.scope_roots(args.cwd), _key(args.key)))
            print(json.dumps(data, indent=2)) if args.json else _print_show(data)
        else:
            rows = listing(conn)
            if args.json:
                print(json.dumps({"projects": rows}, indent=2))
            else:
                for r in rows:
                    print(f"{r['key']} - {r['title']} ({r['open_tasks']} open tasks)")
                if not rows:
                    print("no project here: `shapa project new KEY`")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    run(run_project, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
