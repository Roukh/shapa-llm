"""Tests for cwd-based wiki discovery (the repo-root wiki model).

A wiki is a directory named ``shapa`` that contains the ``AGENTS.md`` marker.
``config.discover`` walks up from a starting directory (the cwd) to the nearest
ancestor holding such a wiki, and ``config.memory_dir`` prefers a discovered
local wiki over the persisted pointer/default so the Claude Code hooks target
whichever project the session runs in.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shapa import config


def _make_wiki(root: Path) -> Path:
    """Create a minimal valid wiki (``shapa/AGENTS.md``) under *root*."""
    wiki = root / config.WIKI_DIRNAME
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / config.WIKI_MARKER).write_text("# rules\n", encoding="utf-8")
    return wiki


class TestDiscover(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"
        self.patch = mock.patch.object(config, "CONFIG_FILE", self.cfg)
        self.patch.start()
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp)

    def test_discovers_wiki_from_repo_root(self):
        repo = self.tmp / "repo"
        wiki = _make_wiki(repo)
        self.assertEqual(config.discover(repo), wiki.resolve())

    def test_discovers_wiki_from_nested_subdir(self):
        repo = self.tmp / "repo"
        wiki = _make_wiki(repo)
        nested = repo / "src" / "deep"
        nested.mkdir(parents=True)
        self.assertEqual(config.discover(nested), wiki.resolve())

    def test_no_wiki_returns_none(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIsNone(config.discover(empty))

    def test_bare_shapa_dir_without_marker_is_not_a_wiki(self):
        # The shapa *package* directory is named ``shapa`` but has no AGENTS.md
        # marker, so discovery must not mistake it for a wiki.
        repo = self.tmp / "repo"
        (repo / config.WIKI_DIRNAME).mkdir(parents=True)
        (repo / config.WIKI_DIRNAME / "__init__.py").write_text("", encoding="utf-8")
        self.assertIsNone(config.discover(repo))

    def test_memory_dir_prefers_discovered_wiki_over_pointer(self):
        # A recorded pointer (the default/global wiki) is overridden by a local
        # repo-root wiki when the cwd is inside that repo.
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        repo = self.tmp / "repo"
        wiki = _make_wiki(repo)
        with mock.patch.object(config.Path, "cwd", staticmethod(lambda: repo)):
            self.assertEqual(config.memory_dir(), wiki.resolve())

    def test_memory_dir_falls_back_to_pointer_outside_any_wiki(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        outside = self.tmp / "outside"
        outside.mkdir()
        with mock.patch.object(config.Path, "cwd", staticmethod(lambda: outside)):
            self.assertEqual(config.memory_dir(), global_wiki.resolve())

    def test_env_var_overrides_discovery(self):
        repo = self.tmp / "repo"
        _make_wiki(repo)
        override = self.tmp / "override"
        with mock.patch.dict(os.environ, {config.ENV_VAR: str(override)}):
            with mock.patch.object(config.Path, "cwd", staticmethod(lambda: repo)):
                self.assertEqual(config.memory_dir(), override)


class TestInitPointerPolicy(unittest.TestCase):
    """A bare ``shapa init`` (repo-root wiki) must not hijack the recorded
    default; only an explicit ``shapa init DIR`` sets it."""

    def setUp(self):
        from shapa import cli
        self.cli = cli
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.object(config.Path, "cwd", staticmethod(lambda: self.tmp)),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def test_bare_init_does_not_write_pointer(self):
        self.cli._init([])  # scaffolds <cwd>/shapa
        self.assertTrue((self.tmp / config.WIKI_DIRNAME / config.WIKI_MARKER).is_file())
        self.assertFalse(self.cfg.exists())  # default pointer untouched

    def test_explicit_init_writes_pointer(self):
        wiki = self.tmp / "global"
        self.cli._init([str(wiki)])
        self.assertTrue(self.cfg.is_file())
        self.assertEqual(config._pointer(), wiki.resolve())

    def test_init_refuses_non_wiki_nonempty_target(self):
        # A pre-existing ./shapa holding unrelated files (e.g. a package dir)
        # must NOT be merged into - refuse rather than pollute it.
        pkg = self.tmp / config.WIKI_DIRNAME
        pkg.mkdir()
        (pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            self.cli._init([])  # bare init targets <cwd>/shapa
        self.assertNotEqual(cm.exception.code, 0)
        # Nothing was written into the unrelated directory.
        self.assertFalse((pkg / "AGENTS.md").exists())
        self.assertFalse((pkg / "arch").exists())

    def test_reinit_existing_wiki_is_allowed(self):
        # Re-running init on a real wiki (has the AGENTS.md marker) is idempotent.
        wiki = self.tmp / config.WIKI_DIRNAME
        wiki.mkdir()
        (wiki / config.WIKI_MARKER).write_text("# rules\n", encoding="utf-8")
        self.cli._init([])  # must not raise
        self.assertTrue((wiki / "arch" / "PRD.md").is_file())


if __name__ == "__main__":
    unittest.main()
