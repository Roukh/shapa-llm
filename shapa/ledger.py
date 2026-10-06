"""``shapa ledger`` / ``shapa row`` / ``shapa correction`` - the format-4 CLI.

The work ledger (features, jobs, tasks) and the memory/rule/issue rows live
in ``<wiki>/shapa.db`` (:mod:`shapa.db`). This module is the command line
and the hook entry points around it:

- ``shapa ledger ...`` - add, claim, close, list the work; the git
  triggers (``on-commit`` from a ``post-commit`` hook, ``on-merge`` /
  ``reconcile`` after a PR merge, ``hook-posttool`` for a harness
  PostToolUse hook on ``gh pr merge``); the after-feature ``sweep``.
- ``shapa row ...`` - add, edit, remove, tag, link memory/rule/issue rows.
- ``shapa correction`` - the UserPromptSubmit hook: an operator message
  that corrects the agent ("no", "wrong", "not like this", ...) becomes an
  issue row, linked to the work items claimed at the time.

Hooks never run a verify command and never fail the harness: they exit 0
and print at most one line. A verify command runs only on an explicit
``shapa ledger close`` (the agent's own, sandboxed shell).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from shapa import config, db

VERIFY_TIMEOUT = 300
GIT_TIMEOUT = 10
GH_TIMEOUT = 8
LIVE_FEATURES_SHOWN = 8
TEMP_DIRNAME = "temp"

#: An operator message that corrects the agent. Anchored cues at the start
#: of the message ("no", "wrong", "stop", ...) or strong phrases anywhere.
CORRECTION_RE = re.compile(
    r"^\s*(?:no+|nope|nah|wrong|stop|wait|wtf|ugh|undo|revert|incorrect)\b"
    r"|\b(?:wrong|what the (?:fuck|hell|heck)|wtf|not like this|not like that|not quite"
    r"|not (?:what|how) i (?:asked|said|meant|wanted)|that'?s not (?:it|right|what|how)"
    r"|i (?:said|told you|asked you|already said)|you (?:didn'?t|did not|forgot|missed|ignored|broke)"
    r"|why (?:did|would) you|do(?:n'?t| not) do that|that'?s wrong|my tempo|try again"
    r"|this is (?:wrong|broken)|you'?re wrong|not what i)\b",
    re.I)


class CommandError(Exception):
    pass


# --- resolving the wiki ------------------------------------------------------------

def _roots(cwd=None):
    return config.wiki_roots(cwd)


def repo_root(cwd=None) -> Path:
    """The repo wiki for *cwd* - the only place features, jobs and tasks
    live (a global wiki has no work ledger)."""
    for wr in _roots(cwd):
        if wr.kind == "repo":
            return Path(wr.path)
    raise CommandError("no repo wiki here - the work ledger lives in a repo's own .shapa "
                       "(the global wiki holds memories, rules and issues only)")


def scoped_root(scope: str, cwd=None) -> Path:
    if scope == "global":
        return config.global_root()
    if scope == "repo":
        return repo_root(cwd)
    raise CommandError("--scope must be global or repo (see placement.md)")


def _connect(root: Path, create: bool = False):
    conn = db.connect(root, create=create)
    if conn is None:
        raise CommandError(f"{root} has no database yet - `shapa upgrade {root}` "
                           "brings a wiki to format 4")
    return conn


def default_root_for_hook(cwd=None) -> Path | None:
    """Repo wiki with a database, else the global wiki's, else None."""
    roots = _roots(cwd)
    for wr in roots:
        if wr.kind == "repo" and db.exists(wr.path):
            return Path(wr.path)
    for wr in roots:
        if wr.kind == "global" and db.exists(wr.path):
            return Path(wr.path)
    return None


# --- git helpers ---------------------------------------------------------------------

def _git(cwd, *args, timeout=GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def _repo_dir(root: Path) -> Path:
    """The checkout a wiki belongs to (its parent directory)."""
    return Path(root).resolve().parent


def default_branch(repo: Path) -> str:
    r = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/", 1)[-1]
    for name in ("main", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
            return name
    return "main"


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "work"


def branch_name(item: db.Item) -> str:
    return f"{item.id}-{slug(item.title)}"


def _branch_merged(repo: Path, branch: str, base: str) -> bool:
    """Whether *branch* is merged: a merged PR on GitHub (when ``gh`` answers
    in time), else the branch tip is an ancestor of the local or remote
    default branch."""
    if shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "pr", "list", "--state", "merged", "--head", branch,
                                "--json", "number", "--jq", "length"], cwd=str(repo),
                               capture_output=True, text=True, timeout=GH_TIMEOUT, check=False)
            if r.returncode == 0 and r.stdout.strip() not in ("", "0"):
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    tip = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if tip.returncode != 0:
        tip = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
    if tip.returncode != 0 or not tip.stdout.strip():
        return False
    sha = tip.stdout.strip()
    for target in (base, f"origin/{base}"):
        if _git(repo, "rev-parse", "--verify", "--quiet", target).returncode != 0:
            continue
        if _git(repo, "merge-base", "--is-ancestor", sha, target).returncode == 0:
            base_sha = _git(repo, "rev-parse", target).stdout.strip()
            if base_sha != sha:  # a fresh branch with no commits is not "merged"
                return True
    return False


# --- rendering -------------------------------------------------------------------------

def _item_line(item: db.Item, conn=None) -> str:
    extra = []
    if item.status == "claimed":
        extra.append("claimed")
    if item.git_ref:
        extra.append(item.git_ref)
    if item.alias:
        extra.append(f"was {item.alias}")
    if conn is not None and item.kind in ("F", "J"):
        n = conn.execute("SELECT COUNT(*) FROM items WHERE parent = ? AND status != 'closed'",
                         (item.id,)).fetchone()[0]
        if n:
            extra.append(f"{n} open {'jobs' if item.kind == 'F' else 'tasks'}")
    tail = f" ({', '.join(extra)})" if extra else ""
    return f"{item.id} {item.title}{tail}"


def overview(root: Path, repo_label: str | None = None, *, work: bool = True) -> list[str]:
    """The session-start ledger block for one wiki: counts, live features,
    claimed items, flagged rule conflicts. ``[]`` when the wiki has no
    database or nothing live."""
    conn = db.connect(root)
    if conn is None:
        return []
    try:
        c = db.counts(conn)
        features = db.items(conn, status="live", kind="F")
        claimed = [i for i in db.items(conn, status="claimed")]
        standalone = [i for i in db.items(conn, status="live")
                      if i.kind != "F" and not i.parent]
        flagged = db.conflicts(conn)
        lines = []
        live = c["F"] + c["J"] + c["T"]
        label = f" {repo_label}" if repo_label else ""
        rows_text = f"{c['M']} memories, {c['R']} rules, {c['I']} issues"
        if work and (live or c["M"] + c["R"] + c["I"]):
            lines.append(f"ledger{label}: {c['F']} features, {c['J']} jobs, {c['T']} tasks open; "
                         f"{rows_text}. `shapa ledger` lists, `shapa ledger claim ID` takes one.")
        elif c["M"] + c["R"] + c["I"]:
            lines.append(f"rows{label}: {rows_text}.")
        for item in features[:LIVE_FEATURES_SHOWN]:
            lines.append(f"- {_item_line(item, conn)}")
        if len(features) > LIVE_FEATURES_SHOWN:
            lines.append(f"- … {len(features) - LIVE_FEATURES_SHOWN} more features")
        if standalone:
            lines.append(f"- {len(standalone)} standalone jobs/tasks")
        for item in claimed:
            if item.kind != "F":
                lines.append(f"- claimed: {_item_line(item)}")
        if flagged:
            lines.append("- possible rule conflicts to judge: "
                         + ", ".join(f"{a}/{b}" for a, b in flagged[:6]))
        return lines
    finally:
        conn.close()


# --- ledger commands ------------------------------------------------------------------------

def _print_list(conn, show_closed: bool) -> None:
    status = None if show_closed else "live"
    feats = db.items(conn, status=status, kind="F")
    for f in feats:
        print(_item_line(f, conn))
        for depth, child in db.tree(conn, f.id)[1:]:
            if not show_closed and child.status == "closed":
                continue
            print("  " * depth + _item_line(child, conn))
    loose = [i for i in db.items(conn, status=status) if i.kind != "F" and not i.parent]
    if loose:
        print("standalone:")
        for i in loose:
            print("  " + _item_line(i, conn))
    if not feats and not loose:
        print("ledger: nothing open")


def _run_verify(repo: Path, command: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(["bash", "-c", command], cwd=str(repo), capture_output=True, text=True,
                           timeout=VERIFY_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return False, f"verify timed out after {VERIFY_TIMEOUT}s"
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out[-1200:]


def cmd_close(root: Path, item_id: str, reason: str, ref: str | None, skip_verify: bool) -> int:
    conn = _connect(root)
    try:
        item = db.get_item(conn, item_id)
        if item is None:
            raise CommandError(f"no ledger item {item_id!r}")
        if item.verify and not skip_verify and reason == "agent":
            ok, out = _run_verify(_repo_dir(root), item.verify)
            if not ok:
                print(f"{item.id} stays open: its verify failed\n$ {item.verify}\n{out}",
                      file=sys.stderr)
                return 1
        closed = db.close(conn, item.id, reason, git_ref=ref)
    finally:
        conn.close()
    print(f"closed {', '.join(closed)}" if closed else f"{item_id} was already closed")
    return 0


def on_commit(cwd=None) -> list[str]:
    """git ``post-commit``: close the job a ``J<n>:`` subject names."""
    repo = Path(cwd or Path.cwd())
    r = _git(repo, "log", "-1", "--format=%H%n%B")
    if r.returncode != 0:
        return []
    sha, _, message = r.stdout.partition("\n")
    job = db.job_of_message(message)
    if not job:
        return []
    root = default_root_for_hook(repo)
    if root is None:
        return []
    conn = db.connect(root)
    if conn is None:
        return []
    try:
        if db.get_item(conn, job) is None:
            return []
        return db.close(conn, job, "commit", git_ref=sha.strip()[:12])
    finally:
        conn.close()


def pre_commit(cwd=None) -> list[str]:
    """git ``pre-commit``: a wiki database may be committed only on the
    default branch. A feature branch that commits its own copy makes every
    later checkout swap the database. Returns the problems (empty = allow)."""
    repo = Path(cwd or Path.cwd())
    staged = _git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR")
    if staged.returncode != 0:
        return []
    dbs = [p for p in staged.stdout.split("\n") if p.endswith("/" + db.DB_FILENAME)
           or p == db.DB_FILENAME]
    if not dbs:
        return []
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    base = default_branch(repo)
    if branch == base:
        return []
    return [f"{p} is staged on branch {branch or '(detached)'}; the database commits only on "
            f"{base} (unstage it: git restore --staged {p})" for p in dbs]


def _vectors_and_uses(root: Path):
    """Row vectors and use counts from the derived index (both optional)."""
    from shapa import embed, memlog

    vectors, uses = None, None
    try:
        conn = memlog.open_index(root, embed_vectors=embed.available())
    except Exception:
        conn = None
    if conn is None:
        return None, None
    try:
        uses = memlog.uses(conn)
        if embed.available():
            model = embed.model_name()
            vectors = {}
            from array import array
            for rid, blob in conn.execute(
                    "SELECT m.id, v.vec FROM memories m JOIN memory_vectors v ON v.hash = m.hash "
                    "AND v.model = ? WHERE m.source != '' AND m.kind IN ('memory','rule','issue')",
                    (model,)):
                vectors[rid] = list(array("f", blob))
    except Exception:
        vectors = None
    finally:
        conn.close()
    return vectors, uses


def run_sweep(root: Path, *, keep_closed_since: str | None = None) -> db.SweepReport:
    vectors, uses = _vectors_and_uses(root)
    conn = _connect(root)
    try:
        return db.sweep(conn, vectors=vectors, uses=uses, keep_closed_since=keep_closed_since)
    finally:
        conn.close()


def remove_temp(root: Path, feature: str) -> bool:
    """Delete ``temp/<feature>/`` in the wiki and in the primary checkout's
    copy of it - scrap notes leave with their feature."""
    gone = False
    for base in {Path(root), db.db_path(root).parent}:
        d = base / TEMP_DIRNAME / feature
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            gone = True
    return gone


def reconcile(cwd=None) -> list[str]:
    """Close every live feature whose branch has merged, then sweep once.
    Returns report lines (empty when nothing merged)."""
    root = default_root_for_hook(cwd)
    if root is None:
        return []
    repo = db.db_path(root).parent.parent  # the primary checkout, even from a worktree
    conn = db.connect(root)
    if conn is None:
        return []
    started = db.now_iso()
    merged: list[str] = []
    try:
        base = default_branch(repo)
        for f in db.items(conn, status="live", kind="F"):
            branch = f.git_ref or ""
            if not branch:
                local = _git(repo, "for-each-ref", "--format=%(refname:short)",
                             f"refs/heads/{f.id}-*", f"refs/heads/{f.id.lower()}-*")
                names = [n for n in local.stdout.split() if db.feature_of_branch(n) == f.id]
                branch = names[0] if names else ""
            if branch and _branch_merged(repo, branch, base):
                merged.extend(db.close(conn, f.id, "merge", git_ref=branch))
                remove_temp(root, f.id)
    finally:
        conn.close()
    if not merged:
        return []
    report = run_sweep(root, keep_closed_since=started)
    return [f"merged: closed {', '.join(merged)}", *report.lines()]


def on_merge(cwd=None, branch: str | None = None) -> list[str]:
    """After a PR merge: close the named branch's feature (or every feature
    whose branch merged), then sweep."""
    if branch:
        feature = db.feature_of_branch(branch)
        root = default_root_for_hook(cwd)
        if feature and root is not None:
            conn = db.connect(root)
            if conn is not None:
                started = db.now_iso()
                try:
                    if db.get_item(conn, feature) is None:
                        return reconcile(cwd)
                    closed = db.close(conn, feature, "merge", git_ref=branch)
                finally:
                    conn.close()
                remove_temp(root, feature)
                report = run_sweep(root, keep_closed_since=started)
                return [f"merged: closed {', '.join(closed) or feature}", *report.lines()]
    return reconcile(cwd)


_GH_MERGE_RE = re.compile(r"\bgh\s+pr\s+merge\b")


def hook_posttool(data: dict) -> list[str]:
    """PostToolUse on a shell tool: a ``gh pr merge`` triggers reconcile."""
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not _GH_MERGE_RE.search(command):
        return []
    return reconcile(data.get("cwd"))


def _read_hook_stdin() -> dict:
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _hook_print(lines: list[str], event: str | None = None) -> None:
    if not lines:
        return
    text = "shapa: " + "; ".join(lines)
    if event:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                 "additionalContext": text}}))
    else:
        print(text)


def _subcommand_adder(sub):
    """``--cwd`` is accepted before or after the subcommand."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", default=argparse.SUPPRESS)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    return add


def ledger_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa ledger", description="The work ledger: features "
                                "(branch + PR), jobs (one commit), tasks (no git artifact).")
    p.add_argument("--cwd", default=None)
    sub = p.add_subparsers(dest="cmd")
    add = _subcommand_adder(sub)
    ls = add("list", help="live features with their jobs and tasks (default)")
    ls.add_argument("--all", action="store_true", help="include closed items")
    a = add("add", help="add a feature, job or task")
    a.add_argument("kind", choices=["F", "J", "T", "f", "j", "t"])
    a.add_argument("title")
    a.add_argument("--parent")
    a.add_argument("--body", default="")
    a.add_argument("--verify", help="shell command that exits 0 only when the item is done")
    for name in ("claim", "release"):
        c = add(name)
        c.add_argument("ids", nargs="+")
        c.add_argument("--session", default="")
    cl = add("close", help="close an item (runs its verify first)")
    cl.add_argument("id")
    cl.add_argument("--reason", default="agent", choices=list(db.CLOSE_REASONS))
    cl.add_argument("--ref")
    cl.add_argument("--no-verify", action="store_true",
                    help="operator closes only: skip the verify command")
    t = add("tree")
    t.add_argument("id")
    br = add("branch", help="name a feature's branch and record it")
    br.add_argument("id")
    ed = add("edit")
    ed.add_argument("id")
    ed.add_argument("--title")
    ed.add_argument("--body")
    ed.add_argument("--verify")
    iss = add("issues", help="past issues most related to an item")
    iss.add_argument("id")
    iss.add_argument("-k", type=int, default=5)
    add("on-commit", help="git post-commit hook")
    add("pre-commit", help="git pre-commit hook: the database commits only on the "
                   "default branch (exit 1 otherwise)")
    om = add("on-merge", help="after a PR merge")
    om.add_argument("--branch")
    add("reconcile", help="close features whose branch merged, then sweep")
    add("hook-posttool", help="PostToolUse hook (stdin JSON)")
    add("hook-start", help="SessionStart hook: reconcile merged features")
    sw = add("sweep", help="after-feature cleanup of the database")
    sw.add_argument("--scope", choices=["repo", "global"], default="repo")
    sc = add("scrub", help="replace private terms in every text column")
    sc.add_argument("--term", action="append", default=[])
    sc.add_argument("--scope", choices=["repo", "global"], default="repo")
    args = p.parse_args(argv)
    cmd = args.cmd or "list"

    if cmd == "on-commit":
        try:
            closed = on_commit(args.cwd)
        except Exception:
            closed = []
        if closed:
            print(f"shapa: closed {', '.join(closed)} (commit)")
        return 0
    if cmd == "pre-commit":
        try:
            problems = pre_commit(args.cwd)
        except Exception:
            problems = []
        for line in problems:
            print(f"shapa: {line}", file=sys.stderr)
        return 1 if problems else 0
    if cmd in ("hook-posttool", "hook-start"):
        data = _read_hook_stdin()
        try:
            lines = hook_posttool(data) if cmd == "hook-posttool" else reconcile(
                data.get("cwd") or args.cwd)
        except Exception:
            lines = []
        _hook_print(lines, "PostToolUse" if cmd == "hook-posttool" else "SessionStart")
        return 0

    if cmd == "on-merge":
        for line in on_merge(args.cwd, args.branch):
            print(line)
        return 0
    if cmd == "reconcile":
        lines = reconcile(args.cwd)
        print("\n".join(lines) if lines else "no merged features")
        return 0
    if cmd in ("sweep", "scrub"):
        root = scoped_root(args.scope, args.cwd)
        if cmd == "sweep":
            report = run_sweep(root)
            print("\n".join(report.lines()) or "sweep: nothing to clean")
        else:
            conn = _connect(root)
            try:
                print(f"scrubbed {db.scrub(conn, args.term)} rows")
            finally:
                conn.close()
        return 0

    root = repo_root(args.cwd)
    if cmd == "close":
        return cmd_close(root, args.id, args.reason, args.ref, args.no_verify)
    conn = _connect(root, create=False)
    try:
        if cmd == "list":
            _print_list(conn, getattr(args, "all", False))
        elif cmd == "add":
            new = db.add_item(conn, args.kind, args.title, parent=args.parent, body=args.body,
                              verify=args.verify)
            print(new)
        elif cmd in ("claim", "release"):
            for i in args.ids:
                item = db.claim(conn, i, args.session) if cmd == "claim" else db.release(conn, i)
                print(f"{item.id} {item.status}")
                if cmd == "claim":
                    for score, row in db.related_issues(conn, item.id, k=3):
                        print(f"  past issue {row.id}: {row.summary}")
        elif cmd == "tree":
            for depth, item in db.tree(conn, args.id):
                print("  " * depth + _item_line(item) + ("" if item.status != "closed"
                                                          else f" [closed: {item.closed_reason}]"))
        elif cmd == "branch":
            item = db.get_item(conn, args.id)
            if item is None or item.kind != "F":
                raise CommandError("only a feature gets a branch")
            name = item.git_ref or branch_name(item)
            db.update_item(conn, item.id, git_ref=name)
            print(name)
        elif cmd == "edit":
            fields = {k: v for k, v in (("title", args.title), ("body", args.body),
                                        ("verify", args.verify)) if v is not None}
            print(_item_line(db.update_item(conn, args.id, **fields)))
        elif cmd == "issues":
            vectors, _ = _vectors_and_uses(root)
            qv = None
            if vectors:
                from shapa import embed
                item = db.get_item(conn, args.id)
                qv = embed.embed_one(item.title + "\n" + item.body) if item else None
            for score, row in db.related_issues(conn, args.id, k=args.k, vectors=vectors,
                                                query_vector=qv):
                print(f"{row.id} ({score:.2f}) {row.summary}")
    finally:
        conn.close()
    return 0


# --- rows -----------------------------------------------------------------------------------

def row_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa row", description="Memory (M), rule (R) and issue "
                                "(I) rows in a wiki's database.")
    p.add_argument("--cwd", default=None)
    sub = p.add_subparsers(dest="cmd")
    add = _subcommand_adder(sub)
    ls = add("list")
    ls.add_argument("kind", nargs="?", choices=["M", "R", "I", "m", "r", "i"])
    ls.add_argument("--scope", choices=["repo", "global"], default=None)
    a = add("add")
    a.add_argument("kind", choices=["M", "R", "I", "m", "r", "i"])
    a.add_argument("summary")
    a.add_argument("--body", default="")
    a.add_argument("--scope", required=True, choices=["repo", "global"],
                   help="placement.md: would this still be true in a different repo tomorrow?")
    a.add_argument("--tag", action="append", default=[])
    a.add_argument("--about", action="append", default=[], help="a ledger item this concerns")
    a.add_argument("--alias")
    a.add_argument("--source", default="agent")
    ed = add("edit")
    ed.add_argument("id")
    ed.add_argument("--summary")
    ed.add_argument("--body")
    ed.add_argument("--alias")
    ed.add_argument("--scope", choices=["repo", "global"], default=None)
    rm = add("rm")
    rm.add_argument("ids", nargs="+")
    rm.add_argument("--scope", choices=["repo", "global"], default=None)
    lk = add("link")
    lk.add_argument("src")
    lk.add_argument("rel", choices=list(db.LINK_RELS))
    lk.add_argument("dst")
    lk.add_argument("--scope", choices=["repo", "global"], default=None)
    tg = add("tag")
    tg.add_argument("id")
    tg.add_argument("tags", nargs="+")
    tg.add_argument("--scope", choices=["repo", "global"], default=None)
    args = p.parse_args(argv)
    cmd = args.cmd or "list"

    def root_for(scope, row_id=None) -> Path:
        if scope:
            return scoped_root(scope, args.cwd)
        # No scope: the first wiki in scope whose database holds row_id.
        for wr in _roots(args.cwd):
            if not db.exists(wr.path):
                continue
            if row_id is None:
                return Path(wr.path)
            conn = db.connect(wr.path)
            try:
                if db.get_row(conn, row_id) or db.get_item(conn, row_id):
                    return Path(wr.path)
            finally:
                conn.close()
        raise CommandError(f"no wiki in scope holds {row_id}" if row_id else "no database in scope")

    if cmd == "list":
        scope, kind = getattr(args, "scope", None), getattr(args, "kind", None)
        targets = [scoped_root(scope, args.cwd)] if scope else [
            Path(wr.path) for wr in _roots(args.cwd) if db.exists(wr.path)]
        for root in targets:
            conn = _connect(root)
            try:
                print(f"# {root}")
                for r in db.rows(conn, kind.upper() if kind else None):
                    alias = f" [{r.alias}]" if r.alias else ""
                    print(f"{r.id}{alias} {r.summary}")
            finally:
                conn.close()
        return 0
    if cmd == "add":
        root = scoped_root(args.scope, args.cwd)
        conn = _connect(root, create=True)
        try:
            rid, created = db.add_row(conn, args.kind, args.summary, args.body, tags=args.tag,
                                      about=args.about, alias=args.alias, source=args.source)
        finally:
            conn.close()
        print(rid if created else f"{rid} (identical row already stored)")
        return 0
    root = root_for(getattr(args, "scope", None),
                    getattr(args, "id", None) or getattr(args, "src", None)
                    or (args.ids[0] if getattr(args, "ids", None) else None))
    conn = _connect(root)
    try:
        if cmd == "edit":
            r = db.update_row(conn, args.id, summary=args.summary, body=args.body, alias=args.alias)
            print(f"{r.id} {r.summary}")
        elif cmd == "rm":
            print(f"removed {db.delete(conn, args.ids)}")
        elif cmd == "link":
            db.link(conn, args.src, args.dst, args.rel)
            print(f"{args.src} {args.rel} {args.dst}")
        elif cmd == "tag":
            db.tag(conn, args.id, args.tags)
            print(f"{args.id}: {', '.join(db._tags_of(conn, args.id))}")
    finally:
        conn.close()
    return 0


# --- corrections -> issues --------------------------------------------------------------------

def _last_assistant_text(transcript_path: str | None, limit: int = 400) -> str:
    if not transcript_path:
        return ""
    try:
        from shapa import capture

        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 400_000))
            tail = fh.read().decode("utf-8", errors="replace")
        entries = [e for e in (capture._parse_entry(l) for l in tail.splitlines()) if e is not None]
        return " ".join(capture._final_assistant_text(entries).split())[:limit]
    except Exception:
        return ""


def is_correction(text: str) -> bool:
    return bool(CORRECTION_RE.search(text or ""))


def record_correction(prompt: str, *, cwd=None, session: str = "",
                      transcript_path: str | None = None, now: datetime | None = None
                      ) -> tuple[Path, str] | None:
    """Write an issue row for an operator correction. ``None`` when *prompt*
    is not a correction or no wiki in scope has a database."""
    from shapa import capture

    text = capture._clean_user_text(prompt or "")
    if not text or not is_correction(text):
        return None
    root = default_root_for_hook(cwd)
    if root is None:
        return None
    conn = db.connect(root)
    if conn is None:
        return None
    try:
        claimed = [i.id for i in db.items(conn, status="claimed")]
        before = _last_assistant_text(transcript_path)
        body = f"Operator: {text[:600]}"
        if before:
            body += f"\nAgent before: {before}"
        first = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
        rid, _ = db.add_row(conn, "I", first, body, tags=["correction"], about=claimed,
                            source="correction", session=(session or "")[:8], now=now)
    finally:
        conn.close()
    return root, rid


def correction_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shapa correction",
                                description="UserPromptSubmit hook: log operator corrections "
                                            "as issue rows.")
    p.add_argument("--text", help="classify and record TEXT instead of reading the hook stdin")
    p.add_argument("--check", action="store_true", help="only print whether TEXT is a correction")
    args = p.parse_args(argv)
    if args.text is not None and args.check:
        print("correction" if is_correction(args.text) else "not a correction")
        return 0
    data = {"prompt": args.text} if args.text is not None else _read_hook_stdin()
    try:
        hit = record_correction(str(data.get("prompt") or ""), cwd=data.get("cwd"),
                                session=str(data.get("session_id") or ""),
                                transcript_path=data.get("transcript_path"))
    except Exception:
        hit = None
    if hit is not None:
        root, rid = hit
        print(f"shapa: logged this correction as issue {rid} ({root})")
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        code = ledger_main(argv)
    except (CommandError, db.LedgerError) as exc:
        print(f"shapa ledger: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)


def run(entry, argv) -> None:
    try:
        code = entry(argv)
    except (CommandError, db.LedgerError) as exc:
        print(f"shapa: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)
