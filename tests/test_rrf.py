"""Tests for shapa.rank: Reciprocal Rank Fusion and score-normalized fusion
(shapa-backend-spec.md §5, GAP C)."""

import unittest

from shapa import rank


class TestRRF(unittest.TestCase):
    def test_single_ranking_preserves_relative_order(self):
        fused = rank.rrf([{"a": 5.0, "b": 3.0, "c": 1.0}])
        self.assertGreater(fused["a"], fused["b"])
        self.assertGreater(fused["b"], fused["c"])

    def test_agreement_across_rankings_boosts_the_shared_top_result(self):
        # 'a' is #1 in both rankings (two positive terms); 'b' is #1 in only
        # r2, absent (no evidence at all) from r1 (one positive term).
        # Agreement across sources must win over being top-ranked once.
        r1 = {"a": 10.0, "c": 1.0}
        r2 = {"b": 10.0, "a": 8.0, "d": 0.5}
        fused = rank.rrf([r1, r2])
        self.assertGreater(fused["a"], fused["b"])

    def test_id_present_in_only_one_ranking_still_gets_a_score(self):
        # Graceful degradation to single-backend behavior: an id only one
        # source retrieved is not zeroed out just because it's absent
        # (equivalently score 0.0) from the other.
        fused = rank.rrf([{"a": 5.0}, {"b": 5.0}])
        self.assertIn("a", fused)
        self.assertIn("b", fused)
        self.assertGreater(fused["a"], 0.0)
        self.assertGreater(fused["b"], 0.0)

    def test_zero_scores_never_manufacture_a_rank(self):
        # Every id tied at 0.0 in a ranking means that ranking retrieved
        # NOTHING - none of them should get an artificial rank-based term
        # just because they're all tied with each other.
        fused = rank.rrf([{"a": 0.0, "b": 0.0, "c": 0.0}])
        self.assertEqual(fused, {})

    def test_all_rankings_empty_or_zero_yields_empty_fusion(self):
        self.assertEqual(rank.rrf([{}, {"a": 0.0, "b": 0.0}]), {})

    def test_no_rankings_yields_empty_fusion(self):
        self.assertEqual(rank.rrf([]), {})

    def test_result_never_padded_to_the_full_id_universe(self):
        # rrf() only returns ids it positively ranked - callers that need a
        # complete dict (fetch.py) backfill 0.0 themselves; rrf() must not
        # do that padding itself (it can't know the full id universe from
        # partial per-source rankings).
        fused = rank.rrf([{"a": 5.0, "b": 0.0}])
        self.assertEqual(set(fused), {"a"})

    def test_a_second_zero_scoring_source_does_not_dilute_a_real_hit(self):
        # 'a' is the only thing found relevant anywhere; a second ranking
        # that found nothing must not change its fused score at all.
        alone = rank.rrf([{"a": 5.0, "b": 1.0}])
        with_empty_source = rank.rrf([{"a": 5.0, "b": 1.0}, {"a": 0.0, "b": 0.0}])
        self.assertEqual(alone, with_empty_source)

    def test_rank_positions_are_scale_invariant_across_wildly_different_ranges(self):
        # BM25's unbounded scores vs. cosine's [0, 1] range must fuse purely
        # by RANK, never by raw magnitude. Both rankings agree on the same
        # order (a > b > c) despite operating on utterly different scales
        # (hundreds vs. a fraction of one) - the fused order must match
        # that agreed rank order exactly, unaffected by the scale gap.
        bm25_like = {"a": 812.0, "b": 400.0, "c": 5.0}
        cosine_like = {"a": 0.95, "b": 0.5, "c": 0.02}
        fused = rank.rrf([bm25_like, cosine_like])
        self.assertGreater(fused["a"], fused["b"])
        self.assertGreater(fused["b"], fused["c"])

    def test_custom_k_changes_the_fused_scale_but_not_the_order(self):
        r = {"a": 3.0, "b": 2.0, "c": 1.0}
        default_k = rank.rrf([r])
        small_k = rank.rrf([r], k=1)
        self.assertNotEqual(default_k, small_k)
        for fused in (default_k, small_k):
            self.assertGreater(fused["a"], fused["b"])
            self.assertGreater(fused["b"], fused["c"])


class TestMinmaxNormalize(unittest.TestCase):
    def test_top_score_lands_at_exactly_one(self):
        norm = rank.minmax_normalize({"a": 8.0, "b": 4.0, "c": 2.0})
        self.assertEqual(norm["a"], 1.0)

    def test_zero_scores_stay_zero(self):
        norm = rank.minmax_normalize({"a": 8.0, "b": 0.0})
        self.assertEqual(norm["b"], 0.0)

    def test_all_zero_ranking_normalizes_to_all_zero(self):
        self.assertEqual(rank.minmax_normalize({"a": 0.0, "b": 0.0}), {"a": 0.0, "b": 0.0})

    def test_empty_ranking_normalizes_to_empty(self):
        self.assertEqual(rank.minmax_normalize({}), {})

    def test_relative_magnitude_is_preserved_not_just_order(self):
        # Unlike a rank position, the normalized gap between a clear
        # standout and a distant second must survive - that gap is
        # exactly what lets fuse() tell "strong in one modality" apart
        # from "merely first by a hair."
        norm = rank.minmax_normalize({"a": 10.0, "b": 1.0})
        self.assertLess(norm["b"], 0.2)


class TestFuse(unittest.TestCase):
    def test_a_standout_in_one_modality_beats_lukewarm_agreement_in_both(self):
        # shapa-backend-spec.md GAP C's own motivating failure mode: 'x' is
        # the clear best match in modality 1 (normalizes near 1.0) and
        # absent from modality 2 entirely (zero BM25 overlap, say); 'y' is
        # merely mediocre in BOTH modalities (a middling normalized score
        # in each) - RRF would let 'y' win by being "found twice"; fuse()
        # must let 'x' win on the strength of its one genuinely strong
        # match instead.
        # 'y' is rank-2 in BOTH modalities (present but never the best in
        # either) - neither modality's own standout ('x' in modality1,
        # 'w' in modality2) ever shares a modality with the other.
        modality1 = {"x": 10.0, "y": 3.0, "z": 1.0}
        modality2 = {"w": 10.0, "y": 3.0, "z": 2.5}
        fused = rank.fuse([modality1, modality2])
        self.assertGreater(fused["x"], fused["y"])

        # The SAME inputs, fused by plain RRF, show the failure this test
        # guards against: 'y' (rank-agreement in both) beats 'x' (rank 1
        # in only one), the exact burying GAP C's fix corrects.
        rrf_fused = rank.rrf([modality1, modality2])
        self.assertGreater(rrf_fused["y"], rrf_fused["x"])

    def test_wildly_different_raw_scales_still_fuse_by_normalized_magnitude(self):
        bm25_like = {"a": 812.0, "b": 400.0, "c": 5.0}
        cosine_like = {"a": 0.95, "b": 0.5, "c": 0.02}
        fused = rank.fuse([bm25_like, cosine_like])
        self.assertGreater(fused["a"], fused["b"])
        self.assertGreater(fused["b"], fused["c"])

    def test_id_present_in_only_one_ranking_still_gets_a_score(self):
        fused = rank.fuse([{"a": 5.0}, {"b": 5.0}])
        self.assertIn("a", fused)
        self.assertIn("b", fused)

    def test_a_ranking_with_nothing_positive_is_dropped_not_averaged_in(self):
        # A second modality that found NOTHING must not dilute the first
        # modality's real signal by being averaged in as a 0 - its weight
        # is excluded entirely, matching rrf()'s graceful degradation.
        alone = rank.fuse([{"a": 5.0, "b": 1.0}])
        with_empty_source = rank.fuse([{"a": 5.0, "b": 1.0}, {"a": 0.0, "b": 0.0}])
        self.assertEqual(alone, with_empty_source)

    def test_all_rankings_empty_or_zero_yields_empty_fusion(self):
        self.assertEqual(rank.fuse([{}, {"a": 0.0}]), {})

    def test_no_rankings_yields_empty_fusion(self):
        self.assertEqual(rank.fuse([]), {})

    def test_custom_weights_shift_which_modality_dominates(self):
        modality1 = {"x": 10.0, "y": 1.0}   # x is the standout here
        modality2 = {"x": 1.0, "y": 10.0}   # y is the standout here
        favor_1 = rank.fuse([modality1, modality2], weights=[0.9, 0.1])
        favor_2 = rank.fuse([modality1, modality2], weights=[0.1, 0.9])
        self.assertGreater(favor_1["x"], favor_1["y"])
        self.assertGreater(favor_2["y"], favor_2["x"])


if __name__ == "__main__":
    unittest.main()
