"""Tests for `shapa doctor`/`status` as a whole-install health check (J36):
the global wiki, this repo's git hooks, and whether any agent harness
(Claude Code, Codex, OpenCode) is actually wired to shapa - on top of the
wiki format/malformed-log checks doctor already had.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from shapa import cli, config, status


def _git(cwd, *args, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


class DoctorCase(unittest.TestCase):
    """A scratch HOME (so ~/.claude.json, ~/.codex, ~/.config/opencode and
    the global wiki pointer never leak from this machine) plus a real
    throwaway git repo."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.claude_config_dir = self.home / "claude-config"
        self.codex_home = self.home / "codex-home"
        self.xdg_config_home = self.home / "xdg-config"
        self.cfg = self.home / "shapa-config.json"
        self.default_dir = self.home / "shapa-default-memory"

        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.object(config, "DEFAULT_DIR", self.default_dir),
            mock.patch.object(Path, "home", staticmethod(lambda: self.home)),
            mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": str(self.claude_config_dir),
                "CODEX_HOME": str(self.codex_home),
                "XDG_CONFIG_HOME": str(self.xdg_config_home),
            }, clear=False),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("hi\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        r = _git(self.repo, "commit", "-q", "-m", "initial")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cwd(self, path: Path):
        return mock.patch.object(config.Path, "cwd", staticmethod(lambda: path))

    # --- fixture builders ----------------------------------------------------

    def create_global_wiki(self) -> Path:
        with self.cwd(self.tmp):
            cli._init(["--global", str(self.default_dir)])
        return self.default_dir

    def create_repo_wiki(self) -> Path:
        """Via `shapa init` - also wires this repo's git hooks."""
        with self.cwd(self.repo):
            cli._init([])
        return self.repo / ".shapa"

    def create_repo_wiki_without_hooks(self) -> Path:
        wiki = self.repo / ".shapa"
        wiki.mkdir()
        (wiki / "AGENTS.md").write_text("# marker\n", encoding="utf-8")
        return wiki

    def fake_shapa(self) -> Path:
        fake = self.tmp / "fake-shapa"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        return fake

    def wire_claude_hook(self, command: str) -> None:
        self.claude_config_dir.mkdir(parents=True, exist_ok=True)
        settings = {"hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": 60}]}]}}
        (self.claude_config_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    def run_doctor(self, *, json_out: bool = False):
        args = ["--cwd", str(self.repo)] + (["--json"] if json_out else [])
        out = io.StringIO()
        with self.cwd(self.repo), redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                status.main(args, doctor=True)
        return ctx.exception.code, out.getvalue()


class TestBaselineAndHappyPath(DoctorCase):
    def test_scratch_repo_no_global_wiki_no_harness_exits_1_listing_both(self):
        # Nothing set up at all: no global wiki, no repo wiki, no harness.
        code, out = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("no global wiki", out)
        self.assertIn("no agent harness is connected", out)
        # The missing repo wiki itself is informational, not a third problem.
        self.assertIn("no repo wiki here", out)

    def test_fully_wired_exits_0(self):
        self.create_global_wiki()
        self.create_repo_wiki()
        self.wire_claude_hook(f"{self.fake_shapa()} bootstrap")
        code, out = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertIn("problems: none", out)


class TestGitHooksProblem(DoctorCase):
    def test_hooks_missing_is_a_problem(self):
        self.create_global_wiki()
        self.create_repo_wiki_without_hooks()
        self.wire_claude_hook(f"{self.fake_shapa()} bootstrap")
        code, out = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("hook not wired", out)
        self.assertIn("shapa ledger git-hooks", out)


class TestBrokenHarness(DoctorCase):
    def test_broken_harness_command_path_is_a_problem(self):
        self.create_global_wiki()
        self.create_repo_wiki()
        self.wire_claude_hook("/nonexistent/path/to/shapa bootstrap")
        code, out = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("does not resolve", out)
        self.assertIn("re-run the installer", out)


class TestJsonShape(DoctorCase):
    def test_json_report_has_the_new_sections(self):
        self.create_global_wiki()
        self.create_repo_wiki()
        self.wire_claude_hook(f"{self.fake_shapa()} bootstrap")
        code, out = self.run_doctor(json_out=True)
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertIn("problems", report)
        self.assertEqual(report["problems"], [])
        self.assertIn("harness", report)
        self.assertTrue(report["harness"]["connected"])
        self.assertTrue(report["harness"]["claude_hooks"])
        self.assertIn("git_hooks", report)
        self.assertTrue(report["git_hooks"]["hooks"]["pre-commit"]["wired"])
        self.assertTrue(report["git_hooks"]["hooks"]["post-commit"]["wired"])
        self.assertIn("repo_wiki_missing_here", report)
        self.assertFalse(report["repo_wiki_missing_here"])

    def test_status_never_exits_1_even_when_nothing_is_set_up(self):
        out = io.StringIO()
        with self.cwd(self.repo), redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                status.main(["--cwd", str(self.repo)], doctor=False)
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
