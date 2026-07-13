"""Tests for shapa.maintain: stale detection, auto-merge, expanded prune.

Embeddings are not installed in CI, so similarity falls back to Jaccard.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shapa import maintain
from shapa.nodes import load_nodes

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _note(d: Path, nid, created, uses, body, consequence=6):
    (d / f"{nid}.md").write_text(
        "---\n"
        f"id: {nid}\ntype: rule\ncreated: \"{created}\"\n"
        f"consequence: {consequence}\nlocus: output\nuses: {uses}\n---\n{body}\n",
        encoding="utf-8",
    )


class TestMaintain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _note(self.tmp, "keepme", "2026-06-25T00:00:00Z", 0, "anchor links [[dup-low]] [[dup-high]]")
        _note(self.tmp, "stale-old", "2020-01-01T00:00:00Z", 0, "old unused links [[keepme]]")
        _note(self.tmp, "used-old", "2020-01-01T00:00:00Z", 3, "old but used links [[keepme]]")
        _note(self.tmp, "lonely", "2026-06-25T00:00:00Z", 0, "no links at all")
        dup = "always validate every input at the boundary before processing it links [[keepme]]"
        _note(self.tmp, "dup-low", "2026-06-25T00:00:00Z", 0, dup, consequence=5)
        _note(self.tmp, "dup-high", "2026-06-25T00:00:00Z", 0, dup, consequence=8)
        # A non-markdown sidecar living directly in the wiki dir (e.g. a repo's real
        # .shapa/adr-constraints.json — see scripts/docs/adr-fitness.js in roukh-llm).
        # Shaped to look maximally "prunable" if it were ever treated as a node: old,
        # zero-linked, zero-use — so this fixture would catch a future regression
        # (e.g. someone widening nodes.py's `rglob("*.md")` to `rglob("*")`) even
        # though it never fires against today's code, which already excludes it by
        # extension before it can be scored as an orphan or stale note.
        self.sidecar = self.tmp / "adr-constraints.json"
        self.sidecar.write_text(
            '{"id": "adr-fitness", "kind": "path", "pattern": "^legacy/"}\n', encoding="utf-8"
        )
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(self.sidecar, (old, old))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_find_stale(self):
        stale = maintain.find_stale(load_nodes(self.tmp), NOW, max_age_days=90)
        self.assertIn("stale-old", stale)
        self.assertNotIn("used-old", stale)

    def test_auto_merge_keeps_higher_score_and_repoints(self):
        merged = maintain.merge_duplicates(self.tmp)
        self.assertEqual([(d, k) for d, k, _ in merged], [("dup-low", "dup-high")])
        self.assertFalse((self.tmp / "dup-low.md").exists())
        self.assertTrue((self.tmp / "dup-high.md").exists())
        keepme = (self.tmp / "keepme.md").read_text()
        self.assertNotIn("[[dup-low]]", keepme)
        self.assertIn("[[dup-high]]", keepme)

    def test_maintain_prune_and_merge(self):
        r = maintain.maintain(self.tmp, prune=True, max_age_days=90, now=NOW)
        self.assertEqual([(d, k) for d, k, _ in r["merged"]], [("dup-low", "dup-high")])
        self.assertEqual(set(r["pruned"]), {"stale-old", "lonely"})
        self.assertFalse((self.tmp / "lonely.md").exists())
        self.assertTrue((self.tmp / "dup-high.md").exists())

    def test_dry_run_changes_nothing(self):
        r = maintain.maintain(self.tmp, prune=True, max_age_days=90, now=NOW, dry_run=True)
        # reports what WOULD happen...
        self.assertEqual([(d, k) for d, k, _ in r["merged"]], [("dup-low", "dup-high")])
        self.assertEqual(set(r["pruned"]), {"stale-old", "lonely"})
        # ...but every file is still there
        self.assertEqual(len(list(self.tmp.glob("*.md"))), 6)
        self.assertTrue((self.tmp / "dup-low.md").exists())

    def test_resolve_noop_without_claude_or_candidates(self):
        # No rule-vs-rule contradiction candidates here beyond the merged dupe.
        r = maintain.maintain(self.tmp, prune=False, resolve=False, now=NOW)
        self.assertEqual(r["resolved"], [])

    def test_non_md_sidecar_is_never_loaded_as_a_node(self):
        # load_nodes globs *.md exclusively — a sidecar like adr-constraints.json is
        # never even a candidate node, regardless of its own age/content.
        nodes = load_nodes(self.tmp)
        self.assertNotIn("adr-constraints", nodes)
        self.assertNotIn("adr-constraints.json", nodes)

    def test_prune_never_deletes_a_non_md_sidecar(self):
        # brain task 67d830ea-d33b-4ce8-b4a8-5c00b4d15da8: a tracked non-markdown
        # .shapa sidecar (e.g. adr-constraints.json) must survive `shapa maintain
        # --prune` no matter how old/unlinked it is — the pruner's scan path is
        # markdown wiki nodes only.
        r = maintain.maintain(self.tmp, prune=True, max_age_days=1, now=NOW)
        self.assertTrue(self.sidecar.exists(), "non-md sidecar was deleted by --prune")
        self.assertNotIn("adr-constraints", r["pruned"])
        self.assertNotIn("adr-constraints.json", r["pruned"])

    def test_prune_never_deletes_a_non_md_sidecar_non_dry_run_real_files(self):
        # Belt-and-braces: same assertion via the real (non-dry-run) unlink path,
        # with every OTHER on-disk file in the same directory actually pruned, so
        # this proves survival is per-file selection, not an accidental global no-op.
        r = maintain.maintain(self.tmp, prune=True, max_age_days=1, now=NOW, dry_run=False)
        self.assertIn("lonely", r["pruned"])  # sanity: real pruning did happen
        self.assertTrue(self.sidecar.exists())
        self.assertEqual(
            self.sidecar.read_text(encoding="utf-8"),
            '{"id": "adr-fitness", "kind": "path", "pattern": "^legacy/"}\n',
        )


if __name__ == "__main__":
    unittest.main()
