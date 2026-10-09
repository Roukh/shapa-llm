"""Tests for shapa.githooks (J26): deciding whether and how to link this
repo's git triggers without ever writing into a shared or hook-manager-owned
hooks dir, and without clobbering a foreign hand-written hook."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shapa import githooks


def _git(cwd, *args, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


class GitHooksCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hooks_dir(self) -> Path:
        return self.repo / ".git" / "hooks"


class TestLocalRepo(GitHooksCase):
    def test_missing_hooks_are_written(self):
        out = githooks.install(self.repo)
        self.assertEqual((self.hooks_dir() / "pre-commit").read_text(encoding="utf-8"),
                          githooks.GIT_HOOKS["pre-commit"])
        self.assertEqual((self.hooks_dir() / "post-commit").read_text(encoding="utf-8"),
                          githooks.GIT_HOOKS["post-commit"])
        self.assertEqual(len(out), 2)
        self.assertTrue(os.access(self.hooks_dir() / "pre-commit", os.X_OK))

    def test_foreign_hook_is_untouched_and_lines_are_printed(self):
        hooks = self.hooks_dir()
        hooks.mkdir(parents=True, exist_ok=True)
        foreign = "#!/bin/sh\necho mine\n"
        (hooks / "post-commit").write_text(foreign, encoding="utf-8")
        out = githooks.install(self.repo)
        self.assertEqual((hooks / "post-commit").read_text(encoding="utf-8"), foreign)
        self.assertTrue(any("is not shapa's" in line for line in out))
        self.assertTrue((hooks / "pre-commit").is_file())  # the other hook still gets written

    def test_idempotent_rerun_rewrites_only_shapas_own(self):
        githooks.install(self.repo)
        again = githooks.install(self.repo)
        self.assertEqual(len(again), 2)
        self.assertEqual((self.hooks_dir() / "pre-commit").read_text(encoding="utf-8"),
                          githooks.GIT_HOOKS["pre-commit"])

    def test_foreign_hook_already_wired_is_reported_not_rewritten(self):
        hooks = self.hooks_dir()
        hooks.mkdir(parents=True, exist_ok=True)
        text = "#!/bin/sh\nshapa ledger on-commit\n"
        (hooks / "post-commit").write_text(text, encoding="utf-8")
        out = githooks.install(self.repo)
        self.assertEqual((hooks / "post-commit").read_text(encoding="utf-8"), text)
        self.assertTrue(any("already wired" in line for line in out))


class TestLocalHooksPath(GitHooksCase):
    def test_local_hookspath_inside_the_repo_is_written(self):
        custom = self.repo / "tools" / "hooks"
        custom.mkdir(parents=True)
        _git(self.repo, "config", "core.hooksPath", str(custom))
        out = githooks.install(self.repo)
        self.assertTrue((custom / "pre-commit").is_file())
        self.assertFalse(any("shared" in line for line in out))

        st = githooks.state(self.repo)
        self.assertFalse(st["shared"])
        self.assertEqual(st["hooks_path_scope"], "local")


class TestSharedGlobalHooksPath(GitHooksCase):
    def setUp(self):
        super().setUp()
        self.shared = self.tmp / "shared-hooks"
        self.shared.mkdir()
        self.global_cfg = self.tmp / "global-gitconfig"
        self.global_cfg.write_text(
            "[user]\n\tname = Test\n\temail = test@example.com\n"
            f"[core]\n\thooksPath = {self.shared}\n", encoding="utf-8",
        )
        self.env = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(self.global_cfg), "GIT_CONFIG_NOSYSTEM": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_shared_global_hookspath_is_never_written_but_reported(self):
        st = githooks.state(self.repo)
        self.assertTrue(st["shared"])
        self.assertEqual(st["hooks_path_scope"], "global")

        out = githooks.install(self.repo)
        self.assertFalse((self.shared / "pre-commit").exists())
        self.assertFalse((self.shared / "post-commit").exists())
        self.assertTrue(any("shared hooks dir" in line for line in out))

    def test_shared_global_hookspath_already_wired_is_reported_as_wired(self):
        (self.shared / "post-commit").write_text(
            "#!/bin/sh\nshapa ledger on-commit\n", encoding="utf-8")
        out = githooks.install(self.repo)
        self.assertNotIn(githooks.GIT_HOOK_MARK,
                          (self.shared / "post-commit").read_text(encoding="utf-8"))
        self.assertTrue(any("already wired" in line and "scope global" in line for line in out))


class TestManagers(GitHooksCase):
    def test_husky_detected_nothing_written(self):
        (self.repo / ".husky").mkdir()
        _git(self.repo, "config", "core.hooksPath", ".husky")
        out = githooks.install(self.repo)
        self.assertFalse((self.repo / ".husky" / "pre-commit").exists())
        self.assertTrue(any("husky" in line for line in out))

    def test_lefthook_detected_nothing_written(self):
        (self.repo / "lefthook.yml").write_text("pre-commit:\n  commands: {}\n", encoding="utf-8")
        out = githooks.install(self.repo)
        self.assertFalse((self.hooks_dir() / "pre-commit").exists())
        self.assertTrue(any("lefthook" in line for line in out))

    def test_pre_commit_framework_detected_nothing_written(self):
        (self.repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
        out = githooks.install(self.repo)
        self.assertFalse((self.hooks_dir() / "pre-commit").exists())
        self.assertTrue(any("pre-commit framework" in line for line in out))


class TestState(GitHooksCase):
    def test_state_reports_missing_hooks(self):
        st = githooks.state(self.repo)
        self.assertFalse(st["hooks"]["pre-commit"]["exists"])
        self.assertFalse(st["shared"])
        self.assertIsNone(st["manager"])

    def test_state_detects_wired_hand_written_hook(self):
        hooks = self.hooks_dir()
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nshapa ledger pre-commit\n", encoding="utf-8")
        st = githooks.state(self.repo)
        self.assertTrue(st["hooks"]["pre-commit"]["wired"])
        self.assertFalse(st["hooks"]["pre-commit"]["ours"])


if __name__ == "__main__":
    unittest.main()
