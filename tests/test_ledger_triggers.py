"""Git triggers for the work ledger, against a real throwaway repository: a
``J<n>:`` commit closes its job, a merged feature branch closes its feature
(then sweeps), and every worktree shares the primary checkout's database."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from shapa import config, db, ledger


class GitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        pointer = self.tmp / "config.json"
        pointer.write_text(json.dumps({"memory": str(self.tmp / "global")}), encoding="utf-8")
        self.patches = [mock.patch.object(config, "CONFIG_FILE", pointer),
                        mock.patch.object(ledger.shutil, "which", return_value=None)]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.wiki = self.repo / ".shapa"
        self.wiki.mkdir()
        (self.wiki / "AGENTS.md").write_text("# marker\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
        db.connect(self.wiki, create=True).close()
        self.commit("initial")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args, cwd=None) -> subprocess.CompletedProcess:
        # core.hooksPath off: a disposable fixture repo, not a real commit.
        r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                           cwd=str(cwd or self.repo), capture_output=True, text=True, timeout=30)
        return r

    def commit(self, message: str, cwd=None) -> None:
        self.git("add", "-A", cwd=cwd)
        r = self.git("commit", "-q", "--allow-empty", "-m", message, cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stderr)

    def conn(self):
        return db.connect(self.wiki)


class TestOnCommit(GitCase):
    def test_a_job_commit_closes_the_job(self):
        conn = self.conn()
        f1 = db.add_item(conn, "F", "ledger")
        j1 = db.add_item(conn, "J", "schema", parent=f1)
        conn.close()
        (self.repo / "schema.sql").write_text("create table x(y);\n")
        self.commit(f"{j1}: add the schema")
        self.assertEqual(ledger.on_commit(self.repo), [j1])
        conn = self.conn()
        try:
            item = db.get_item(conn, j1)
            self.assertEqual((item.status, item.closed_reason), ("closed", "commit"))
            self.assertEqual(len(item.git_ref), 12)
            self.assertEqual(db.get_item(conn, f1).status, "open")
        finally:
            conn.close()

    def test_other_commits_and_unknown_jobs_do_nothing(self):
        self.commit("chore: tidy")
        self.assertEqual(ledger.on_commit(self.repo), [])
        self.commit("J99: no such job")
        self.assertEqual(ledger.on_commit(self.repo), [])


class TestWorktrees(GitCase):
    def test_every_worktree_uses_the_primary_database(self):
        r = self.git("worktree", "add", "-q", "-b", "F1-ledger", str(self.repo / ".worktrees" / "F1-ledger"))
        self.assertEqual(r.returncode, 0, r.stderr)
        wt_wiki = self.repo / ".worktrees" / "F1-ledger" / ".shapa"
        self.assertEqual(db.db_path(wt_wiki), (self.wiki / db.DB_FILENAME).resolve())
        conn = db.connect(wt_wiki)
        db.add_item(conn, "F", "made from the worktree")
        conn.close()
        conn = self.conn()
        try:
            self.assertEqual(db.get_item(conn, "F1").title, "made from the worktree")
        finally:
            conn.close()
        # The worktree's own checked-out copy is never written.
        self.assertEqual(self.git("status", "--porcelain", cwd=wt_wiki.parent).stdout, "")


class TestPreCommit(GitCase):
    def test_the_database_commits_only_on_the_default_branch(self):
        conn = self.conn()
        db.add_item(conn, "F", "x")
        conn.close()
        self.git("add", ".shapa/shapa.db")
        self.assertEqual(ledger.pre_commit(self.repo), [])
        self.git("checkout", "-q", "-b", "F1-x")
        problems = ledger.pre_commit(self.repo)
        self.assertEqual(len(problems), 1)
        self.assertIn(".shapa/shapa.db", problems[0])


class TestMerge(GitCase):
    def _feature_with_branch(self):
        conn = self.conn()
        f1 = db.add_item(conn, "F", "ledger")
        db.add_item(conn, "J", "schema", parent=f1)
        branch = ledger.branch_name(db.get_item(conn, f1))
        db.update_item(conn, f1, git_ref=branch)
        conn.close()
        # Feature work happens in a worktree; the primary checkout stays on
        # main, where the database (with uncommitted rows) lives.
        wt = self.repo / ".worktrees" / branch
        r = self.git("worktree", "add", "-q", "-b", branch, str(wt))
        self.assertEqual(r.returncode, 0, r.stderr)
        (wt / "feature.txt").write_text("work\n")
        self.commit("J1: the work", cwd=wt)
        return f1, branch

    def test_unmerged_branch_keeps_the_feature_open(self):
        f1, _ = self._feature_with_branch()
        self.assertEqual(ledger.reconcile(self.repo), [])
        conn = self.conn()
        try:
            self.assertEqual(db.get_item(conn, f1).status, "open")
        finally:
            conn.close()

    def test_a_merged_branch_closes_the_feature_cleans_temp_and_sweeps(self):
        f1, branch = self._feature_with_branch()
        scrap = self.wiki / "temp" / f1 / "notes.md"
        scrap.parent.mkdir(parents=True)
        scrap.write_text("scrap\n")
        conn = self.conn()
        old_done = db.add_item(conn, "T", "done earlier")
        db.close(conn, old_done, "agent", now=datetime.now(timezone.utc) - timedelta(hours=1))
        conn.close()
        r = self.git("merge", "-q", "--no-ff", branch, "-m", f"Merge {branch}")
        self.assertEqual(r.returncode, 0, r.stderr)

        lines = ledger.reconcile(self.repo)
        self.assertEqual(lines[0], f"merged: closed {f1}, J1")
        self.assertFalse(scrap.parent.exists())
        conn = self.conn()
        try:
            self.assertEqual(db.get_item(conn, f1).closed_reason, "merge")
            self.assertIsNone(db.get_item(conn, old_done), "closed before the merge: swept")
        finally:
            conn.close()
        self.assertEqual(ledger.reconcile(self.repo), [], "already closed")

    def test_posttool_hook_reacts_only_to_gh_pr_merge(self):
        f1, branch = self._feature_with_branch()
        self.git("merge", "-q", "--no-ff", branch, "-m", "merge")
        self.assertEqual(ledger.hook_posttool({"tool_input": {"command": "git status"},
                                               "cwd": str(self.repo)}), [])
        lines = ledger.hook_posttool({"tool_input": {"command": f"gh pr merge {branch} --merge"},
                                      "cwd": str(self.repo)})
        self.assertTrue(lines and lines[0].startswith("merged: closed F1"))

    def test_on_merge_with_a_branch_name(self):
        f1, branch = self._feature_with_branch()
        lines = ledger.on_merge(self.repo, branch)
        self.assertTrue(lines[0].startswith(f"merged: closed {f1}"))


if __name__ == "__main__":
    unittest.main()
