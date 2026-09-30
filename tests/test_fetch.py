"""Tests for shapa.fetch: the read path (select, snippet, record_use)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from shapa import fetch, score, store

FIX = Path(__file__).parent / "fixtures" / "fetch"


class TestFetch(unittest.TestCase):
    def test_high_value_ranks_first(self):
        selected = fetch.select("testing discipline", root=FIX, k=5)
        ids = [n.id for n, _ in selected]
        self.assertEqual(ids[0], "f-meta")  # meta + consequence 9 + relevance
        self.assertIn("f-out", ids)

    def test_snippet_is_bounded(self):
        selected = fetch.select("word", root=FIX, k=5)
        long = next(snip for n, snip in selected if n.id == "f-long")
        self.assertLessEqual(len(long), fetch.SNIPPET_CHARS + 4)  # + " ..."
        self.assertTrue(long.endswith("..."))

    def test_context_block_shape(self):
        block = fetch.fetch_context("testing", root=FIX, record=False)
        self.assertTrue(block.startswith("<shapa-memory>"))
        self.assertTrue(block.rstrip().endswith("</shapa-memory>"))
        self.assertIn("### f-meta (rule)", block)

    def test_record_use_bumps_the_index_store_not_the_file(self):
        # shapa-backend-spec.md §10 decision 7 ("reads never write notes"):
        # a use bumps store.py's counter, keyed by (root, note id) - the
        # note file itself is untouched, so `git status` on a wiki checkout
        # stays clean after a fetch.
        tmp = Path(tempfile.mkdtemp())
        try:
            for f in FIX.glob("*.md"):
                shutil.copy(f, tmp / f.name)
            before = {f: f.read_bytes() for f in tmp.glob("*.md")}
            selected = fetch.select("testing", root=tmp, k=5)
            fetch.fetch_context("testing", root=tmp, record=True)
            for n, _ in selected:
                uses, last_used = store.get_use(tmp, n.id)
                self.assertEqual(uses, 1)
                self.assertIsNotNone(last_used)
                # score.score_node still reads the file directly (legacy,
                # untouched by this read path) - it must show the frontmatter
                # value unchanged, never bumped by fetch.
                self.assertEqual(score.score_node(tmp / f"{n.id}.md").uses, 0)
            for f, contents in before.items():
                self.assertEqual(f.read_bytes(), contents, f"{f} was written by a read path")
        finally:
            shutil.rmtree(tmp)

    def test_live_use_count_feeds_scoring(self):
        # fetch's own value ranking picks the bumped count back up from the
        # store (§10 decision 7: "score.py reads them from there"), so the
        # use signal stays live even though the file is never rewritten.
        tmp = Path(tempfile.mkdtemp())
        try:
            for f in FIX.glob("*.md"):
                shutil.copy(f, tmp / f.name)
            for _ in range(60):
                fetch.fetch_context("testing", root=tmp, record=True)
            uses, _ = store.get_use(tmp, "f-out")
            self.assertGreaterEqual(uses, 50)
            data = fetch._load_root_data(tmp, "")
            self.assertGreater(data.value["f-out"], score.score_meta(data.nodes["f-out"].meta)[0])
        finally:
            shutil.rmtree(tmp)

    def test_empty_query_still_returns_by_score(self):
        # No relevance, pure score ranking; high-value note still first.
        selected = fetch.select("", root=FIX, k=5)
        self.assertEqual(selected[0][0].id, "f-meta")

    def test_bm25_relevance_steers(self):
        # 'git' prompt: after the meta anchor, the git note outranks the filler.
        ids = [n.id for n, _ in fetch.select("git repository commit", root=FIX, k=5)]
        self.assertEqual(ids[0], "f-meta")  # meta anchor
        self.assertLess(ids.index("f-out"), ids.index("f-long"))

    def test_archived_notes_never_surface(self):
        # GAP A acceptance: a query that matches an archived note's content
        # exactly must never surface it - archive/ is never loaded, so it
        # can never win on relevance either.
        tmp = Path(tempfile.mkdtemp())
        try:
            for f in FIX.glob("*.md"):
                shutil.copy(f, tmp / f.name)
            archive = tmp / "archive"
            archive.mkdir()
            (archive / "watchdog-alert.md").write_text(
                "---\nid: watchdog-alert\ntype: memory\n"
                "created: \"2026-01-01T00:00:00Z\"\nconsequence: 9\n"
                "locus: output\nuses: 0\n---\n"
                "keeper watchdog alert firing on the exchange feed\n",
                encoding="utf-8",
            )
            selected = fetch.select("keeper watchdog alert", root=tmp, k=5)
            ids = [n.id for n, _ in selected]
            self.assertNotIn("watchdog-alert", ids)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
