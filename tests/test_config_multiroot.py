"""Tests for shapa.config.wiki_roots(): multi-root read resolution.

A read fans out across every wiki in scope - the repo-local wiki (if any),
an external/<repo>/ bucket (if the repo has no wiki of its own), and always
the global wiki - most-specific first, deduped, missing directories
tolerated. See shapa-backend-spec.md §4.1.
"""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from shapa import config, validate
from shapa.config import WikiRoot


def _make_wiki(root: Path) -> Path:
    """Create a minimal valid wiki (``.shapa/AGENTS.md`` + ``agenda.md``)
    under *root*.

    The marker file itself carries real node frontmatter (as the shipped
    ``shapa/assets/AGENTS.md`` does: ``id: AGENTS, type: reference``) so a
    ``rglob("*.md")`` over the wiki dir - which ``validate --all-roots`` and
    ``nodes.load_nodes`` both do - doesn't trip F01-F03 on it. ``agenda.md``
    is included too (F11 - spec §10 decision 6 requires one at every wiki
    root), so a "minimal valid wiki" stays valid under the lean-shape check,
    not just the frontmatter one.
    """
    wiki = root / ".shapa"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / config.WIKI_MARKER).write_text(
        "---\nid: AGENTS\ntype: reference\ncreated: \"2026-01-01T00:00:00Z\"\n"
        "consequence: 8\nlocus: output\nuses: 0\n---\n# rules\n",
        encoding="utf-8",
    )
    (wiki / "agenda.md").write_text(
        "---\nid: agenda\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
        "consequence: 5\nlocus: meta\nuses: 0\n---\n# Agenda\n\n1. one fire\n",
        encoding="utf-8",
    )
    return wiki


def _make_git_repo(path: Path) -> Path:
    """A directory that *looks* like a git checkout - just needs a ``.git``
    entry, walked by config._git_toplevel; no real git init needed."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _note(d: Path, nid: str, body: str = "body text") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{nid}.md").write_text(
        "---\n"
        f"id: {nid}\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
        "consequence: 5\nlocus: output\nuses: 0\n---\n"
        f"{body}\n",
        encoding="utf-8",
    )


class MultirootTestCase(unittest.TestCase):
    """Shared harness: isolates $SHAPA_MEMORY and the ~/.shapa/config.json
    pointer, exactly like tests/test_discover.py."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
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


class TestWikiRootsOrderAndKind(MultirootTestCase):
    def test_repo_plus_global_order_and_kind(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)

        repo = _make_git_repo(self.tmp / "repoA")
        repo_wiki = _make_wiki(repo)

        roots = config.wiki_roots(repo)
        self.assertEqual([r.kind for r in roots], ["repo", "global"])
        self.assertEqual(roots[0].path, repo_wiki.resolve())
        self.assertEqual(roots[0].repo, "repoA")
        self.assertEqual(roots[1].path, config.global_root())

    def test_external_bucket_used_when_repo_has_no_own_wiki(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        external = global_wiki / "external" / "repoB"
        _note(external, "ext-note")

        repo = _make_git_repo(self.tmp / "repoB")

        roots = config.wiki_roots(repo)
        self.assertEqual([r.kind for r in roots], ["external", "global"])
        self.assertEqual(roots[0].path, external.resolve())
        self.assertEqual(roots[0].repo, "repoB")

    def test_repo_with_no_wiki_and_no_external_bucket_is_global_only(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        repo = _make_git_repo(self.tmp / "repoC")

        roots = config.wiki_roots(repo)
        self.assertEqual([r.kind for r in roots], ["global"])
        self.assertEqual(roots[0].path, config.global_root())

    def test_non_git_dir_with_no_wiki_is_global_only(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        plain = self.tmp / "just-a-dir"
        plain.mkdir()

        roots = config.wiki_roots(plain)
        self.assertEqual([r.kind for r in roots], ["global"])


class TestWikiRootsDedup(MultirootTestCase):
    def test_repo_wiki_that_is_the_global_wiki_dedups_to_one_entry(self):
        # A repo checked out AT the global wiki's own directory: discover()
        # finds the same physical path global_root() already names, so it
        # must appear exactly once, not twice.
        global_wiki = _make_git_repo(self.tmp / "global-repo")
        wiki = _make_wiki(global_wiki)
        config.set_memory_dir(wiki)

        roots = config.wiki_roots(global_wiki)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].kind, "global")
        self.assertEqual(roots[0].path, wiki.resolve())


class TestWikiRootsMissingGlobal(MultirootTestCase):
    def test_no_crash_when_global_dir_does_not_exist(self):
        # Nothing ever pointed the config at a global wiki, and the default
        # ~/.shapa/memory doesn't exist on this machine either (patched away)
        # - wiki_roots() must still return a (non-crashing) global entry.
        with mock.patch.object(config, "DEFAULT_DIR", self.tmp / "nonexistent" / "memory"):
            repo = _make_git_repo(self.tmp / "repoD")
            roots = config.wiki_roots(repo)
            self.assertEqual([r.kind for r in roots], ["global"])
            self.assertFalse(roots[0].path.exists())

    def test_no_crash_when_repo_wiki_exists_but_global_missing(self):
        with mock.patch.object(config, "DEFAULT_DIR", self.tmp / "nonexistent" / "memory"):
            repo = _make_git_repo(self.tmp / "repoE")
            _make_wiki(repo)
            roots = config.wiki_roots(repo)
            self.assertEqual([r.kind for r in roots], ["repo", "global"])
            self.assertFalse(roots[1].path.exists())


class TestWikiRootsEnvOverride(MultirootTestCase):
    def test_env_var_returns_single_global_root(self):
        override = self.tmp / "override"
        repo = _make_git_repo(self.tmp / "repoF")
        _make_wiki(repo)
        with mock.patch.dict(os.environ, {config.ENV_VAR: str(override)}):
            roots = config.wiki_roots(repo)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0], WikiRoot(path=override, kind="global"))


class TestCrossRootDuplicateId(MultirootTestCase):
    """F09: the same note id in two roots is an error, not a silent pick."""

    def test_duplicate_id_across_roots_is_flagged(self):
        global_wiki = self.tmp / "global"
        _note(global_wiki, "shared-id", "global body")

        repo = _make_git_repo(self.tmp / "repoG")
        repo_wiki = _make_wiki(repo)
        _note(repo_wiki, "shared-id", "repo body")
        config.set_memory_dir(global_wiki)

        roots = config.wiki_roots(repo)
        violations = validate.check_cross_root_duplicates(roots)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "F09")
        self.assertEqual(violations[0].severity, "error")
        self.assertIn("shared-id", violations[0].message)

    def test_no_duplicate_ids_is_clean(self):
        global_wiki = self.tmp / "global"
        _note(global_wiki, "global-only")

        repo = _make_git_repo(self.tmp / "repoH")
        repo_wiki = _make_wiki(repo)
        _note(repo_wiki, "repo-only")
        config.set_memory_dir(global_wiki)

        roots = config.wiki_roots(repo)
        self.assertEqual(validate.check_cross_root_duplicates(roots), [])

    def test_all_roots_cli_exits_nonzero_on_duplicate(self):
        global_wiki = self.tmp / "global"
        _note(global_wiki, "dup-cli")

        repo = _make_git_repo(self.tmp / "repoI")
        repo_wiki = _make_wiki(repo)
        _note(repo_wiki, "dup-cli")
        config.set_memory_dir(global_wiki)

        with self.assertRaises(SystemExit) as cm:
            validate.main(["--all-roots", str(repo)])
        self.assertNotEqual(cm.exception.code, 0)

    def test_structural_ids_never_flagged_as_f09(self):
        # Every wiki `shapa init` scaffolds carries the same AGENTS/
        # placement/agenda ids by construction (and, if `shapa init` filled
        # arch/ too, PRD/architecture/system-design) - that recurrence is
        # not the ambiguity F09 exists to catch.
        global_wiki = self.tmp / "global"
        _note(global_wiki, "AGENTS", "schema doc")
        _note(global_wiki, "agenda", "1. one fire\n")

        repo = _make_git_repo(self.tmp / "repoK")
        repo_wiki = _make_wiki(repo)
        config.set_memory_dir(global_wiki)

        roots = config.wiki_roots(repo)
        violations = validate.check_cross_root_duplicates(roots)
        self.assertEqual(violations, [])

    def test_every_convention_file_is_exempt_from_f09(self):
        # Every wiki carries its own agenda.md and ideas.md by convention,
        # plus the shipped AGENTS.md/placement.md - none of them is F09,
        # while a genuinely shared note id still is.
        global_wiki = self.tmp / "global"
        repo = _make_git_repo(self.tmp / "repoL")
        repo_wiki = _make_wiki(repo)
        for wiki in (global_wiki, repo_wiki):
            for nid in ("AGENTS", "placement", "agenda", "ideas", "really-shared"):
                _note(wiki, nid)
        config.set_memory_dir(global_wiki)

        violations = validate.check_cross_root_duplicates(config.wiki_roots(repo))
        self.assertEqual([v.rule for v in violations], ["F09"])
        self.assertIn("really-shared", violations[0].message)

    def test_all_roots_cli_exits_zero_with_an_ideas_log_in_every_root(self):
        global_wiki = self.tmp / "global"
        _note(global_wiki, "agenda", "1. one fire\n")
        _note(global_wiki, "ideas", "2026-10-01 a global idea")

        repo = _make_git_repo(self.tmp / "repoM")
        repo_wiki = _make_wiki(repo)
        _note(repo_wiki, "ideas", "2026-10-01 a repo idea")
        config.set_memory_dir(global_wiki)

        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            validate.main(["--all-roots", str(repo)])
        self.assertEqual(cm.exception.code, 0)

    def test_all_roots_cli_exits_zero_when_clean(self):
        global_wiki = self.tmp / "global"
        _note(global_wiki, "clean-global")
        _note(global_wiki, "agenda", "1. one fire\n")

        repo = _make_git_repo(self.tmp / "repoJ")
        repo_wiki = _make_wiki(repo)
        _note(repo_wiki, "clean-repo")
        config.set_memory_dir(global_wiki)

        with self.assertRaises(SystemExit) as cm:
            validate.main(["--all-roots", str(repo)])
        self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
