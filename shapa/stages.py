"""``shapa stage`` - which rules, memories and skills every session starts with.

A stage is the tag ``stage:N`` (one digit, 0-9) on a rule, a memory or a skill version.
``stage:0`` is the *baseline*: served in full at every session start, to every client. Higher
stages load on demand through search, and a lower stage ranks first on a tie. A record with no
stage is on demand too.

``stage:0`` is the operator's, like skill activation: only the local CLI sets it (this module's
commands, ``shapa row add|tag``, ``shapa save``). From anything else (the MCP tools, a hook, a
pulled record) it is stored as ``stage-proposed:0``, which no session start ever serves, and the
operator lists those with ``shapa stage pending`` and accepts one with ``shapa stage set ID 0``.
The baseline of a wiki is capped at :data:`db.BASELINE_BUDGET` characters (a rule or memory counts
its summary and body, a skill its description), so a session start cannot be flooded.

CLI::

    shapa stage set ID N [--scope global|repo]     # N 0-9; ID a row, or skill:NAME
    shapa stage clear ID [--scope ...]
    shapa stage list [--scope ...] [--json]        # stage, id, size, and the baseline used
    shapa stage pending [--scope ...] [--json]     # the stage-proposed:0 records
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from shapa import db
from shapa.ledger import CommandError, _connect, _subcommand_adder, run, scoped_root


def _resolve(conn: sqlite3.Connection, ident: str) -> str:
    """The id behind *ident*: a row or document id or alias, or ``skill:NAME`` (its active
    version, else its newest)."""
    ident = (ident or "").strip()
    row = db.get_row(conn, ident)
    if row is not None:
        return row.id
    doc = db.get_doc(conn, ident)
    if doc is not None:
        return doc.id
    if ident.startswith("skill:") and "." not in ident:
        from shapa import skills

        vs = skills.versions(conn, ident[len("skill:"):])
        if vs:
            return next((v for v in reversed(vs) if v.status == "active"), vs[-1]).doc_id
    raise db.LedgerError(f"no rule, memory or skill version {ident!r}")


def description_size(body: str) -> int:
    """What a skill version's ``SKILL.md`` *body* counts against the baseline: its description."""
    from shapa import skills

    fields, _ = skills._split(body or "")
    return len(skills._description(fields.get("description", "")))


def _skill_size(conn: sqlite3.Connection, doc_id: str) -> int:
    from shapa import skills

    d = db.get_doc(conn, doc_id)
    v = skills._from_doc(d) if d is not None else None
    return len(v.description) if v is not None else 0


def record_size(conn: sqlite3.Connection, record_id: str) -> int:
    """What a record counts against the baseline budget."""
    if record_id[:1] == db.DOC_KIND:
        return _skill_size(conn, record_id)
    r = conn.execute("SELECT length(summary) + length(body) FROM mri WHERE id = ?", (record_id,)).fetchone()
    return int(r[0]) if r else 0


def baseline_ids(conn: sqlite3.Connection, *, threshold: int = 0) -> list[str]:
    """Ids served at session start for stages 0..*threshold*: rules, memories and the ACTIVE
    version of a skill, lowest stage first (then rules before memories, then by number)."""
    wanted = [f"{db.STAGE_PREFIX}{n}" for n in range(0, max(0, min(db.STAGE_MAX, threshold)) + 1)]
    found = conn.execute(f"SELECT tag, id FROM tags WHERE tag IN ({','.join('?' * len(wanted))})",
                         wanted).fetchall()
    out = []
    for tag, rid in found:
        letter = rid[:1]
        if letter in ("R", "M"):
            out.append((int(tag[-1]), 0 if letter == "R" else 1, int(rid[1:] or 0), rid))
        elif letter == db.DOC_KIND:
            tags = db._tags_of(conn, rid)
            if db.SKILL_TAG in tags and "status:active" in tags:
                out.append((int(tag[-1]), 2, int(rid[1:] or 0), rid))
    return [rid for *_, rid in sorted(out)]


def baseline_size(conn: sqlite3.Connection, *, exclude: str | None = None) -> int:
    return sum(record_size(conn, i) for i in baseline_ids(conn) if i != exclude)


def check_budget(conn: sqlite3.Connection, record_id: str | None, size: int | None) -> None:
    """Refuse a stage-0 record that would take the baseline past the cap. *size* is the record's
    (new) size, or ``None`` to measure the stored one; the record's own current size is not
    counted twice."""
    if record_id and record_id[:1] == db.DOC_KIND and "status:active" not in db._tags_of(conn, record_id):
        return  # a skill version that is not in use is not served: activation checks the cap
    own = size if size is not None else (record_size(conn, record_id) if record_id else 0)
    now = baseline_size(conn)
    total = baseline_size(conn, exclude=record_id) + own
    if total > db.BASELINE_BUDGET:
        raise db.LedgerError(f"the baseline holds {now} of {db.BASELINE_BUDGET} characters; this record "
                             f"would make it {total}. Shorten it, or move another record off stage 0 "
                             "(`shapa stage list`)")


def set_stage(conn: sqlite3.Connection, ident: str, stage: int) -> str:
    """Give a record stage *stage* (replacing its stage, or its proposed one). Local CLI only."""
    try:
        n = int(stage)
    except (TypeError, ValueError):
        raise db.LedgerError(f"a stage is a digit 0-{db.STAGE_MAX} (got {stage!r})") from None
    if not 0 <= n <= db.STAGE_MAX or str(stage).strip() not in (str(n), f"+{n}"):
        raise db.LedgerError(f"a stage is a digit 0-{db.STAGE_MAX} (got {stage!r})")
    rid = _resolve(conn, ident)
    if not db.takes_stage(rid[:1], db._tags_of(conn, rid)):
        raise db.LedgerError("a stage applies to rules, memories and skill versions only")
    db.retag(conn, rid, add=[f"{db.STAGE_PREFIX}{n}"], drop_prefix=db.STAGE_PREFIX,
             drop=[db.STAGE_PROPOSED], privileged=True)
    return rid


def clear_stage(conn: sqlite3.Connection, ident: str) -> str:
    rid = _resolve(conn, ident)
    db.retag(conn, rid, drop_prefix=db.STAGE_PREFIX, drop=[db.STAGE_PROPOSED], privileged=True)
    return rid


def _summary(conn: sqlite3.Connection, rid: str) -> str:
    if rid[:1] == db.DOC_KIND:
        d = db.get_doc(conn, rid)
        return d.title if d else ""
    r = conn.execute("SELECT summary FROM mri WHERE id = ?", (rid,)).fetchone()
    return r[0] if r else ""


def listing(conn: sqlite3.Connection) -> dict:
    """Every staged record, lowest stage first, and the baseline used."""
    found = conn.execute("SELECT tag, id FROM tags WHERE tag IN (%s)" % ",".join(
        "?" * (db.STAGE_MAX + 1)), [f"{db.STAGE_PREFIX}{n}" for n in range(db.STAGE_MAX + 1)]).fetchall()
    records = sorted(({"stage": int(t[-1]), "id": i, "size": record_size(conn, i), "summary": _summary(conn, i)}
                      for t, i in found),
                     key=lambda r: (r["stage"], "RMD".find(r["id"][:1]), int(r["id"][1:] or 0)))
    return {"records": records, "baseline_used": baseline_size(conn), "baseline_budget": db.BASELINE_BUDGET}


def pending(conn: sqlite3.Connection) -> list[dict]:
    ids = [r[0] for r in conn.execute("SELECT id FROM tags WHERE tag = ?", (db.STAGE_PROPOSED,))]
    return sorted(({"id": i, "size": record_size(conn, i), "summary": _summary(conn, i)} for i in ids),
                  key=lambda r: (r["id"][:1], int(r["id"][1:] or 0)))


def run_stage(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa stage", description="Which rules, memories and skills "
                                "every session starts with (stage 0 = the baseline).")
    p.add_argument("--cwd", default=None)
    sub = p.add_subparsers(dest="cmd")
    add = _subcommand_adder(sub)
    scope_kw = dict(choices=["repo", "global"], default="global")
    st = add("set", help="give a record a stage 0-9 (0 = baseline, served in full every session)")
    st.add_argument("id")
    st.add_argument("stage")
    st.add_argument("--scope", **scope_kw)
    cl = add("clear", help="take a record off every stage (on demand again)")
    cl.add_argument("id")
    cl.add_argument("--scope", **scope_kw)
    ls = add("list", help="staged records, lowest first, and the baseline budget used")
    ls.add_argument("--scope", **scope_kw)
    ls.add_argument("--json", action="store_true")
    pe = add("pending", help="records proposed for the baseline (stage-proposed:0), "
                             "accepted with `shapa stage set ID 0`")
    pe.add_argument("--scope", **scope_kw)
    pe.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cmd = args.cmd or "list"
    conn = _connect(scoped_root(getattr(args, "scope", "global"), args.cwd))
    try:
        if cmd == "set":
            print(f"{set_stage(conn, args.id, args.stage)} stage {int(args.stage)}")
        elif cmd == "clear":
            print(f"{clear_stage(conn, args.id)} cleared")
        elif cmd == "pending":
            found = pending(conn)
            if args.json:
                print(json.dumps({"pending": found}, indent=2))
            else:
                for r in found:
                    print(f"{r['id']} ({r['size']} chars) {r['summary']}")
                if not found:
                    print("no record is proposed for the baseline")
        else:
            data = listing(conn)
            if getattr(args, "json", False):
                print(json.dumps(data, indent=2))
            else:
                for r in data["records"]:
                    print(f"stage {r['stage']} {r['id']} ({r['size']} chars) {r['summary']}")
                print(f"baseline: {data['baseline_used']} of {data['baseline_budget']} characters")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    run(run_stage, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
