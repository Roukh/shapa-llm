"""Tests for ``shapa commit`` (J39): the wiki commits itself on the default
branch only, scoping ``git add``/``git commit`` to the wiki directory alone
so anything else the operator has staged stays staged and uncommitted."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shapa import cli, commit, config, db, ledger


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=30)


@unittest.skipUnless(shutil.which("git"), "git not on PATH")
class CommitCase(unittest.TestCase):
    """A throwaway repo with a ``.shapa`` wiki scaffolded the real way (via
    ``shapa init``), one seed commit on ``main``. The suite-wide
    ``_isolated_git_config`` fixture (tests/conftest.py) keeps this off the
    machine's real global hooks and gives every commit an identity."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "shapa-config.json"
        self.patches = [mock.patch.object(config, "CONFIG_FILE", self.cfg)]
        for p in self.patches:
            p.start()

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.repo)

        self.wiki = self.repo / ".shapa"
        cli._init([str(self.wiki)])

        _git("add", "-A", cwd=self.repo)
        r = _git("commit", "-q", "-m", "seed wiki", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self) -> str:
        return _git("status", "--porcelain", cwd=self.repo).stdout

    def head_subject(self) -> str:
        return _git("log", "-1", "--format=%s", cwd=self.repo).stdout.strip()

    def head_sha(self) -> str:
        return _git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()

    def add_task(self, wiki=None, title: str = "a task") -> None:
        conn = db.connect(wiki if wiki is not None else self.wiki)
        db.add_item(conn, "T", title)
        conn.close()


class TestDefaultBranchCommit(CommitCase):
    def test_commits_only_the_wiki_and_leaves_other_staged_files_staged(self):
        self.add_task()
        other = self.repo / "other.txt"
        other.write_text("hello\n", encoding="utf-8")
        _git("add", "other.txt", cwd=self.repo)

        results = commit.run_commit(self.repo, label="test")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "committed", results[0])
        self.assertEqual(self.head_subject(), "chore(shapa): wiki (test)")

        # other.txt rode along in the index but never entered the commit.
        changed = _git("show", "--stat", "--format=", "HEAD", cwd=self.repo).stdout
        self.assertIn(".shapa", changed)
        self.assertNotIn("other.txt", changed)
        self.assertIn("A  other.txt", self.status())


class TestNothingToCommit(CommitCase):
    def test_no_changes_means_no_commit(self):
        before = self.head_sha()
        results = commit.run_commit(self.repo, label="test")
        self.assertEqual(results[0]["status"], "nothing")
        self.assertEqual(self.head_sha(), before)


class TestFeatureBranchSkipped(CommitCase):
    def test_feature_branch_is_skipped(self):
        _git("checkout", "-q", "-b", "F1-work", cwd=self.repo)
        self.add_task()
        before = self.head_sha()
        results = commit.run_commit(self.repo, label="test")
        self.assertEqual(results[0]["status"], "skipped")
        self.assertIn("main", results[0]["reason"])
        self.assertEqual(self.head_sha(), before)


class TestLinkedWorktreeCommitsPrimary(CommitCase):
    def test_commits_through_the_primary_checkout_from_a_worktree(self):
        wt_dir = self.tmp / "wt"
        r = _git("worktree", "add", "-q", "-b", "F1-work", str(wt_dir), cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        wt_wiki = wt_dir / ".shapa"

        self.add_task(wiki=wt_wiki, title="made from the worktree")

        results = commit.run_commit(wt_dir, label="test")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "committed", results[0])
        self.assertEqual(Path(results[0]["checkout"]).resolve(), self.repo.resolve())
        self.assertEqual(self.head_subject(), "chore(shapa): wiki (test)")

        # The worktree's own checked-out copy is never touched.
        self.assertEqual(_git("status", "--porcelain", cwd=wt_dir).stdout, "")


class TestMidMergeSkipped(CommitCase):
    def test_mid_merge_is_skipped(self):
        (self.repo / ".git" / "MERGE_HEAD").write_text(self.head_sha() + "\n", encoding="utf-8")
        self.add_task()
        before = self.head_sha()
        results = commit.run_commit(self.repo, label="test")
        self.assertEqual(results[0]["status"], "skipped")
        self.assertIn("merge", results[0]["reason"])
        self.assertEqual(self.head_sha(), before)


class TestJournalPresentSkipped(CommitCase):
    def test_database_mid_write_is_skipped(self):
        journal = self.wiki / (db.DB_FILENAME + "-journal")
        journal.write_text("", encoding="utf-8")
        results = commit.run_commit(self.repo, label="test")
        self.assertEqual(results[0]["status"], "skipped")
        self.assertIn("journal", results[0]["reason"])


class TestScrub(CommitCase):
    TERM = "codename-nightjar"

    def test_scrub_flag_redacts_before_commit(self):
        conn = db.connect(self.wiki)
        db.add_row(conn, "M", f"a private note about {self.TERM}")
        conn.close()
        results = commit.run_commit(self.repo, label="test", scrub_terms=[self.TERM])
        self.assertEqual(results[0]["status"], "committed", results[0])
        content = (self.wiki / db.DB_FILENAME).read_bytes()
        self.assertNotIn(self.TERM.encode(), content)
        self.assertIn(b"[workspace]", content)

    def test_scrub_that_finds_nothing_leaves_nothing_to_commit(self):
        conn = db.connect(self.wiki)
        db.add_row(conn, "M", "a note with no private term in it")
        conn.close()
        self.assertEqual(commit.run_commit(self.repo, label="test")[0]["status"], "committed")
        before = self.head_sha()
        results = commit.run_commit(self.repo, label="test", scrub_terms=[self.TERM])
        self.assertEqual(results[0]["status"], "nothing", results[0])
        self.assertEqual(self.head_sha(), before)

    def test_scrub_terms_from_config_json_are_applied_too(self):
        self.cfg.write_text(json.dumps({"scrub_terms": [self.TERM]}), encoding="utf-8")
        conn = db.connect(self.wiki)
        db.add_row(conn, "M", f"another note mentioning {self.TERM}")
        conn.close()
        results = commit.run_commit(self.repo, label="test")
        self.assertEqual(results[0]["status"], "committed", results[0])
        content = (self.wiki / db.DB_FILENAME).read_bytes()
        self.assertNotIn(self.TERM.encode(), content)


class TestRejectingHookLeavesIndexAsItWas(CommitCase):
    def test_rejecting_pre_commit_hook_leaves_the_index_as_it_was(self):
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\necho rejected by the operator's own hook >&2\nexit 1\n",
                        encoding="utf-8")
        hook.chmod(0o755)

        other = self.repo / "other.txt"
        other.write_text("hello\n", encoding="utf-8")
        _git("add", "other.txt", cwd=self.repo)
        self.add_task()

        before = self.head_sha()
        results = commit.run_commit(self.repo, label="test")
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(self.head_sha(), before, "nothing should have been committed")

        status = self.status()
        self.assertIn("A  other.txt", status, "the operator's own staged file stays staged")
        self.assertIn(" M .shapa/shapa.db", status, "the wiki is unstaged again, not committed")
        self.assertNotIn("M  .shapa/shapa.db", status, "the wiki must not still be staged")


class TestHook(CommitCase):
    def _run_hook(self, payload: dict) -> tuple[int, str]:
        stdin = io.StringIO(json.dumps(payload))
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out), \
             self.assertRaises(SystemExit) as caught:
            commit.main(["--hook"])
        return caught.exception.code, out.getvalue()

    def test_hook_commits_and_prints_one_line(self):
        self.add_task()
        code, out = self._run_hook({"cwd": str(self.repo), "session_id": "abcdef1234567890"})
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertIn("committed", lines[0])
        self.assertEqual(self.head_subject(), "chore(shapa): wiki (session abcdef12)")

    def test_hook_never_blocks_when_git_is_broken(self):
        self.add_task()
        with mock.patch.object(commit, "_git", side_effect=RuntimeError("boom")):
            code, out = self._run_hook({"cwd": str(self.repo)})
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line]
        self.assertLessEqual(len(lines), 1)


class TestDefaultBranchFallback(unittest.TestCase):
    """``ledger.default_branch``'s fallback chain for a repo with no origin
    (J39 fixed this: it used to just hardcode ``main``)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_falls_back_to_init_default_branch_config(self):
        _git("init", "-q", cwd=self.repo)
        _git("config", "init.defaultBranch", "trunk", cwd=self.repo)
        self.assertEqual(ledger.default_branch(self.repo), "trunk")

    def test_falls_back_to_the_only_local_branch(self):
        _git("init", "-q", "-b", "develop", cwd=self.repo)
        (self.repo / "f.txt").write_text("x\n", encoding="utf-8")
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-q", "-m", "seed", cwd=self.repo)
        self.assertEqual(ledger.default_branch(self.repo), "develop")


if __name__ == "__main__":
    unittest.main()
