"""Tests for the heartbeat over the [[wikilink]] graph.

Decentralized model: a link to a topic/type that has no file yet (a phantom)
still counts as connectivity. Only a real file with no link in or out at all
is an orphan. In the fixtures: a/b/c link each other, d links only a phantom
topic (so it is NOT an orphan), e links nothing (the only orphan).
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from shapa import heartbeat
from shapa.heartbeat import find_orphans
from shapa.nodes import build_graph, extract_links, load_nodes

GRAPH = Path(__file__).parent / "fixtures" / "graph"


class TestLinks(unittest.TestCase):
    def test_extract_links(self):
        self.assertEqual(
            extract_links("see [[a]] and [[b|alias]] and [[c#h]]"),
            {"a", "b", "c"},
        )

    def test_graph_edges_are_undirected(self):
        g = build_graph(load_nodes(GRAPH))
        self.assertIn("b", g.edges["a"])
        self.assertIn("a", g.edges["b"])

    def test_phantom_topic_counts_as_connectivity(self):
        g = build_graph(load_nodes(GRAPH))
        # d links [[topic]] which has no file; d is still connected, not orphan.
        self.assertIn("topic", g.edges["d"])
        self.assertNotIn("d", find_orphans(g))


class TestHeartbeat(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for f in GRAPH.glob("*.md"):
            shutil.copy(f, self.tmp / f.name)
        # A non-markdown sidecar living directly in the wiki dir (e.g. a repo's
        # real .shapa/adr-constraints.json — see scripts/docs/adr-fitness.js in
        # roukh-llm). It is unlinked, so it would look maximally "prunable" if
        # it were ever treated as a node. shapa/nodes.py's load_nodes() docstring
        # claims its *.md glob protects both shapa.maintain and shapa.heartbeat
        # from ever scoring/unlinking a sidecar like this — this fixture proves
        # the heartbeat half of that claim.
        self.sidecar = self.tmp / "adr-constraints.json"
        self.sidecar.write_text(
            '{"id": "adr-fitness", "kind": "path", "pattern": "^legacy/"}\n', encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_only_zero_link_note_is_orphan(self):
        orphans = find_orphans(build_graph(load_nodes(self.tmp)))
        self.assertEqual(orphans, ["e"])  # only e has no link in or out

    def test_prune_removes_only_orphan(self):
        result = heartbeat.heartbeat(self.tmp, seed=42)
        self.assertEqual(set(result["pruned"]), {"e"})
        for keep in ("a", "b", "c", "d"):
            self.assertTrue((self.tmp / f"{keep}.md").exists())
        self.assertFalse((self.tmp / "e.md").exists())

    def test_dry_run_deletes_nothing(self):
        result = heartbeat.heartbeat(self.tmp, seed=42, dry_run=True)
        self.assertEqual(set(result["pruned"]), {"e"})
        self.assertEqual(len(list(self.tmp.glob("*.md"))), 5)

    def test_prune_never_deletes_a_non_md_sidecar(self):
        # brain task 67d830ea-d33b-4ce8-b4a8-5c00b4d15da8: dry-run half of the
        # pair below — a non-md .shapa sidecar must survive `shapa heartbeat`'s
        # orphan prune, exactly like it survives `shapa maintain --prune`.
        result = heartbeat.heartbeat(self.tmp, seed=42, dry_run=True)
        self.assertTrue(self.sidecar.exists(), "non-md sidecar was deleted by heartbeat prune")
        self.assertNotIn("adr-constraints", result["pruned"])
        self.assertNotIn("adr-constraints.json", result["pruned"])

    def test_prune_never_deletes_a_non_md_sidecar_real_files(self):
        # Belt-and-braces: same assertion via the real (non-dry-run) unlink
        # path, with the actual orphan ("e") still pruned, so this proves
        # survival is per-file selection, not an accidental global no-op.
        result = heartbeat.heartbeat(self.tmp, seed=42, dry_run=False)
        self.assertIn("e", result["pruned"])  # sanity: real pruning did happen
        self.assertTrue(self.sidecar.exists())
        self.assertEqual(
            self.sidecar.read_text(encoding="utf-8"),
            '{"id": "adr-fitness", "kind": "path", "pattern": "^legacy/"}\n',
        )

    def test_deterministic(self):
        t2 = Path(tempfile.mkdtemp())
        for f in GRAPH.glob("*.md"):
            shutil.copy(f, t2 / f.name)
        try:
            r1 = heartbeat.heartbeat(self.tmp, seed=7, dry_run=True)
            r2 = heartbeat.heartbeat(t2, seed=7, dry_run=True)
            self.assertEqual(r1["walk"], r2["walk"])
            self.assertEqual(r1["pruned"], r2["pruned"])
        finally:
            shutil.rmtree(t2)


if __name__ == "__main__":
    unittest.main()
