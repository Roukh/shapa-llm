"""Tests for shapa.score: node scoring and the mechanical use counter."""

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shapa import score
from shapa.validate import validate_node

FIX = Path(__file__).parent / "fixtures" / "score"
NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
LATER = datetime(2027, 7, 1, tzinfo=timezone.utc)


class TestScore(unittest.TestCase):

    def test_meta_high_outranks_output_low(self):
        hi = score.score_node(FIX / "high.md", now=NOW)
        lo = score.score_node(FIX / "low.md", now=NOW)
        self.assertGreater(hi.score, lo.score)
        self.assertEqual(hi.locus, "meta")
        self.assertEqual(hi.consequence, 9)

    def test_freshness_decays_with_age(self):
        near = score.score_node(FIX / "low.md", now=NOW)
        far = score.score_node(FIX / "low.md", now=LATER)
        self.assertAlmostEqual(near.freshness, 1.0, places=3)
        self.assertLess(far.freshness, near.freshness)
        self.assertLess(far.score, near.score)

    def test_high_consequence_decays_slower(self):
        # Same one-year age: the high-consequence node retains more freshness.
        hi = score.score_node(FIX / "high.md", now=LATER)
        lo = score.score_node(FIX / "low.md", now=LATER)
        self.assertGreater(hi.freshness, lo.freshness)

    def test_record_use_increments_and_boosts(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "low.md"
            f.write_text((FIX / "low.md").read_text(), encoding="utf-8")
            before = score.score_node(f, now=NOW)
            new_count = score.record_use(f, now=NOW)
            self.assertEqual(new_count, 1)
            after = score.score_node(f, now=NOW)
            self.assertEqual(after.uses, 1)
            # use_factor > 1 with freshness held at 1.0 (last_used set to NOW).
            self.assertGreater(after.score, before.score)
        finally:
            shutil.rmtree(tmp)

    def test_record_use_twice(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "high.md"
            f.write_text((FIX / "high.md").read_text(), encoding="utf-8")
            score.record_use(f, now=NOW)
            self.assertEqual(score.record_use(f, now=NOW), 2)
            self.assertEqual(score.score_node(f, now=NOW).uses, 2)
        finally:
            shutil.rmtree(tmp)

    def test_invalid_score_fields_rejected(self):
        result = validate_node(FIX / "badscore.md")
        rules = {v.rule for v in result.violations}
        self.assertFalse(result.valid)
        self.assertIn("S01", rules)  # consequence out of range
        self.assertIn("S02", rules)  # invalid locus

    def test_every_shipped_doc_validates(self):
        # Every bundled design doc (arch/ + AGENTS.md) installed by `shapa init`
        # carries the uniform frontmatter schema.
        assets = Path(__file__).parent.parent / "shapa" / "assets"
        checked = 0
        for f in assets.rglob("*.md"):
            result = validate_node(f)
            self.assertEqual(result.errors, [], f"{f}: {result.errors}")
            checked += 1
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
