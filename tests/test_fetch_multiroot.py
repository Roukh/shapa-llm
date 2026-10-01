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
    """GAP F (2026-09-30): a ``locus: meta`` note is no longer an
    unconditional, query-independent reservation - it is just another
    candidate that earns its slot on relevance/value like everything else.
    ``shapa.bootstrap`` already surfaces every root's own anchors,
    unconditionally, once per session; per-prompt fetch re-showing the
    SAME notes on every prompt regardless of relevance was the bug (a
    fixed, query-independent slice of every fetch's output - see
    fetch.py's ``NO_MATCH_ANCHOR_CHARS`` comment). These tests assert the
    corrected contract directly: a meta note surfaces when it is actually
    relevant, and does NOT auto-surface just because it is ``locus: meta``
    when it is not."""

    def test_relevant_meta_note_surfaces_on_its_own_merit(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-meta", "standing rule about deploy safety", locus="meta", consequence=9)
        _note(global_wiki, "g-filler", "unrelated filler note about lunch")

        repo = _make_git_repo(self.tmp / "repoA")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "r-meta", "standing rule about test discipline", locus="meta", consequence=9)
        _note(repo_wiki, "r-filler", "unrelated filler note about weather")

        # Query shares "safety" with g-meta's body only - r-meta ("test
        # discipline") and both fillers share no term with it at all.
        selection = fetch.select_multi("database migration rollback safety", start=repo)
        ids = [n.id for n, _ in selection.items]
        self.assertFalse(selection.no_match)
        self.assertIn("g-meta", ids)

    def test_irrelevant_meta_note_does_not_auto_surface(self):
        # The old unconditional per-root anchor reservation would have put
        # r-meta in the result regardless of the query. GAP F: it must NOT
        # appear here - its content ("test discipline") has no relevance
        # to this query, so being locus:meta buys it nothing on its own.
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-meta", "standing rule about deploy safety", locus="meta", consequence=9)

        repo = _make_git_repo(self.tmp / "repoA2")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "r-meta", "standing rule about test discipline", locus="meta", consequence=9)
        _note(repo_wiki, "r-topic", "notes about database migration rollback safety procedures")

        selection = fetch.select_multi("database migration rollback safety", start=repo)
        ids = [n.id for n, _ in selection.items]
        self.assertIn("r-topic", ids)  # the actually-relevant note still wins a slot
        self.assertNotIn("r-meta", ids)

    def test_value_mode_ranks_meta_notes_by_score_not_by_per_root_reservation(self):
        # An empty query (the "value" fallback, exactly like the
        # single-root path) is the one place a meta note's OLD per-root
        # count guarantee genuinely disappears under GAP F - it is no
        # longer special-cased at all, so a root whose notes simply sort
        # ahead on value can fill k before another root gets a turn, same
        # as any other note would. This is accepted scope: a real
        # UserPromptSubmit hook call always carries prompt text (mode is
        # "relevance" or "none", never "value"); the empty-query path is a
        # manual/CLI-only corner this fix does not extend the old
        # anchor-specific guarantee to.
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-weak-meta", "a weak global rule", locus="meta", consequence=2)

        repo = _make_git_repo(self.tmp / "repoB")
        repo_wiki = _make_repo_wiki(repo)
        for i in range(5):
            _note(repo_wiki, f"r-strong-meta-{i}", f"strong repo rule {i}", locus="meta", consequence=10)

        selection = fetch.select_multi("", start=repo, k=3)
        ids = {n.id for n, _ in selection.items}
        self.assertEqual(ids, {"r-strong-meta-0", "r-strong-meta-1", "r-strong-meta-2"})


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


class TestGlobalFusedRelevanceOrderNotPreempted(FetchMultirootTestCase):
    """2026-09-30 regression: the per-root proportional-floor fill used to
    reserve each root's share, in per-root order, BEFORE any cross-root
    relevance comparison ran at all - so a strong global match could be
    pushed below weaker repo-local notes just because the repo root was
    processed (and filled its reserve) first. Measured against the real
    global wiki + per-repo wikis (7 real prompts, 16 on-topic prompt/repo
    pairs, repo processed before global - matching real discovery order):
    the globally-best note landed outside the top 3 in 15 of 16 cases.

    Fix: output order is the global fused-relevance order across every
    root; the per-root floor is only a guarantee that a root with a
    relevant note is represented SOMEWHERE in the ``k`` results (never
    zero), applied by displacing the lowest-ranked tail item(s) already
    selected - never pre-empting a higher-scoring note from the top ranks.

    Fixture: a repo root (processed FIRST, as real discovery orders it -
    most-specific first) holds one weak, barely-relevant note (shares only
    "rollback" with the query, diluted by padding words) plus one
    unrelated decoy; the global root (processed second) holds the single
    best match plus two partial-overlap fillers that outscore the repo's
    note on raw relevance. Pre-fix, repo-first processing would have put
    the repo's own (weaker) note ahead of the global root's stronger
    fillers/best match purely by iteration order; this asserts the
    corrected contract instead."""

    def _fixture(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "global-strong", "database migration rollback safety procedure")
        _note(global_wiki, "global-filler-1", "database migration rollback procedure")
        _note(global_wiki, "global-filler-2", "database migration safety")

        repo = _make_git_repo(self.tmp / "repoA")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "repo-weak",
              "rollback mentioned briefly here for context padding words extra")
        _note(repo_wiki, "repo-decoy", "unrelated decoy content about gardening")
        return repo

    def test_strong_global_match_ranks_first_above_weaker_repo_notes(self):
        repo = self._fixture()
        selection = fetch.select_multi(
            "database migration rollback safety procedure", start=repo, k=3,
        )
        ids = [n.id for n, _ in selection.items]
        self.assertEqual(ids[0], "global-strong")

    def test_weaker_repo_local_note_still_appears_within_k(self):
        # k=3 is smaller than the number of candidates that clear the
        # confidence floor (4) and repo-weak ranks LAST of the four on raw
        # relevance - without the inclusion guarantee it would be squeezed
        # out entirely (the pre-fix per-root-reserve bug this regresses
        # against ran the opposite direction: it could squeeze out the
        # STRONGER global notes instead, by filling the repo's reserve
        # first). Either failure mode is wrong; this asserts repo-weak is
        # guaranteed a seat without displacing global-strong/global-filler-1
        # (the two candidates that actually outrank it).
        repo = self._fixture()
        selection = fetch.select_multi(
            "database migration rollback safety procedure", start=repo, k=3,
        )
        ids = [n.id for n, _ in selection.items]
        self.assertIn("repo-weak", ids)
        self.assertEqual(ids, ["global-strong", "global-filler-1", "repo-weak"])

    def test_full_k_shows_pure_global_relevance_order(self):
        # With k large enough for every candidate that clears the
        # confidence floor, no displacement is needed at all - the result
        # is exactly the global fused-relevance order, repo-weak included
        # on its own (last-place) merit rather than any guarantee.
        repo = self._fixture()
        selection = fetch.select_multi(
            "database migration rollback safety procedure", start=repo, k=8,
        )
        ids = [n.id for n, _ in selection.items]
        self.assertEqual(
            ids, ["global-strong", "global-filler-1", "global-filler-2", "repo-weak"],
        )


class TestInclusionGuaranteeNeverEvictsTopRank(FetchMultirootTestCase):
    """2026-09-30 regression in the *fix above*: the inclusion-guarantee
    fallback (when every item currently in `included` is already its own
    root's sole representative - the `displace_at is None` branch) used to
    force a displacement anyway, picking `included[-1]` unconditionally.
    In global rank order `included[-1]` is whatever is CURRENTLY the
    lowest-ranked included item - which, at this exact boundary (k equal
    to the number of roots still needing a guaranteed seat, e.g. k=1 with
    2 roots), can be the single highest-ranked candidate overall, not a
    tail item. That directly violates this fix's own stated invariant
    ("the per-root floor ... never pre-empts higher-scoring notes from the
    top ranks"). Correct behavior: leave the root ungoverned in this
    boundary case (safe degradation), never evict the global #1."""

    def test_k1_two_roots_keeps_the_single_best_match(self):
        rootA = self.tmp / "rootA"
        rootB = self.tmp / "rootB"
        _note(rootA, "a-strong", "rollback safety")
        _note(rootB, "b-weak",
              "rollback padding words extra unrelated filler content here more words")

        roots = [WikiRoot(path=rootA, kind="repo"), WikiRoot(path=rootB, kind="external")]
        selection = fetch.select_multi("rollback safety", roots=roots, k=1)
        ids = [n.id for n, _ in selection.items]
        self.assertEqual(ids, ["a-strong"])

    def test_k_smaller_than_root_count_never_swaps_out_top_items(self):
        # 3 roots, k=2: rootA and rootB are both strong exact matches that
        # legitimately fill k on merit (each its own sole representative);
        # rootC clears the confidence floor but only weakly and ranks
        # below both. rootC's guarantee has no safe tail to displace
        # without evicting one of the two top matches - it must go
        # unrepresented rather than knocking out a higher-scoring match.
        rootA = self.tmp / "rootA"
        rootB = self.tmp / "rootB"
        rootC = self.tmp / "rootC"
        _note(rootA, "a-strong", "rollback safety procedure")
        _note(rootB, "b-strong", "rollback safety migration")
        _note(rootC, "c-weak",
              "rollback padding words extra unrelated filler content here more words")

        roots = [
            WikiRoot(path=rootA, kind="repo"),
            WikiRoot(path=rootB, kind="external"),
            WikiRoot(path=rootC, kind="global"),
        ]
        selection = fetch.select_multi("rollback safety procedure migration", roots=roots, k=2)
        ids = {n.id for n, _ in selection.items}
        self.assertEqual(ids, {"a-strong", "b-strong"})


class TestConfidenceFloorNoPaddedFiller(FetchMultirootTestCase):
    """An off-topic prompt (no term overlap anywhere) gets the GAP F
    no-match fallback - at most ONE short locus:meta pointer line (never
    the old full per-root anchor set), plus the explicit no-match marker -
    never a confident-looking but wrong note padded in to fill k."""

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

    def test_off_topic_query_is_at_most_one_short_anchor_plus_no_match(self):
        repo = self._two_root_fixture()
        selection = fetch.select_multi("xylophone kumquat zephyr", start=repo)
        ids = {n.id for n, _ in selection.items}
        # GAP F: at most ONE anchor line total - never both roots'
        # anchors unconditionally. r-meta and g-meta tie on value (same
        # consequence/freshness/uses); the tie-break prefers the
        # most-specific root in scope (repo over global).
        self.assertEqual(ids, {"r-meta"})
        self.assertTrue(selection.no_match)
        self.assertEqual(selection.collisions, [])

    def test_off_topic_query_marker_in_context_block(self):
        repo = self._two_root_fixture()
        block = fetch.fetch_context("xylophone kumquat zephyr", start=repo, record=False)
        self.assertIn("<!-- no query-relevant notes found -->", block)
        self.assertIn("r-meta", block)
        self.assertNotIn("g-meta", block)
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

    def test_per_wiki_convention_files_are_never_collisions(self):
        # Each wiki's own agenda/ideas log is shown, but never annotated as
        # an ambiguous id - only a genuinely shared note id is.
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        repo = _make_git_repo(self.tmp / "repoF")
        repo_wiki = _make_repo_wiki(repo)
        for wiki, who in ((global_wiki, "global"), (repo_wiki, "repo")):
            _note(wiki, "agenda", f"database migration rollback agenda - {who}")
            _note(wiki, "ideas", f"database migration rollback ideas - {who}")
            _note(wiki, "shared-id", f"database migration rollback safety - {who}")

        selection = fetch.select_multi("database migration rollback", start=repo, k=10)
        self.assertEqual(selection.collisions, ["shared-id"])
        self.assertEqual(len([n for n, _ in selection.items if n.id == "ideas"]), 2)

    def test_duplicate_id_across_same_kind_roots_still_flagged(self):
        # F09's collision detection is keyed on WikiRoot identity, not
        # ``kind`` - two distinct physical roots that happen to share a
        # ``kind`` label (only reachable via the explicit ``roots=`` API,
        # since real discovery never repeats a kind) must still surface as
        # a genuine collision, not be silently dropped.
        root_x = self.tmp / "rootX"
        root_y = self.tmp / "rootY"
        _note(root_x, "dup-id", "database migration rollback safety - X version")
        _note(root_y, "dup-id", "database migration rollback safety - Y version")

        roots = [
            WikiRoot(path=root_x, kind="external", repo="repoX"),
            WikiRoot(path=root_y, kind="external", repo="repoY"),
        ]
        selection = fetch.select_multi(
            "database migration rollback safety", roots=roots, k=10
        )
        self.assertEqual(selection.collisions, ["dup-id"])
        matching = [n for n, _ in selection.items if n.id == "dup-id"]
        self.assertEqual(len(matching), 2)


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


class TestPerPromptTopSlotsAreQueryDependent(FetchMultirootTestCase):
    """GAP F regression (2026-09-30): pre-fix, ``META_ANCHOR_CAP_PER_ROOT
    (2) x N roots`` was reserved out of every fetch's result unconditionally,
    before relevance ranking ran at all. With 2 roots that is 4 slots -
    exactly ``DEFAULT_K // 2`` - occupied by the SAME two locus:meta notes
    per root on every single prompt, on-topic or not: the top-4 of any two
    on-topic prompts were identical regardless of what each prompt was
    actually about, and an actually-relevant note could be pushed past the
    top-k entirely. This is a direct repro of that measurement against a
    fixed 2-root fixture (2 locus:meta anchors per root, exactly the
    real-world shape), asserting the corrected contract: an on-topic
    prompt's relevance-ranked result lands in the top slots, and the
    top-4 for two differently-themed prompts is NOT the same fixed set."""

    def _two_root_fixture_with_anchors_and_topics(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-meta-1", "standing rule about code review etiquette",
              locus="meta", consequence=9)
        _note(global_wiki, "g-meta-2", "standing rule about commit message style",
              locus="meta", consequence=9)
        _note(global_wiki, "topic-db",
              "database migration rollback safety procedure", consequence=5)

        repo = _make_git_repo(self.tmp / "repoG")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "r-meta-1", "standing rule about branch naming conventions",
              locus="meta", consequence=9)
        _note(repo_wiki, "r-meta-2", "standing rule about pull request templates",
              locus="meta", consequence=9)
        _note(repo_wiki, "topic-git", "git worktree branch management", consequence=5)
        return repo

    def test_on_topic_prompt_lands_its_relevant_note_in_top_slots(self):
        repo = self._two_root_fixture_with_anchors_and_topics()
        selection = fetch.select_multi(
            "database migration rollback safety procedure", start=repo, k=8,
        )
        ids = [n.id for n, _ in selection.items]
        self.assertFalse(selection.no_match)
        self.assertIn("topic-db", ids[:4])

    def test_top_four_differs_across_differently_themed_prompts(self):
        repo = self._two_root_fixture_with_anchors_and_topics()
        ids_db = [n.id for n, _ in fetch.select_multi(
            "database migration rollback safety procedure", start=repo, k=8,
        ).items]
        ids_git = [n.id for n, _ in fetch.select_multi(
            "git worktree branch management", start=repo, k=8,
        ).items]

        # Each prompt's own topic lands in ITS top 4, not the other's.
        self.assertIn("topic-db", ids_db[:4])
        self.assertIn("topic-git", ids_git[:4])
        self.assertNotIn("topic-git", ids_db[:4])
        self.assertNotIn("topic-db", ids_git[:4])

        # The actual GAP F repro: two differently-themed on-topic prompts
        # must NOT share an identical top-4 - pre-fix, both were always
        # exactly {g-meta-1, g-meta-2, r-meta-1, r-meta-2} (the same 4
        # anchors, unconditionally, regardless of the prompt).
        self.assertNotEqual(ids_db[:4], ids_git[:4])
        fixed_anchor_set = {"g-meta-1", "g-meta-2", "r-meta-1", "r-meta-2"}
        self.assertNotEqual(set(ids_db[:4]), fixed_anchor_set)
        self.assertNotEqual(set(ids_git[:4]), fixed_anchor_set)


if __name__ == "__main__":
    unittest.main()
