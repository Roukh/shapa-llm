"""Tests for shapa.nodes: the node model and the archive/attic/.obsidian
exclusion (GAP A - shapa-backend-spec.md §10 decision 6)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from shapa.nodes import is_excluded_path, load_nodes


def _note(d: Path, nid: str, body: str = "body text") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{nid}.md"
    p.write_text(
        "---\n"
        f"id: {nid}\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
        "consequence: 5\nlocus: output\nuses: 0\n---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return p


class TestIsExcludedPath(unittest.TestCase):
    def test_archive_at_any_depth_is_excluded(self):
        self.assertTrue(is_excluded_path(("archive", "foo.md")))
        self.assertTrue(is_excluded_path(("nested", "archive", "foo.md")))

    def test_attic_is_excluded(self):
        self.assertTrue(is_excluded_path(("attic", "foo.md")))

    def test_obsidian_is_excluded(self):
        self.assertTrue(is_excluded_path((".obsidian", "app.json")))

    def test_dependency_and_build_dirs_are_excluded(self):
        for d in ("node_modules", ".venv", "__pycache__"):
            self.assertTrue(is_excluded_path(("tools", "x", d, "pkg", "README.md")), d)

    def test_arch_is_not_excluded(self):
        self.assertFalse(is_excluded_path(("arch", "PRD.md")))

    def test_root_file_is_not_excluded(self):
        self.assertFalse(is_excluded_path(("foo.md",)))

    def test_file_merely_named_archive_is_not_excluded(self):
        # Only containing directories are checked, never the file's own name.
        self.assertFalse(is_excluded_path(("archive.md",)))


class TestLoadNodesExcludesArchiveAtticObsidian(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_archive_and_attic_never_become_nodes(self):
        _note(self.tmp, "live-one")
        _note(self.tmp / "archive", "archived-one")
        _note(self.tmp / "attic", "attic-one")
        _note(self.tmp / "arch", "still-loaded")  # arch/ is NOT excluded

        nodes = load_nodes(self.tmp)
        self.assertIn("live-one", nodes)
        self.assertIn("still-loaded", nodes)
        self.assertNotIn("archived-one", nodes)
        self.assertNotIn("attic-one", nodes)

    def test_obsidian_json_never_becomes_a_node(self):
        # .obsidian holds .json, not .md, but confirm the exclusion also
        # guards a stray .md dropped there (belt-and-suspenders).
        _note(self.tmp / ".obsidian", "stray")
        nodes = load_nodes(self.tmp)
        self.assertNotIn("stray", nodes)

    def test_nested_archive_under_a_subdir_is_excluded(self):
        # archive/ nested deeper than the wiki root still counts.
        _note(self.tmp / "arch" / "archive", "deep-archived")
        nodes = load_nodes(self.tmp)
        self.assertNotIn("deep-archived", nodes)


if __name__ == "__main__":
    unittest.main()
