"""Tests for shapa.embed: the model2vec backend (shapa-backend-spec.md §5).

The caching/invalidation logic (:func:`note_vectors`) is tested against a
monkeypatched ``embed_one`` so it is fast and deterministic regardless of
whether the real ``[semantic]`` extra happens to be installed - matching
``tests/test_fetch_multiroot.py``'s own "force the backend, don't depend on
the environment" convention. A few tests that need the *real* model2vec
backend (installed via the ``semantic`` extra) are skipped when it is not
available, exactly like every other optional-dependency path in this suite.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shapa import embed


class TestCosine(unittest.TestCase):
    def test_identical_vectors_score_one(self):
        v = [0.6, 0.8, 0.0]
        self.assertAlmostEqual(embed.cosine(v, v), 1.0, places=6)

    def test_orthogonal_vectors_score_zero(self):
        self.assertAlmostEqual(embed.cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_empty_vector_scores_zero(self):
        self.assertEqual(embed.cosine([], [1.0, 0.0]), 0.0)
        self.assertEqual(embed.cosine([1.0, 0.0], []), 0.0)


class TestNoteVectorsCaching(unittest.TestCase):
    """``note_vectors`` re-embeds only what changed, and never trusts a
    cache written by a different embedding model - a bare content-hash
    match is not enough once the vector *space* itself can change (the
    sentence-transformers -> model2vec swap this slice makes, and any
    future backend swap)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calls = []
        patcher = mock.patch.object(
            embed, "embed_one",
            side_effect=lambda text: self.calls.append(text) or [float(len(text)), 0.0],
        )
        self.mock_embed_one = patcher.start()
        self.addCleanup(patcher.stop)
        # Pin the model name so the test controls the cache stamp precisely,
        # independent of whichever real model happens to be configured.
        patcher2 = mock.patch.object(embed, "_MODEL_NAME", "test-model-v1")
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_embeds_each_text_once_then_reuses_cache(self):
        texts = {"a": "hello world", "b": "goodbye"}
        out1 = embed.note_vectors(self.tmp, texts)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(out1["a"], [11.0, 0.0])

        out2 = embed.note_vectors(self.tmp, texts)  # unchanged content
        self.assertEqual(len(self.calls), 2, "unchanged text was re-embedded")
        self.assertEqual(out2, out1)

    def test_changed_content_is_re_embedded_unchanged_is_not(self):
        embed.note_vectors(self.tmp, {"a": "hello world", "b": "goodbye"})
        self.calls.clear()
        embed.note_vectors(self.tmp, {"a": "hello world CHANGED", "b": "goodbye"})
        self.assertEqual(self.calls, ["hello world CHANGED"])

    def test_removed_note_is_dropped_from_cache(self):
        embed.note_vectors(self.tmp, {"a": "hello", "b": "world"})
        embed.note_vectors(self.tmp, {"a": "hello"})
        cache = json.loads((self.tmp / embed._CACHE_FILE).read_text())
        self.assertNotIn("b", cache["notes"])

    def test_cache_is_stamped_with_the_model_name(self):
        embed.note_vectors(self.tmp, {"a": "hello"})
        cache = json.loads((self.tmp / embed._CACHE_FILE).read_text())
        self.assertEqual(cache["model"], "test-model-v1")

    def test_cache_from_a_different_model_is_discarded_not_trusted(self):
        # Simulate a cache left behind by a different embedding backend -
        # same note, same content hash, but a different (or absent) model
        # stamp. This must be fully re-embedded, not silently reused just
        # because the content hash still matches.
        stale = {
            "model": "some-other-backend",
            "notes": {"a": {"h": embed._hash("hello"), "v": [999.0, 999.0]}},
        }
        (self.tmp / embed._CACHE_FILE).write_text(json.dumps(stale), encoding="utf-8")
        out = embed.note_vectors(self.tmp, {"a": "hello"})
        self.assertEqual(self.calls, ["hello"])  # re-embedded, not trusted
        self.assertNotEqual(out["a"], [999.0, 999.0])

    def test_legacy_unstamped_cache_is_discarded_not_trusted(self):
        # A cache written before this stamp existed (pre-Slice-6 format:
        # {id: {"h":..., "v":...}} with no "model"/"notes" wrapper at all).
        legacy = {"a": {"h": embed._hash("hello"), "v": [999.0, 999.0]}}
        (self.tmp / embed._CACHE_FILE).write_text(json.dumps(legacy), encoding="utf-8")
        out = embed.note_vectors(self.tmp, {"a": "hello"})
        self.assertEqual(self.calls, ["hello"])
        self.assertNotEqual(out["a"], [999.0, 999.0])


@unittest.skipUnless(embed.available(), "model2vec ([semantic] extra) not installed")
class TestRealModel2VecBackend(unittest.TestCase):
    """Exercised only when the real backend is installed - the contract
    (available()/embed_one()/note_vectors()/cosine()) is otherwise fully
    covered by the monkeypatched tests above."""

    def test_available_is_true_and_no_torch_is_imported(self):
        import sys
        self.assertTrue(embed.available())
        self.assertNotIn("torch", sys.modules, "model2vec backend must not pull in torch")

    def test_embed_one_returns_a_normalized_float_vector(self):
        import math
        v = embed.embed_one("shapa is a markdown memory graph")
        self.assertIsInstance(v, list)
        self.assertGreater(len(v), 0)
        self.assertTrue(all(isinstance(x, float) for x in v))
        norm = math.sqrt(sum(x * x for x in v))
        self.assertAlmostEqual(norm, 1.0, places=3)

    def test_similar_text_scores_higher_than_unrelated_text(self):
        anchor = embed.embed_one("commit your changes with git before switching branches")
        near = embed.embed_one("use git commit to save your branch changes")
        far = embed.embed_one("the weather today is sunny with a light breeze")
        self.assertGreater(embed.cosine(anchor, near), embed.cosine(anchor, far))


if __name__ == "__main__":
    unittest.main()
