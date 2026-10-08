"""End-to-end tests for `shapa init`'s git-aware default target (J25):
resolving the git top level - the PRIMARY checkout's, from a linked
worktree - creating `.shapa/` there, respecting a legacy `shapa/` wiki
already there, and leaving a clean `git status --porcelain` footprint.

tests/test_cli.py covers the DIR / --global / --upgrade-docs cases this
doesn't touch (those never needed a real git checkout). tests/test_githooks.py
covers the git-hook linking behavior `_install_hooks_quietly` calls into.
"""

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from shapa import cli, config


def _git(cwd, *args, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


class GitRepoCase(unittest.TestCase):
    """A real throwaway git repo, with the global pointer isolated and the
    cwd pinned the way an interactive `shapa init` sees it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        r = _git(self.repo, "commit", "-q", "-m", "initial")
        self.assertEqual(r.returncode, 0, r.stderr)

        self.cfg = self.tmp / "config.json"
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cwd(self, path: Path):
        """Context manager: pin `Path.cwd()` to *path* for the block (the
        same class object cli.py's bare `Path.cwd()` resolves through)."""
        return mock.patch.object(config.Path, "cwd", staticmethod(lambda: path))


class TestBareInitTopLevel(GitRepoCase):
    def test_subdir_creates_dot_shapa_at_top_level(self):
        sub = self.repo / "sub" / "dir"
        sub.mkdir(parents=True)
        with self.cwd(sub):
            cli._init([])
        self.assertTrue((self.repo / ".shapa" / "AGENTS.md").is_file())
        self.assertFalse((sub / ".shapa").exists())
        self.assertFalse((self.repo / "sub" / ".shapa").exists())

    def test_outside_a_git_work_tree_exits_2(self):
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        with self.cwd(outside), self.assertRaises(SystemExit) as ctx, \
                redirect_stderr(io.StringIO()):
            cli._init([])
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse((outside / ".shapa").exists())

    def test_no_git_scaffolds_in_cwd_even_outside_a_repo(self):
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        with self.cwd(outside):
            cli._init(["--no-git"])
        self.assertTrue((outside / ".shapa" / "AGENTS.md").is_file())

    def test_no_obsidian_by_default_present_with_flag(self):
        with self.cwd(self.repo):
            cli._init([])
        self.assertFalse((self.repo / ".shapa" / ".obsidian").exists())
        with self.cwd(self.repo):
            cli._init(["--obsidian"])
        self.assertTrue((self.repo / ".shapa" / ".obsidian" / "app.json").is_file())

    def test_idempotent_rerun_leaves_edits_alone(self):
        with self.cwd(self.repo):
            cli._init([])
        agents = self.repo / ".shapa" / "AGENTS.md"
        agents.write_text("EDITED", encoding="utf-8")
        out = io.StringIO()
        with self.cwd(self.repo), redirect_stdout(out):
            cli._init([])  # re-run
        self.assertEqual(agents.read_text(encoding="utf-8"), "EDITED")
        self.assertIn("already present", out.getvalue())

    def test_legacy_shapa_wiki_respected_no_second_wiki(self):
        legacy = self.repo / "shapa"
        legacy.mkdir()
        (legacy / "AGENTS.md").write_text("# legacy\n", encoding="utf-8")
        with self.cwd(self.repo):
            cli._init([])
        self.assertFalse((self.repo / ".shapa").exists())
        self.assertEqual((legacy / "AGENTS.md").read_text(encoding="utf-8"), "# legacy\n")

    def test_clean_footprint_outside_dot_shapa(self):
        with self.cwd(self.repo):
            cli._init([])
        r = _git(self.repo, "status", "--porcelain")
        # The new wiki directory itself is untracked (nobody has `git add`ed
        # it yet) - that single ".shapa/" line is expected. Nothing else -
        # no stray file outside it, no edit to a tracked file - should show.
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        for ln in lines:
            self.assertTrue(ln[3:].startswith(".shapa/"), ln)

    def test_output_mentions_next_steps_not_shapa_connect(self):
        out = io.StringIO()
        with self.cwd(self.repo), redirect_stdout(out):
            cli._init([])
        text = out.getvalue()
        self.assertIn("issue row", text)
        self.assertIn("J<n>:", text)
        self.assertNotIn("shapa connect", text)


class TestWorktreeInit(GitRepoCase):
    def test_from_a_linked_worktree_targets_the_primary_checkout(self):
        wt = self.tmp / "wt"
        r = _git(self.repo, "worktree", "add", "-q", "-b", "F1-x", str(wt))
        self.assertEqual(r.returncode, 0, r.stderr)
        with self.cwd(wt):
            cli._init([])
        self.assertTrue((self.repo / ".shapa" / "AGENTS.md").is_file())
        self.assertFalse((wt / ".shapa").exists())


if __name__ == "__main__":
    unittest.main()
