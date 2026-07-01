"""Tests for `shapa init` (connect a wiki + install design docs), the memory
pointer in shapa.config, and the maintainer's protection of the arch cluster.
"""

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from shapa import cli, config, heartbeat, maintain
from shapa.nodes import build_graph, load_nodes

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


class TestInit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wiki = self.tmp / "wiki"
        self.cfg = self.tmp / "config.json"
        # Redirect the persisted pointer and clear any env override.
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.dict("os.environ", {}, clear=False),
        ]
        for p in self.patches:
            p.start()
        import os
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def test_init_installs_design_docs(self):
        cli._init([str(self.wiki)])
        self.assertTrue((self.wiki / "AGENTS.md").is_file())
        self.assertTrue((self.wiki / "arch" / "MACRO.md").is_file())
        # Every installed doc validates against the schema (loads as a node).
        nodes = load_nodes(self.wiki)
        self.assertIn("MACRO", nodes)
        self.assertIn("AGENTS", nodes)

    def test_init_records_pointer_and_where_reads_it(self):
        cli._init([str(self.wiki)])
        self.assertTrue(self.cfg.is_file())
        # With no env override, memory_dir resolves to the connected wiki.
        self.assertEqual(config.memory_dir(), self.wiki.resolve())

    def test_env_overrides_pointer(self):
        cli._init([str(self.wiki)])
        other = self.tmp / "other"
        import os
        with mock.patch.dict(os.environ, {config.ENV_VAR: str(other)}):
            self.assertEqual(config.memory_dir(), other)

    def test_init_is_idempotent_and_preserves_edits(self):
        cli._init([str(self.wiki)])
        agents = self.wiki / "AGENTS.md"
        agents.write_text("EDITED", encoding="utf-8")
        cli._init([str(self.wiki)])  # re-run
        self.assertEqual(agents.read_text(encoding="utf-8"), "EDITED")


class TestConfigResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"
        self.patch = mock.patch.object(config, "CONFIG_FILE", self.cfg)
        self.patch.start()
        import os
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp)

    def test_malformed_pointer_falls_through_to_default(self):
        # A non-string "memory" value must not crash; fall back to the default.
        self.cfg.write_text('{"memory": 123}', encoding="utf-8")
        self.assertEqual(config.memory_dir(), config.DEFAULT_DIR)
        self.cfg.write_text("not json at all", encoding="utf-8")
        self.assertEqual(config.memory_dir(), config.DEFAULT_DIR)

    def test_resolve_expands_user(self):
        # A ~-prefixed --root must be expanded, not treated as a literal dir.
        resolved = config.resolve("~/some-wiki")
        self.assertFalse(str(resolved).startswith("~"))
        self.assertTrue(str(resolved).endswith("some-wiki"))


class TestArchProtection(unittest.TestCase):
    """The maintainer never prunes/merges a `type: reference` doc."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _ref(self, nid, created, body=""):
        (self.tmp / f"{nid}.md").write_text(
            "---\n"
            f"id: {nid}\ntype: reference\ncreated: \"{created}\"\n"
            f"consequence: 6\nlocus: output\nuses: 0\n---\n{body}\n",
            encoding="utf-8",
        )

    def test_orphan_prune_skips_reference(self):
        # A reference with no links is NOT an orphan.
        self._ref("lonely-arch", "2026-06-25T00:00:00Z", "no links at all")
        graph = build_graph(load_nodes(self.tmp))
        self.assertNotIn("lonely-arch", heartbeat.find_orphans(graph))

    def test_stale_prune_skips_reference(self):
        # An old, unused reference is NOT stale.
        self._ref("old-arch", "2020-01-01T00:00:00Z", "ancient design doc")
        stale = maintain.find_stale(load_nodes(self.tmp), NOW, max_age_days=90)
        self.assertNotIn("old-arch", stale)


if __name__ == "__main__":
    unittest.main()
