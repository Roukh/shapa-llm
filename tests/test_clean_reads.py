"""Acceptance test for GAP E (shapa-backend-spec.md §10 decision 7, "reads
never write notes"): bootstrap, fetch, score, and the MCP `search` tool are
all read paths. Running every one of them against a real git repo fixture
must leave the checkout exactly as clean as it started - no counter-only
frontmatter diffs, no stray index-db file left untracked.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shapa import bootstrap, cli, config, fetch, mcp, score, store


def _git(*args: str, cwd: Path) -> None:
    # This machine's global git hooks enforce an identity allowlist (see
    # .githooks/pre-commit) even for a brand-new throwaway repo - use an
    # address it accepts, not a made-up one.
    subprocess.run(
        ["git", "-c", "user.email=admin@ghobz.com", "-c", "user.name=shapa-test", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _note(d: Path, nid: str, *, consequence: int = 5, locus: str = "output",
          body: str = "a note about keeping the git workflow disciplined") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{nid}.md").write_text(
        "---\n"
        f"id: {nid}\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
        f"consequence: {consequence}\nlocus: {locus}\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )


@unittest.skipUnless(shutil.which("git"), "git not on PATH")
class TestReadsNeverWriteNotes(unittest.TestCase):
    """GAP E acceptance: bootstrap + fetch + score + MCP search against a
    real git repo, then `git status --porcelain` must still be empty."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "shapa-config.json"
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git("init", "-q", cwd=self.repo)

        # Scaffold the wiki the way a real operator would: `shapa init`
        # (exercises the .gitignore-for-the-index-cache fix too).
        self.wiki = self.repo / ".shapa"
        cli.main(["init", str(self.wiki)])

        _note(self.wiki, "r-meta", consequence=9, locus="meta",
              body="a standing rule about keeping the git workflow disciplined")
        _note(self.wiki, "r-out", consequence=5, locus="output",
              body="a routine memory: day to day habits around the git workflow")

        _git("add", "-A", cwd=self.repo)
        _git("commit", "-q", "-m", "seed wiki", cwd=self.repo)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _porcelain(self) -> str:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.repo),
                            check=True, capture_output=True, text=True)
        return r.stdout

    def test_bootstrap_fetch_score_and_mcp_search_leave_git_clean(self):
        self.assertEqual(self._porcelain(), "", "fixture must start clean")

        # Tier 1: SessionStart.
        text = bootstrap.build_context(start=str(self.repo))
        self.assertIn("r-meta", text)

        # Tier 2: the UserPromptSubmit hook, record=True (the live default) -
        # this is exactly the path that used to rewrite uses/last_used into
        # the note file on every prompt.
        block = fetch.fetch_context("git workflow", root=self.wiki, record=True)
        self.assertIn("r-meta", block)

        # `shapa score` (the CLI path, root-threaded from the directory arg).
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            score.main([str(self.wiki)])
        self.assertIn("r-meta", out.getvalue())

        # Tier 3: the MCP `search` tool (read_only=True already, Slice 6).
        result = mcp.tool_search({"query": "git workflow"}, cwd=str(self.repo))
        self.assertTrue(result["results"])

        # The read path DID record real uses - in the index store, never in
        # the note files (decision 7): fetch_context alone recorded a use
        # of each surfaced note.
        uses_out, last_used_out = store.get_use(self.wiki, "r-out")
        self.assertGreaterEqual(uses_out, 1)
        self.assertIsNotNone(last_used_out)
        uses_meta, _ = store.get_use(self.wiki, "r-meta")
        self.assertGreaterEqual(uses_meta, 1)

        # And score.py picked that same live count back up (GAP E).
        self.assertGreaterEqual(score.score_node(self.wiki / "r-out.md", root=self.wiki).uses, 1)

        # The acceptance criterion itself: a real git checkout, after every
        # read path has run against it, is exactly as clean as it started.
        self.assertEqual(self._porcelain(), "")

    def test_repeated_reads_stay_clean_too(self):
        # Not just the first read after seeding - a warm/re-synced index
        # (sync() is called again on every record_use) must not dirty the
        # tree on a second pass either.
        fetch.fetch_context("git workflow", root=self.wiki, record=True)
        fetch.fetch_context("git workflow", root=self.wiki, record=True)
        bootstrap.build_context(start=str(self.repo))
        mcp.tool_search({"query": "git workflow"}, cwd=str(self.repo))
        self.assertEqual(self._porcelain(), "")


if __name__ == "__main__":
    unittest.main()
