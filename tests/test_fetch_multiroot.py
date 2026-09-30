"""Tests for shapa.fetch's multi-root read path (select_multi/fetch_context).

A prompt's relevant memory is not one directory: select_multi() fans out
across every wiki in scope (repo/external/global), guarantees each root's
own locus:meta anchors and a proportional-floor share of the relevance-
ranked fill (so no root's share collapses to zero as root count grows),
and applies a percentile-relative confidence floor so an off-topic prompt
surfaces anchors only - never padded, low-confidence filler. See
shapa-backend-spec.md §4.1 and Slice 2's acceptance criteria (§8).

BM25 is forced throughout (embed.available() patched False) so these tests
are deterministic and fast regardless of whether the [semantic] extra
happens to be installed in the environment running them.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shapa import config, fetch
from shapa.config import WikiRoot


def _note(d: Path, nid: str, body: str, locus: str = "output", consequence: int = 5) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{nid}.md").write_text(
        "---\n"
        f"id: {nid}\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
        f"consequence: {consequence}\nlocus: {locus}\nuses: 0\n---\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _make_repo_wiki(repo: Path) -> Path:
    """A repo-local wiki config.discover() will actually find: the
    AGENTS.md marker file, written as a valid node so rglob("*.md") over
    the wiki dir doesn't trip frontmatter validation on it."""
    wiki = repo / ".shapa"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / config.WIKI_MARKER).write_text(
        "---\nid: AGENTS\ntype: reference\ncreated: \"2026-01-01T00:00:00Z\"\n"
        "consequence: 8\nlocus: output\nuses: 0\n---\n# rules\n",
        encoding="utf-8",
    )
    return wiki


class FetchMultirootTestCase(unittest.TestCase):
    """Shared harness: isolated tmp dir, isolated config pointer, BM25 forced
    (no network/model dependency), exactly like tests/test_config_multiroot.py."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(fetch.embed, "available", return_value=False),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAnchorsPresentPerRoot(FetchMultirootTestCase):
    """Each root's own locus:meta anchor surfaces regardless of the prompt,
    and regardless of what the *other* root contains."""

    def test_global_and_repo_anchors_both_present(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-meta", "standing rule about deploy safety", locus="meta", consequence=9)
        _note(global_wiki, "g-filler", "unrelated filler note about lunch")

        repo = _make_git_repo(self.tmp / "repoA")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "r-meta", "standing rule about test discipline", locus="meta", consequence=9)
        _note(repo_wiki, "r-filler", "unrelated filler note about weather")

        selection = fetch.select_multi("database migration rollback safety", start=repo)
        ids = [n.id for n, _ in selection.items]
        self.assertIn("g-meta", ids)
        self.assertIn("r-meta", ids)

    def test_low_value_root_anchor_survives_a_high_value_root(self):
        # The old single-root code capped anchors at 2 GLOBALLY (sorted by
        # value) - a root whose own meta note is weak would lose its anchor
        # slot entirely to another root's stronger ones. The per-root cap
        # (2 EACH) must guarantee the weak root's own anchor regardless.
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-weak-meta", "a weak global rule", locus="meta", consequence=2)

        repo = _make_git_repo(self.tmp / "repoB")
        repo_wiki = _make_repo_wiki(repo)
        for i in range(5):
            _note(repo_wiki, f"r-strong-meta-{i}", f"strong repo rule {i}", locus="meta", consequence=10)

        # k == exactly (2 per-root cap) + 1 == 3: a global cap-of-2 sorted
        # by value across both roots would fill entirely from the repo's
        # 5 strong metas and starve the weak global anchor. The per-root
        # cap must reserve exactly repo's top 2 + global's 1 instead.
        selection = fetch.select_multi("", start=repo, k=3)
        ids = {n.id for n, _ in selection.items}
        self.assertIn("g-weak-meta", ids)
        self.assertEqual(ids, {"g-weak-meta", "r-strong-meta-0", "r-strong-meta-1"})


class TestRootShareNeverStarves(FetchMultirootTestCase):
    """Proportional-floor budget: as root count grows (2, 3, 5+), every
    root's own relevant note still shows up - never squeezed to zero by a
    fixed total budget or another root's larger share."""

    def _roots_with_relevant_note(self, n: int) -> list[WikiRoot]:
        roots = []
        for i in range(n):
            d = self.tmp / f"root-{i}"
            _note(d, f"relevant-{i}", "database migration rollback safety procedure", consequence=5)
            for j in range(3):
                _note(d, f"decoy-{i}-{j}", f"unrelated decoy content number {j} about gardening")
            # Real WikiRoot kinds only take 3 literal values; a synthetic
            # scale test beyond 3 roots necessarily reuses one - the merge
            # must stay correct even so (keyed by WikiRoot identity, not
            # just .kind - see fetch.select_multi).
            kind = ("repo", "external", "global")[i % 3]
            roots.append(WikiRoot(path=d, kind=kind, repo=f"repo-{i}"))
        return roots

    def _assert_every_root_represented(self, n: int) -> None:
        roots = self._roots_with_relevant_note(n)
        selection = fetch.select_multi(
            "database migration rollback safety procedure",
            roots=roots, k=50, budget=1000,  # budget << n * ROOT_FLOOR_CHARS for n>=3
        )
        ids = {n_.id for n_, _ in selection.items}
        for i in range(n):
            self.assertIn(f"relevant-{i}", ids, f"root {i}'s own note missing at n={n}")

    def test_two_roots(self):
        self._assert_every_root_represented(2)

    def test_three_roots(self):
        self._assert_every_root_represented(3)

    def test_five_roots(self):
        self._assert_every_root_represented(5)


class TestConfidenceFloorNoPaddedFiller(FetchMultirootTestCase):
    """An off-topic prompt (no term overlap anywhere) gets anchors only,
    plus the explicit no-match marker - never a confident-looking but
    wrong note padded in to fill k."""

    def _two_root_fixture(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-meta", "standing rule about deploy safety", locus="meta", consequence=9)
        _note(global_wiki, "g-topic", "notes about database migration rollback procedures")

        repo = _make_git_repo(self.tmp / "repoC")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "r-meta", "standing rule about test discipline", locus="meta", consequence=9)
        _note(repo_wiki, "r-topic", "notes about git worktree branch management")
        return repo

    def test_off_topic_query_is_anchors_only_plus_no_match(self):
        repo = self._two_root_fixture()
        selection = fetch.select_multi("xylophone kumquat zephyr", start=repo)
        ids = {n.id for n, _ in selection.items}
        self.assertEqual(ids, {"g-meta", "r-meta"})
        self.assertTrue(selection.no_match)
        self.assertEqual(selection.collisions, [])

    def test_off_topic_query_marker_in_context_block(self):
        repo = self._two_root_fixture()
        block = fetch.fetch_context("xylophone kumquat zephyr", start=repo, record=False)
        self.assertIn("<!-- no query-relevant notes found -->", block)
        self.assertIn("g-meta", block)
        self.assertIn("r-meta", block)
        self.assertNotIn("g-topic", block)
        self.assertNotIn("r-topic", block)

    def test_on_topic_query_has_no_marker(self):
        repo = self._two_root_fixture()
        selection = fetch.select_multi("database migration rollback procedures", start=repo)
        self.assertFalse(selection.no_match)
        ids = {n.id for n, _ in selection.items}
        self.assertIn("g-topic", ids)

    def test_empty_query_is_value_fallback_not_no_match(self):
        # No prompt text at all is not "off-topic" - it's simply no
        # relevance ranking requested; pure value ranking, no marker.
        repo = self._two_root_fixture()
        selection = fetch.select_multi("", start=repo)
        self.assertFalse(selection.no_match)


class TestCrossRootCollisionShowsBoth(FetchMultirootTestCase):
    """A note id that exists in more than one root is never silently
    picked - both versions surface, and the collision is reported."""

    def test_duplicate_id_both_included_and_flagged(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "shared-id", "database migration rollback safety - global version")

        repo = _make_git_repo(self.tmp / "repoD")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "shared-id", "database migration rollback safety - repo version")

        selection = fetch.select_multi("database migration rollback safety", start=repo, k=10)
        self.assertEqual(selection.collisions, ["shared-id"])
        matching = [n for n, _ in selection.items if n.id == "shared-id"]
        self.assertEqual(len(matching), 2)
        bodies = {snip for n, snip in selection.items if n.id == "shared-id"}
        self.assertEqual(len(bodies), 2)

    def test_collision_note_in_context_block(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "shared-id", "database migration rollback safety - global version")

        repo = _make_git_repo(self.tmp / "repoE")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "shared-id", "database migration rollback safety - repo version")

        block = fetch.fetch_context("database migration rollback safety", start=repo, record=False)
        self.assertIn("exists in more than one wiki root", block)
        self.assertIn("shared-id", block)


class TestSelectRootParamUnaffected(FetchMultirootTestCase):
    """select(query, root=...) (explicit single directory) is completely
    untouched by the multi-root merge - back-compat for every existing
    single-root caller."""

    def test_explicit_root_bypasses_multiroot_merge(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-note", "should not be reachable via explicit root")

        repo = _make_git_repo(self.tmp / "repoF")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "r-note", "explicit single root only")

        selected = fetch.select("single root", root=repo_wiki)
        ids = [n.id for n, _ in selected]
        self.assertIn("r-note", ids)
        self.assertNotIn("g-note", ids)  # the global wiki must never be reached


if __name__ == "__main__":
    unittest.main()
