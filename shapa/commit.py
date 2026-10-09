"""``shapa commit`` - a wiki's database commits itself.

``shapa.db`` is meant to be tracked in git (:mod:`shapa.db`'s module docstring):
it is the work ledger plus the memory/rule/issue rows, committed whole. Until
now nothing in the tool ever made that commit - ``shapa ledger pre-commit``
only ever REFUSED to let it ride on a feature branch, so a stranger's wiki
sat uncommitted forever and a clone never saw it. This module is the other
half: it stages and commits the wiki itself, on the default branch only (the
binary file is unmergeable - a feature branch committing its own copy would
make every later checkout swap the database).

Runs as a ``SessionEnd`` hook, and again at the next ``SessionStart`` as a
catch-up for a session that crashed before ``SessionEnd`` fired. A hook
invocation (``--hook``) never blocks the session: every exception is
swallowed and it always exits 0, printing at most one short line.

Targets: the repo wiki in scope for the cwd plus the global wiki, each only
when it sits inside a git work tree. From a linked worktree, the actual
checkout operated on is the PRIMARY one - :func:`shapa.db.db_path` already
resolves a worktree's database to the primary's file, and that same mapping
gives the right ``(checkout, wiki_dir)`` pair here, so the logic is reused
rather than duplicated.

A target is committed only when all of these hold: HEAD is a branch and it
is the repo's default branch, no merge/rebase/cherry-pick/revert is in
progress, no ``index.lock`` is held, and the database is not mid-write (its
rollback-journal or WAL sidecar file present). ``git add -A`` is scoped to
the wiki directory alone and the commit itself names that same pathspec, so
any other files the operator has staged stay staged and uncommitted. If the
commit is rejected (e.g. the operator's own pre-commit hook), exactly the
paths this call staged are restored, leaving the index as it was.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from shapa import config, db, ledger

GIT_TIMEOUT = 10
#: The commit runs the repo's own pre-commit hooks (linters, formatters), so
#: it gets longer than a plumbing call; killing git mid-commit would leave an
#: index.lock behind. Still under the 60 s the installer gives the hook.
COMMIT_TIMEOUT = 45


def _git(cwd, *args, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def _mid_operation(git_dir: Path) -> str | None:
    """A short label when *git_dir* (a real ``.git`` directory, never a
    linked worktree's pointer file) is mid-merge/rebase/cherry-pick/revert,
    else ``None``."""
    if (git_dir / "MERGE_HEAD").exists():
        return "a merge"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "a cherry-pick"
    if (git_dir / "REVERT_HEAD").exists():
        return "a revert"
    if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
        return "a rebase"
    return None


def _journal_present(db_file: Path) -> bool:
    """Whether *db_file* has a live rollback-journal or WAL sidecar - i.e.
    a write transaction is open (db.py forces ``journal_mode = DELETE``, so
    this is normally the ``-journal`` suffix; ``-wal`` is checked too in
    case a database was ever opened under a different mode)."""
    return any((db_file.parent / (db_file.name + suffix)).exists()
              for suffix in ("-journal", "-wal"))


def _staged(checkout: Path, rel: str) -> set[str]:
    r = _git(checkout, "diff", "--cached", "--name-only", "--", rel)
    return {line for line in r.stdout.splitlines() if line}


def _targets(cwd=None) -> list[tuple[Path, Path]]:
    """``(checkout, wiki_dir)`` pairs in scope for *cwd*: the repo wiki (if
    any) and the global wiki, each mapped to its PRIMARY checkout when it
    sits inside a linked worktree, and kept only when that checkout is
    actually a git work tree - the global wiki in particular often isn't
    one, and a wiki with no git history has nothing to commit."""
    seen: set[Path] = set()
    out: list[tuple[Path, Path]] = []
    for wr in config.wiki_roots(cwd):
        if wr.kind not in ("repo", "global"):
            continue
        wiki_dir = db.db_path(wr.path).parent
        checkout = wiki_dir.parent
        if not (checkout / ".git").exists():
            continue
        key = checkout.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append((checkout, wiki_dir))
    return out


def _label(session_id) -> str:
    sid = str(session_id or "").strip()
    return f"session {sid[:8]}" if sid else "manual"


def _effective_scrub_terms(extra: list[str] | None) -> list[str]:
    """``--scrub`` terms plus the pointer file's ``scrub_terms`` (additive,
    de-duplicated) - no terms from either source means no scrub at all."""
    terms = [t for t in (extra or []) if t]
    for t in config.scrub_terms():
        if t not in terms:
            terms.append(t)
    return terms


def _commit_target(checkout: Path, wiki_dir: Path, label: str, *, dry_run: bool,
                   terms: list[str]) -> dict:
    rel = wiki_dir.name  # wiki_dir is always a direct child of checkout
    result: dict = {"checkout": str(checkout), "wiki": rel, "label": label}

    git_dir = checkout / ".git"
    if (git_dir / "index.lock").exists():
        return {**result, "status": "skipped", "reason": "index.lock present"}
    op = _mid_operation(git_dir)
    if op:
        return {**result, "status": "skipped", "reason": f"{op} in progress"}

    branch = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    base = ledger.default_branch(checkout)
    if not branch or branch != base:
        return {**result, "status": "skipped",
               "reason": f"HEAD is {branch or '(detached)'}, not the default branch {base}"}

    db_file = wiki_dir / db.DB_FILENAME
    if db_file.is_file() and _journal_present(db_file):
        return {**result, "status": "skipped", "reason": "database mid-write (journal present)"}

    if dry_run:
        status = _git(checkout, "status", "--porcelain", "--untracked-files=all", "--", rel)
        if status.stdout.strip():
            return {**result, "status": "dry-run", "reason": f"would commit {rel}"}
        return {**result, "status": "nothing", "reason": "nothing to commit"}

    if terms and db_file.is_file():
        conn = db.connect(wiki_dir)
        if conn is not None:
            try:
                db.scrub(conn, terms)
            finally:
                conn.close()

    before = _staged(checkout, rel)
    add = _git(checkout, "add", "-A", "--", rel)
    if add.returncode != 0:
        return {**result, "status": "failed", "reason": f"git add failed: {add.stderr.strip()}"}
    after = _staged(checkout, rel)
    if not after:
        return {**result, "status": "nothing", "reason": "nothing to commit"}

    message = f"chore(shapa): wiki ({label})"
    commit = _git(checkout, "commit", "-q", "-m", message, "--", rel, timeout=COMMIT_TIMEOUT)
    if commit.returncode != 0:
        new_paths = after - before
        if new_paths:
            _git(checkout, "restore", "--staged", "--", *sorted(new_paths))
        reason = (commit.stderr or commit.stdout).strip() or "commit refused"
        return {**result, "status": "failed", "reason": reason}

    sha = _git(checkout, "rev-parse", "--short", "HEAD").stdout.strip()
    return {**result, "status": "committed", "reason": message, "sha": sha}


def run_commit(cwd=None, *, label: str = "manual", dry_run: bool = False,
               scrub_terms: list[str] | None = None) -> list[dict]:
    """Try to commit every wiki in scope for *cwd*. Returns one result dict
    per target (empty list when no target sits inside a git work tree)."""
    terms = _effective_scrub_terms(scrub_terms)
    return [_commit_target(checkout, wiki_dir, label, dry_run=dry_run, terms=terms)
            for checkout, wiki_dir in _targets(cwd)]


def _report_lines(results: list[dict]) -> list[str]:
    if not results:
        return ["shapa: no wiki in a git work tree for this cwd"]
    lines = []
    for r in results:
        where = f"{r['checkout']} ({r['wiki']})"
        status = r["status"]
        if status == "committed":
            lines.append(f"committed {where}: {r['sha']}")
        elif status == "dry-run":
            lines.append(f"would commit {where}")
        elif status == "nothing":
            lines.append(f"nothing to commit at {where}")
        elif status == "skipped":
            lines.append(f"skipped {where}: {r['reason']}")
        else:
            lines.append(f"FAILED {where}: {r['reason']}")
    return lines


def _hook_summary(results: list[dict]) -> str | None:
    committed = [r for r in results if r["status"] == "committed"]
    failed = [r for r in results if r["status"] == "failed"]
    if committed:
        if len(committed) == 1:
            return f"shapa: committed wiki ({committed[0]['wiki']}) {committed[0]['sha']}"
        return f"shapa: committed {len(committed)} wikis"
    if failed:
        return f"shapa: wiki commit failed - {failed[0]['reason']}"
    return None


def _read_hook_stdin() -> dict:
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="shapa commit",
        description="Commit a wiki's database to its own repo, on the default branch only.",
    )
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--hook", action="store_true",
                        help="SessionEnd/SessionStart hook: read cwd/session_id from stdin "
                             "JSON, never block, at most one line of output.")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, stage nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--scrub", action="append", default=[], metavar="TERM",
                        help="replace TERM in every text column before committing (repeatable; "
                             "see also ~/.shapa/config.json's scrub_terms)")
    args = parser.parse_args(argv)

    if args.hook:
        try:
            data = _read_hook_stdin()
            cwd = data.get("cwd") or args.cwd
            label = _label(data.get("session_id"))
            results = run_commit(cwd, label=label, dry_run=False, scrub_terms=args.scrub)
            summary = _hook_summary(results)
        except Exception:
            summary = None
        if summary:
            print(summary)
        sys.exit(0)

    try:
        results = run_commit(args.cwd, label=_label(None), dry_run=args.dry_run,
                             scrub_terms=args.scrub)
    except Exception as exc:
        print(f"shapa: commit failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for line in _report_lines(results):
            print(line)

    sys.exit(1 if any(r["status"] == "failed" for r in results) else 0)


if __name__ == "__main__":
    main()
