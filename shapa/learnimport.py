"""``shapa learn import`` - the operator curates one wiki's principles, mistakes and memories from a file.

Privileged: only the local CLI reaches it (no MCP tool, no hook, no sync). The file is shaped::

    {"wikis":  {KEY: {"principles": [{"text", "class": "principle"|"mistake", "date", "sources": [...],
                                      "project"?}],
                      "memories":   [{"text", "date", "sources": [...]}]}},
     "delete": {KEY: [row ids]},
     "keep":   {KEY: [row ids]}}

KEY is ``global`` or a repo wiki's key (its checkout's directory name). One call is one transaction:

1. Validate everything first and write nothing on any error: every principle and mistake is one rule
   (``db.check_atomic_rule``; a memory states a fact and is not held to it), every text holds no personal data (the gate of :mod:`shapa.pii`), every date and id is well formed, no id is both
   deleted and kept, the principle count stays inside :data:`learn.MAX_PRINCIPLES`, and a ledger item or a
   document is never deleted. A deleted id the wiki does not hold is reported, not fatal.
2. Delete the listed rows (tombstoned as usual), write the principles as ``principle`` rule rows (the shape
   ``learn apply --new`` makes: one evidence line), the mistakes as issues that still carry a lesson (what the
   session start's X list reads: no ``correction``, ``learned``, ``soft`` or ``dismissed`` tag), the memories as
   memory rows - each stamped with its date, so the mempm shows it. The near-candidate check of ``apply`` is
   skipped (the operator curated the file); identical text is written once, and a second import of the same
   file changes nothing.
3. The global wiki only: principles, then memories, most recent first, are staged at ``stage:0`` for as long as
   the baseline budget (:data:`db.BASELINE_BUDGET`, never raised here) holds. Issues take no stage: a mistake
   reaches every device through the X list. When a principle does not fit, nothing after it is staged.

``dry_run`` runs all of it inside the transaction and rolls back, so its report is exactly what a real run
does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shapa import config, db, learn, metrics, registry, stages
from shapa.redact import redact

CLASSES = ("principle", "mistake")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SRC_CAP = 140                      # one evidence line, as long as `learn apply` keeps
#: A row kind per class of entry.
_KIND = {"principle": "R", "mistake": "I", "memory": "M"}
_DELETABLE = ("M", "R", "I")


class ImportRefused(db.LedgerError):
    """The file did not validate; nothing was written. ``problems`` lists every reason."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("the import is refused and nothing was written:\n  - " + "\n  - ".join(self.problems))


@dataclass
class Planned:
    cls: str                        # principle | mistake | memory
    summary: str
    body: str
    tags: list[str]
    date: str
    where: str
    existing: str | None = None     # id of the row that already holds this text
    stage: bool = False             # staged at stage:0 by this run
    existing_staged: bool = False   # the row that holds this text is already at stage:0
    size: int = 0                   # what it counts against the baseline budget


# --- reading the file -------------------------------------------------------------------------------

def load(src) -> dict:
    """The parsed file (a path, or an already parsed dict), its shape checked."""
    if isinstance(src, dict):
        data = src
    else:
        try:
            data = json.loads(Path(src).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise db.LedgerError(f"cannot read the import file: {type(exc).__name__}") from None
    if not isinstance(data, dict) or not isinstance(data.get("wikis"), dict):
        raise db.LedgerError("the import file needs a \"wikis\" mapping (and optional \"delete\" and \"keep\")")
    for k in ("delete", "keep"):
        if not isinstance(data.get(k, {}), dict):
            raise db.LedgerError(f"\"{k}\" is a mapping of wiki key to a list of row ids")
    return data


def resolve_wiki(key: str, scope: str | None, cwd=None) -> tuple[str, Path]:
    """``(scope label, wiki root)`` of the wiki named *key*: ``global``, or a repo wiki found from *cwd* or in
    the registry by its key."""
    want = "global" if key == "global" else "repo"
    if scope not in (None, want):
        raise db.LedgerError(f"--scope {scope} does not match wiki {key!r}: use --scope {want}")
    if want == "global":
        root = config.global_root()
        if not db.exists(root):
            raise db.LedgerError("the global wiki has no database")
        return "global", root
    for s in learn.scopes(cwd):
        if s.label == "repo" and s.key == key:
            return "repo", s.root
    found = []
    for p in registry.load():
        try:
            if db.exists(p) and metrics.repo_key(p) == key and Path(p).resolve() != config.global_root().resolve():
                found.append(Path(p))
        except OSError:
            continue
    if len(found) == 1:
        return "repo", found[0]
    if found:
        raise db.LedgerError(f"several wikis are named {key!r}: pass --cwd DIR of the one you mean")
    raise db.LedgerError(f"no wiki {key!r} with a database: pass --cwd DIR of its checkout")


# --- validating -------------------------------------------------------------------------------------

def _sources(raw, where: str, problems: list[str]) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        problems.append(f"{where}: sources is a list of strings")
        return []
    return [re.sub(r"[^\w./:-]", "", s)[:60] for s in raw if re.sub(r"[^\w./:-]", "", s)]


def _plan_entry(raw, cls: str, label: str, where: str, problems: list[str]) -> Planned | None:
    n0 = len(problems)
    if not isinstance(raw, dict):
        problems.append(f"{where}: an entry is an object")
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        problems.append(f"{where}: text is required")
        return None
    date = raw.get("date")
    if not isinstance(date, str) or not _DATE.match(date):
        problems.append(f"{where}: date is YYYY-MM-DD")
    else:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            problems.append(f"{where}: date {date!r} is not a calendar day")
    srcs = _sources(raw.get("sources", []), where, problems)
    project = ""
    if raw.get("project"):
        try:
            project = db.clean_project(str(raw["project"]))
            db.check_project_tags([db.PROJECT_PREFIX + project])
        except db.LedgerError as exc:
            problems.append(f"{where}: {exc}")
    if cls != "memory":   # a memory states a fact, which may take several sentences; a rule or lesson is ONE
        try:
            db.check_atomic_rule(text, what="a principle" if cls == "principle" else "a mistake")
        except db.LedgerError as exc:
            problems.append(f"{where}: {exc}")
    summary = ""
    if cls == "principle":
        try:
            summary = learn.clean_principle(text)
        except db.LedgerError as exc:
            problems.append(f"{where}: {exc}")
    else:
        summary = db.one_line(redact(text))
        if len(" ".join(text.split())) > db.SUMMARY_MAX:
            problems.append(f"{where}: at most {db.SUMMARY_MAX} characters")
    if len(problems) > n0:
        return None
    ev = "sources " + (", ".join(srcs) if srcs else "none")
    if cls == "principle":
        body = learn._join_body([], [f"- imported {date}: {ev}"[:_SRC_CAP]])
        tags = ["principle", "serves:none", "evidence:1", f"last-seen:{date}T12:00:00Z"]
    elif cls == "mistake":
        body, tags = f"Imported {date}. {ev[:_SRC_CAP]}.", ["mistake"]
    else:
        body, tags = f"Sources: {', '.join(srcs) if srcs else 'none'}"[:_SRC_CAP], []
    if project:
        tags.append(db.PROJECT_PREFIX + project)
    return Planned(cls, summary, body, tags, str(date), where)


def _screen(p: Planned, label: str, problems: list[str]) -> None:
    """The personal-data gate at the level the write will use: strict for anything that may reach stage 0
    (every entry of the global wiki), the operator's for a repo wiki."""
    tags = ["stage:0"] if label == "global" else []
    try:
        db._pii_gate([p.summary, p.body], tags + p.tags, True)
    except db.PiiRejected as exc:
        problems.append(f"{p.where}: personal or sensitive data ({', '.join(exc.categories)})")


def _ids(raw, name: str, key: str, problems: list[str]) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append(f"{name}[{key}] is a list of row ids")
        return []
    out = []
    for i in raw:
        if not isinstance(i, str) or not db.ID_RE.match(i):
            problems.append(f"{name}[{key}]: {str(i)[:30]!r} is not a record id")
        elif i not in out:
            out.append(i)
    return out


# --- the run ----------------------------------------------------------------------------------------

def _stamp(date: str) -> str:
    return f"{date}T12:00:00Z"


def import_curated(src, key: str, *, cwd=None, scope: str | None = None, dry_run: bool = False,
                   now: datetime | None = None) -> dict:
    """Curate wiki *key* from *src* (a path or a parsed file). Returns the report (see the module docstring);
    raises :class:`ImportRefused` with every problem, writing nothing, when the file does not validate."""
    now = now or datetime.now(timezone.utc)
    data = load(src)
    label, root = resolve_wiki(key, scope, cwd)
    problems: list[str] = []
    block = data["wikis"].get(key) or {}
    if not isinstance(block, dict):
        raise db.LedgerError(f"wikis[{key}] is an object with principles and memories")
    delete = _ids(data.get("delete", {}).get(key), "delete", key, problems)
    keep = _ids(data.get("keep", {}).get(key), "keep", key, problems)
    both = [i for i in delete if i in keep]
    if both:
        problems.append(f"{', '.join(both)} is in both delete and keep")

    entries: list[Planned] = []
    seen: set[tuple[str, str]] = set()
    dupes = 0
    raw_p, raw_m = block.get("principles") or [], block.get("memories") or []
    if not isinstance(raw_p, list) or not isinstance(raw_m, list):
        raise db.LedgerError("principles and memories are lists")
    if len(raw_p) > learn.MAX_PRINCIPLES:
        problems.append(f"{len(raw_p)} principles and mistakes: a wiki keeps at most {learn.MAX_PRINCIPLES}")
    for i, raw in enumerate(raw_p):
        cls = raw.get("class") if isinstance(raw, dict) else None
        if cls not in CLASSES:
            problems.append(f"principles[{i}]: class is principle or mistake")
            continue
        p = _plan_entry(raw, cls, label, f"principles[{i}]", problems)
        if p is not None:
            _screen(p, label, problems)
            entries.append(p)
    for i, raw in enumerate(raw_m):
        p = _plan_entry(raw, "memory", label, f"memories[{i}]", problems)
        if p is not None:
            _screen(p, label, problems)
            entries.append(p)

    conn = db.connect(root)
    if conn is None:
        raise db.LedgerError(f"{root} has no database")
    try:
        present, missing, kept_missing = [], [], []
        for rid in delete:
            if not db._stored(conn, rid):
                missing.append(rid)
            elif rid[0] in _DELETABLE:
                present.append(rid)
            else:
                what = "a document" if rid[0] == db.DOC_KIND else "a ledger item"
                problems.append(f"{rid} is {what}: it is never deleted by an import")
        for rid in keep:
            if not db._stored(conn, rid):
                kept_missing.append(rid)
        gone = set(present)
        live = [r for r in learn.live_principles(conn) if r not in gone]
        have = {}
        for kind in ("R", "I", "M"):
            for rid, summary in conn.execute("SELECT id, summary FROM mri WHERE kind = ?", (kind,)):
                if rid not in gone:
                    have[(kind, summary)] = rid
        fresh: list[Planned] = []
        for p in entries:
            k = (_KIND[p.cls], p.summary)
            if k in seen:
                dupes += 1
                continue
            seen.add(k)
            p.existing = have.get(k)
            fresh.append(p)
        if sum(1 for p in fresh if p.cls == "principle" and p.existing is None) + len(live) > learn.MAX_PRINCIPLES:
            problems.append(f"the wiki would hold {len(live)} principles beside the new ones: at most "
                            f"{learn.MAX_PRINCIPLES} (delete or merge some)")
        if problems:
            raise ImportRefused(problems)

        report = _run(conn, root, label, fresh, present, now, dry_run, learn.Scope(label, root, key))
        report.update(wiki=key, scope=label, dry_run=bool(dry_run), missing=missing, kept=len(keep) - len(kept_missing),
                      keep_missing=kept_missing, skipped=dupes + report.pop("skipped_existing"))
        return report
    finally:
        conn.close()


def _run(conn, root, label: str, fresh: list[Planned], present: list[str], now: datetime, dry_run: bool,
         target: learn.Scope) -> dict:
    budget = db.BASELINE_BUDGET
    written = {"principle": 0, "mistake": 0, "memory": 0}
    staged = {"principle": 0, "mistake": 0, "memory": 0}
    not_staged = {"principle": 0, "memory": 0}
    skipped_existing = 0
    overflow = None
    new_principles: list[str] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        removed = db.remove(conn, present)
        if label == "global":
            used = stages.baseline_size(conn)
            for p in fresh:
                if p.existing:
                    tags = db._tags_of(conn, p.existing)
                    p.existing_staged = f"{db.STAGE_PREFIX}0" in tags
                    p.size = 0 if p.existing_staged else stages.record_size(conn, p.existing)
                else:
                    p.size = len(db.one_line(redact(p.summary)))
            order = sorted((p for p in fresh if p.cls == "principle"), key=lambda p: p.date, reverse=True)
            order += sorted((p for p in fresh if p.cls == "memory"), key=lambda p: p.date, reverse=True)
            free = budget - used
            cut = False
            for p in order:
                if cut:
                    break
                if p.existing_staged:
                    continue
                if used + p.size > budget:
                    cut = True
                    break
                p.stage = True
                used += p.size
            if cut:
                need = sum(p.size for p in order if p.cls == "principle" and not p.existing_staged)
                if any(p.cls == "principle" and not p.stage and not p.existing_staged for p in order):
                    overflow = {"needed": need, "available": free, "over": need - free, "budget": budget}
        for p in fresh:
            kind = _KIND[p.cls]
            if p.existing is not None:
                skipped_existing += 1
                if p.stage and label == "global":
                    stages.set_stage(conn, p.existing, 0)
                continue
            tags = list(p.tags) + ([f"{db.STAGE_PREFIX}0"] if p.stage else [])
            rid, created = db.add_row(conn, kind, p.summary, p.body, tags=tags, source="import", privileged=True,
                                      now=now)
            if not created:
                skipped_existing += 1
                continue
            stamp = _stamp(p.date)
            conn.execute("UPDATE mri SET created = ?, updated = ? WHERE id = ?", (stamp, stamp, rid))
            written[p.cls] += 1
            if p.cls == "principle":
                new_principles.append(rid)
        if label == "global":
            for p in fresh:
                if p.cls == "mistake":
                    continue
                if p.stage or p.existing_staged:
                    staged[p.cls] += 1
                elif p.cls in not_staged:
                    not_staged[p.cls] += 1
        baseline = {"used": stages.baseline_size(conn), "budget": budget} if label == "global" else \
            {"used": 0, "budget": budget}
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if dry_run else "COMMIT")
    if not dry_run:
        _refresh_index(target, removed_ids=present, principle_ids=new_principles, now=now)
    written["total"] = sum(written.values())
    staged["total"] = sum(staged.values())
    return {"deleted": removed, "written": written, "staged": staged, "not_staged": not_staged,
            "baseline": baseline, "overflow": overflow, "skipped_existing": skipped_existing}


def summary_line(res: dict) -> str:
    """The report as the lines the CLI prints."""
    w, s = res["written"], res["staged"]
    lines = [f"{'dry run: ' if res['dry_run'] else ''}{res['wiki']} [{res['scope']}]: deleted {res['deleted']}, "
             f"written {w['total']} ({w['principle']} principles, {w['mistake']} mistakes, {w['memory']} memories), "
             f"staged {s['total']} at stage 0, skipped {res['skipped']} already stored"]
    if res["missing"]:
        lines.append(f"not found (reported, not fatal): {', '.join(res['missing'])}")
    if res["scope"] == "global":
        lines.append(f"baseline {res['baseline']['used']} of {res['baseline']['budget']} characters; "
                     f"not staged: {res['not_staged']['principle']} principles, {res['not_staged']['memory']} memories")
    ov = res.get("overflow")
    if ov:
        lines.append(f"the principles need {ov['needed']} characters of stage 0 and {ov['available']} are free: "
                     f"{ov['over']} over the {ov['budget']}-character budget (nothing raised)")
    return "\n".join(lines)


def _refresh_index(target: learn.Scope, *, removed_ids, principle_ids, now: datetime) -> None:
    """The derived similarity index follows the rows: deleted ones leave it, new principles enter and are
    remembered as written on this device. Never fails the import (the index catches up from the rows)."""
    try:
        with learn.LearnIndex(target.root, timeout=5.0) as ix:
            for rid in removed_ids:
                ix.drop(rid)
                ix.conn.execute("DELETE FROM p_local WHERE id = ?", (rid,))
            ix.conn.commit()
    except Exception:
        pass
    for rid in principle_ids:
        learn._post_write(target, rid, now)
