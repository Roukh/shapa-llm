"""Tests for shapa.fetch: the read path (select, snippet, record_use)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from shapa import fetch, score

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

    def test_record_use_bumps_surfaced_notes(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            for f in FIX.glob("*.md"):
                shutil.copy(f, tmp / f.name)
            selected = fetch.select("testing", root=tmp, k=5)
            fetch.fetch_context("testing", root=tmp, record=True)
            for n, _ in selected:
                self.assertEqual(score.score_node(tmp / f"{n.id}.md").uses, 1)
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


if __name__ == "__main__":
    unittest.main()
