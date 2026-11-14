"""``shapa focus`` - the northstar and the three big fires of a wiki scope.

A wiki (the global one, or a repo's) holds one *focus record*: a document
tagged ``focus`` with the alias ``focus`` whose JSON body is::

    {"v": 1, "northstar": "...", "fires": [{"feature": "F22", "text": ""}, null, null]}

``fires`` always has three slots, ranked first to last; a slot is empty
(``null``), points at a ledger feature (``feature``), or is free text for a
fire that spans repos (``text``). Nothing here is a new record kind or a
schema change: the record syncs like any document, and a hosted store reads and
sets it the same way (``hosted.md``).

A feature's rank is a ``rank:N`` tag on the feature (lower first). When a fire's
feature closes, its slot is freed, the other fires move up, and the highest-ranked
open top-level feature that is not already a fire takes the last slot: an
explicit rank first, else the oldest. The operator overrides with
``shapa focus promote``.

CLI::

    shapa focus [show] [--scope global|repo] [--json]
    shapa focus northstar TEXT --scope global|repo
    shapa focus fire SLOT (FEATURE | --text TEXT) --scope global|repo
    shapa focus unfire SLOT --scope ...        # later fires move up
    shapa focus order N N N --scope ...        # e.g. 2 1 3
    shapa focus rank FEATURE N|none --scope repo
    shapa focus candidates [--scope repo]
    shapa focus promote [FEATURE] [--slot N] --scope repo
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from shapa import db
from shapa.ledger import CommandError, _connect, _roots, _subcommand_adder, run, scoped_root
from shapa.redact import redact

ALIAS = "focus"
MAX_FIRES = 3
NORTHSTAR_MAX = 600
FIRE_TEXT_MAX = 200
RANK_PREFIX = "rank:"
FILES_SHOWN = 8
_FILE_RE = re.compile(
    r"(?<![\w/.:-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*\.[A-Za-z0-9]{1,6}"
    r"|[A-Za-z0-9_-]+\.(?:py|ts|tsx|js|md|json|jsonc|toml|sh|yml|yaml|sql))(?![\w/-])")
_BOX_RE = re.compile(r"\[\[([A-Za-z0-9][A-Za-z0-9._-]{0,60})\]\]")


@dataclass
class Fire:
    feature: str = ""
    text: str = ""


@dataclass
class Focus:
    northstar: str = ""
    fires: list = field(default_factory=lambda: [None] * MAX_FIRES)


@dataclass
class TreeJob:
    id: str
    title: str
    status: str
    tasks: list


@dataclass
class TreeTask:
    id: str
    title: str
    status: str


@dataclass
class FireTree:
    slot: int
    feature: str
    title: str
    status: str
    jobs: list
    files: list
    boxes: list


def _line(text, limit: int) -> str:
    """One redacted line of at most *limit* characters: a record can never add a
    line or a heading of its own to the text a session starts with."""
    return db.one_line(redact(str(text or "")), limit)


# --- the record -------------------------------------------------------------------------------

def _parse(body: str) -> Focus:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return Focus()
    if not isinstance(data, dict):
        return Focus()
    north = data.get("northstar")
    out = Focus(northstar=_line(north, NORTHSTAR_MAX) if isinstance(north, str) else "")
    raw = data.get("fires")
    for i, item in enumerate((raw if isinstance(raw, list) else [])[:MAX_FIRES]):
        if not isinstance(item, dict):
            continue
        feature, text = item.get("feature"), item.get("text")
        fire = Fire(feature=feature if isinstance(feature, str) and db.ID_RE.match(feature)
                    and feature[0] == "F" else "",
                    text=_line(text, FIRE_TEXT_MAX) if isinstance(text, str) else "")
        if fire.feature or fire.text:
            out.fires[i] = fire
    return out


def _dump(f: Focus) -> str:
    return json.dumps({"v": 1, "northstar": f.northstar,
                       "fires": [None if x is None else {"feature": x.feature, "text": x.text}
                                 for x in f.fires]}, sort_keys=True, separators=(",", ":"))


def load(conn: sqlite3.Connection) -> Focus:
    """The wiki's focus (empty when never set). Never raises, never writes."""
    d = db.get_doc(conn, ALIAS)
    if d is None or db.FOCUS_TAG not in d.tags:
        return Focus()
    return _parse(d.body)


def save(conn: sqlite3.Connection, f: Focus) -> Focus:
    f.fires = (list(f.fires) + [None] * MAX_FIRES)[:MAX_FIRES]
    body = _dump(f)
    d = db.get_doc(conn, ALIAS)
    if d is None:
        db.add_doc(conn, "Focus", body, tags=[db.FOCUS_TAG], alias=ALIAS, system=True,
                   mime="application/json", source="focus")
    elif db.FOCUS_TAG in d.tags:
        if d.body != body:
            db.update_doc(conn, d.id, body=body)
    else:
        raise db.LedgerError(f"{d.id} already uses the alias {ALIAS!r} and is not the focus record")
    return f


def set_northstar(conn, text: str) -> Focus:
    text = _line(text, NORTHSTAR_MAX)
    if not text:
        raise db.LedgerError("the northstar needs text")
    f = load(conn)
    f.northstar = text
    return save(conn, f)


# --- features and candidates ---------------------------------------------------------------------

def _feature(conn, feature_id: str) -> db.Item:
    item = db.get_item(conn, feature_id)
    if item is None or item.kind != "F":
        raise db.LedgerError(f"{feature_id!r} is not a feature in this wiki")
    if item.status == "closed":
        raise db.LedgerError(f"{item.id} is closed")
    return item


def rank_of(conn, feature_id: str) -> int | None:
    for (t,) in conn.execute("SELECT tag FROM tags WHERE id = ? AND tag LIKE ?",
                             (feature_id, RANK_PREFIX + "%")):
        try:
            return int(t[len(RANK_PREFIX):])
        except ValueError:
            continue
    return None


def rank_feature(conn, feature_id: str, rank: int | None) -> None:
    """Set (or with ``None`` clear) a feature's explicit rank, 1 = first."""
    item = _feature(conn, feature_id)
    if rank is not None and not 1 <= int(rank) <= 999:
        raise db.LedgerError("a rank is a number from 1 to 999")
    db.retag(conn, item.id, add=[] if rank is None else [f"{RANK_PREFIX}{int(rank)}"],
             drop_prefix=RANK_PREFIX)


def candidates(conn) -> list[db.Item]:
    """Open top-level features that are not a fire, best first: an explicit rank
    (lowest first), then the oldest."""
    taken = {x.feature for x in load(conn).fires if x and x.feature}
    live = [i for i in db.items(conn, status="live", kind="F") if i.id not in taken]

    def key(i: db.Item):
        r = rank_of(conn, i.id)
        return (r is None, r or 0, i.created, int(i.id[1:]))

    return sorted(live, key=key)


# --- the fires --------------------------------------------------------------------------------------

def _slot(slot) -> int:
    try:
        n = int(slot)
    except (TypeError, ValueError):
        n = 0
    if not 1 <= n <= MAX_FIRES:
        raise db.LedgerError(f"there are three fires: slot 1, 2 or 3 (got {slot!r})")
    return n


def set_fire(conn, slot, feature: str | None = None, text: str | None = None) -> Focus:
    n = _slot(slot)
    f = load(conn)
    if feature:
        item = _feature(conn, feature)
        if any(x and x.feature == item.id for i, x in enumerate(f.fires) if i != n - 1):
            raise db.LedgerError(f"{item.id} is already a fire")
        f.fires[n - 1] = Fire(feature=item.id, text="")
    elif text and _line(text, FIRE_TEXT_MAX):
        f.fires[n - 1] = Fire(text=_line(text, FIRE_TEXT_MAX))
    else:
        raise db.LedgerError("a fire needs a feature or text")
    return save(conn, f)


def clear_fire(conn, slot) -> Focus:
    n = _slot(slot)
    f = load(conn)
    rest = [x for i, x in enumerate(f.fires) if i != n - 1 and x is not None]
    f.fires = rest + [None] * (MAX_FIRES - len(rest))
    return save(conn, f)


def reorder(conn, order) -> Focus:
    """Rank the fires anew: ``[2, 1, 3]`` puts the current second fire first."""
    order = [_slot(x) for x in order]
    if sorted(order) != list(range(1, MAX_FIRES + 1)):
        raise db.LedgerError("order lists each of the three slots once, e.g. 2 1 3")
    f = load(conn)
    f.fires = [f.fires[i - 1] for i in order]
    return save(conn, f)


def promote(conn, feature: str | None = None, slot=None) -> Focus:
    """Put a feature in a fire slot: the operator's override. With no *feature*,
    the next candidate; with no *slot*, the first free one (an occupied *slot*
    is replaced on purpose)."""
    f = load(conn)
    if slot is not None:
        n = _slot(slot)
    else:
        free = [i for i, x in enumerate(f.fires) if x is None]
        if not free:
            raise db.LedgerError("no free fire slot: pass --slot N to replace one, or unfire one")
        n = free[0] + 1
    if feature:
        item = _feature(conn, feature)
    else:
        found = candidates(conn)
        if not found:
            raise db.LedgerError("no open feature is waiting to be promoted")
        item = found[0]
    if any(x and x.feature == item.id for i, x in enumerate(f.fires) if i != n - 1):
        raise db.LedgerError(f"{item.id} is already a fire")
    f.fires[n - 1] = Fire(feature=item.id)
    return save(conn, f)


def _free_closed(conn, f: Focus, gone) -> int:
    """Drop the fires whose feature is in *gone* (the others move up); returns how many."""
    kept = [x for x in f.fires if x is not None and not (x.feature and gone(x.feature))]
    freed = sum(1 for x in f.fires if x is not None) - len(kept)
    f.fires = kept + [None] * (MAX_FIRES - len(kept))
    return freed


def _promote_next(conn, f: Focus, freed: int) -> None:
    for _ in range(freed):
        found = candidates_for(conn, f)
        free = [i for i, x in enumerate(f.fires) if x is None]
        if not found or not free:
            return
        f.fires[free[0]] = Fire(feature=found[0].id)


def candidates_for(conn, f: Focus) -> list[db.Item]:
    taken = {x.feature for x in f.fires if x and x.feature}
    return [i for i in candidates(conn) if i.id not in taken]


def on_feature_closed(conn, feature_id: str) -> None:
    """The close hook: free the closed feature's slot and promote the next candidate."""
    f = load(conn)
    if not any(x and x.feature == feature_id for x in f.fires):
        return
    freed = _free_closed(conn, f, lambda fid: fid == feature_id)
    _promote_next(conn, f, freed)
    save(conn, f)


def heal(conn) -> Focus:
    """Free every fire whose feature is closed or gone (closed by an older shapa, or
    arrived through a sync) and promote in their place."""
    f = load(conn)

    def gone(fid: str) -> bool:
        item = db.get_item(conn, fid)
        return item is None or item.status == "closed"

    freed = _free_closed(conn, f, gone)
    if freed:
        _promote_next(conn, f, freed)
        save(conn, f)
    return f


# --- the tree under a fire ---------------------------------------------------------------------------

def _mentions(*texts: str) -> tuple[list[str], list[str]]:
    files: list[str] = []
    boxes: list[str] = []
    for text in texts:
        for m in _FILE_RE.findall(text or ""):
            if m not in files:
                files.append(m)
        for m in _BOX_RE.findall(text or ""):
            if m not in boxes:
                boxes.append(m)
    return files[:FILES_SHOWN], boxes[:FILES_SHOWN]


def tree(conn, fire: Fire, slot: int = 0) -> FireTree:
    """A fire's feature, its open jobs and tasks, and the files and arch boxes
    their text names. A free-text fire has only its text."""
    if not fire.feature:
        return FireTree(slot, "", fire.text, "", [], [], [])
    item = db.get_item(conn, fire.feature)
    if item is None:
        return FireTree(slot, fire.feature, fire.text or fire.feature, "missing", [], [], [])
    texts = [item.title, item.body]
    jobs = []
    for job in db.items(conn, parent=item.id):
        if job.status == "closed":
            continue
        tasks = [TreeTask(t.id, t.title, t.status) for t in db.items(conn, parent=job.id)
                 if t.status != "closed"]
        texts += [job.title, job.body] + [t.title for t in db.items(conn, parent=job.id)
                                          if t.status != "closed"]
        for t in db.items(conn, parent=job.id):
            if t.status != "closed":
                texts.append(t.body)
        jobs.append(TreeJob(job.id, job.title, job.status, tasks))
    files, boxes = _mentions(*texts)
    return FireTree(slot, item.id, item.title, item.status, jobs, files, boxes)


# --- the CLI -------------------------------------------------------------------------------------------

def _fire_json(conn, i: int, fire) -> dict | None:
    if fire is None:
        return None
    t = tree(conn, fire, i + 1)
    return {"slot": i + 1, "feature": fire.feature, "text": fire.text, "title": t.title,
            "status": t.status}


def as_dict(conn, f: Focus | None = None) -> dict:
    f = f or load(conn)
    return {"northstar": f.northstar, "fires": [_fire_json(conn, i, x) for i, x in enumerate(f.fires)]}


def _print(conn, label: str, f: Focus) -> None:
    print(f"# {label}")
    print(f"northstar: {f.northstar or '(not set)'}")
    for i, fire in enumerate(f.fires, 1):
        if fire is None:
            print(f"fire {i}: (free)")
            continue
        t = tree(conn, fire, i)
        what = f"{fire.feature} {t.title}" if fire.feature else t.title
        print(f"fire {i}: {what}" + (f" [{t.status}]" if t.status and t.status != "open" else ""))
        for j in t.jobs:
            print(f"  {j.id} {j.title}" + (f" ({len(j.tasks)} tasks)" if j.tasks else ""))


def _scopes(cwd, scope):
    """``(label, root)`` of the wikis a command reads."""
    if scope:
        return [(scope, scoped_root(scope, cwd))]
    out = []
    for wr in _roots(cwd):
        if db.exists(wr.path) and wr.kind in ("global", "repo"):
            out.append((wr.kind, Path(wr.path)))
    return sorted(out, key=lambda x: x[0] != "global")  # the global northstar leads


def run_focus(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa focus", description="The northstar and the three "
                                "big fires of a wiki scope.")
    p.add_argument("--cwd", default=None)
    sub = p.add_subparsers(dest="cmd")
    add = _subcommand_adder(sub)
    scope_kw = dict(choices=["repo", "global"])
    sh = add("show")
    sh.add_argument("--scope", default=None, **scope_kw)
    sh.add_argument("--json", action="store_true")
    ns = add("northstar")
    ns.add_argument("text")
    ns.add_argument("--scope", required=True, **scope_kw)
    fi = add("fire")
    fi.add_argument("slot")
    fi.add_argument("feature", nargs="?")
    fi.add_argument("--text")
    fi.add_argument("--scope", required=True, **scope_kw)
    un = add("unfire")
    un.add_argument("slot")
    un.add_argument("--scope", required=True, **scope_kw)
    od = add("order")
    od.add_argument("slots", nargs=MAX_FIRES)
    od.add_argument("--scope", required=True, **scope_kw)
    rk = add("rank")
    rk.add_argument("feature")
    rk.add_argument("rank", help="1 first ... 999, or none")
    rk.add_argument("--scope", default="repo", choices=["repo"])
    ca = add("candidates")
    ca.add_argument("--scope", default="repo", choices=["repo"])
    pr = add("promote")
    pr.add_argument("feature", nargs="?")
    pr.add_argument("--slot")
    pr.add_argument("--scope", default="repo", choices=["repo"])
    args = p.parse_args(argv)
    cmd = args.cmd or "show"

    if cmd == "show":
        targets = _scopes(args.cwd, getattr(args, "scope", None))
        data = {}
        for label, root in targets:
            conn = _connect(root)
            try:
                if label == "repo":
                    try:
                        heal(conn)
                    except Exception:
                        pass
                f = load(conn)
                if getattr(args, "json", False):
                    data[label] = as_dict(conn, f)
                else:
                    _print(conn, label, f)
            finally:
                conn.close()
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        return 0

    scope = args.scope
    if cmd == "fire" and scope == "global" and args.feature:
        raise db.LedgerError("the global wiki has no ledger: a global fire is free text (--text)")
    conn = _connect(scoped_root(scope, args.cwd), create=cmd in ("northstar", "fire"))
    try:
        if cmd == "northstar":
            set_northstar(conn, args.text)
            print(f"northstar set ({scope})")
        elif cmd == "fire":
            set_fire(conn, args.slot, feature=args.feature, text=args.text)
            print(f"fire {int(args.slot)} set ({scope})")
        elif cmd == "unfire":
            clear_fire(conn, args.slot)
            print(f"fire {args.slot} cleared; the rest moved up")
        elif cmd == "order":
            reorder(conn, args.slots)
            print("fires reordered")
        elif cmd == "rank":
            value = None if str(args.rank).lower() in ("none", "clear", "-") else int(args.rank)
            rank_feature(conn, args.feature, value)
            print(f"{args.feature} rank {value if value is not None else 'cleared'}")
        elif cmd == "candidates":
            for item in candidates(conn):
                r = rank_of(conn, item.id)
                print(f"{item.id} {item.title}" + (f" (rank {r})" if r else ""))
        elif cmd == "promote":
            f = promote(conn, feature=args.feature, slot=args.slot)
            print("promoted: " + ", ".join(x.feature or x.text for x in f.fires if x))
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    run(run_focus, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
