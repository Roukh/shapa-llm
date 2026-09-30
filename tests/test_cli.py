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
        # Redirect the persisted pointer, clear any env override, and pin the
        # cwd to an isolated dir so cwd-discovery can't hijack these pointer/
        # default resolution assertions.
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.dict("os.environ", {}, clear=False),
            mock.patch.object(config.Path, "cwd", staticmethod(lambda: self.tmp)),
        ]
        for p in self.patches:
            p.start()
        import os
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def test_init_installs_rules_and_arch_templates(self):
        cli._init([str(self.wiki)])
        self.assertTrue((self.wiki / "AGENTS.md").is_file())
        self.assertTrue((self.wiki / "arch" / "PRD.md").is_file())
        # The bundled docs are the rules + generic project templates, NOT
        # shapa's own design docs (those live in the repo's docs/design/).
        self.assertFalse((self.wiki / "arch" / "MACRO.md").exists())
        # Every installed doc validates against the schema (loads as a node).
        nodes = load_nodes(self.wiki)
        for nid in ("AGENTS", "PRD", "architecture", "system-design"):
            self.assertIn(nid, nodes)

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


class TestUpgradeDocs(unittest.TestCase):
    """Tests for `shapa init --upgrade-docs [DIR]` (installer-hardening
    GAP B): refreshes only AGENTS.md/placement.md from the bundled assets,
    never arch/ templates, never a user/agent-written note."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wiki = self.tmp / "wiki"
        self.cfg = self.tmp / "config.json"
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.dict("os.environ", {}, clear=False),
            mock.patch.object(config.Path, "cwd", staticmethod(lambda: self.tmp)),
        ]
        for p in self.patches:
            p.start()
        import os
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def test_upgrade_docs_overwrites_edited_agents_and_placement(self):
        cli._init([str(self.wiki)])
        agents = self.wiki / "AGENTS.md"
        placement = self.wiki / "placement.md"
        agents.write_text("STALE SCHEMA", encoding="utf-8")
        placement.write_text("STALE PLACEMENT", encoding="utf-8")

        cli._init(["--upgrade-docs", str(self.wiki)])

        self.assertNotEqual(agents.read_text(encoding="utf-8"), "STALE SCHEMA")
        self.assertNotEqual(placement.read_text(encoding="utf-8"), "STALE PLACEMENT")
        self.assertIn("AGENTS.md", agents.read_text(encoding="utf-8"))

    def test_upgrade_docs_never_touches_arch_or_notes(self):
        cli._init([str(self.wiki)])
        prd = self.wiki / "arch" / "PRD.md"
        prd_before = prd.read_text(encoding="utf-8")
        prd.write_text(prd_before + "\nMY OWN NOTES ON THIS PROJECT", encoding="utf-8")
        a_note = self.wiki / "my-memory.md"
        a_note.write_text(
            "---\nid: my-memory\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
            "consequence: 5\nlocus: output\nuses: 0\n---\nsomething the agent wrote\n",
            encoding="utf-8",
        )

        cli._init(["--upgrade-docs", str(self.wiki)])

        self.assertIn("MY OWN NOTES ON THIS PROJECT", prd.read_text(encoding="utf-8"))
        self.assertTrue(a_note.is_file())
        self.assertIn("something the agent wrote", a_note.read_text(encoding="utf-8"))

    def test_upgrade_docs_default_target_is_the_connected_wiki(self):
        cli._init([str(self.wiki)])  # explicit DIR -> becomes the recorded pointer
        agents = self.wiki / "AGENTS.md"
        agents.write_text("STALE", encoding="utf-8")

        cli._init(["--upgrade-docs"])  # no DIR -> config.memory_dir() resolves it

        self.assertNotEqual(agents.read_text(encoding="utf-8"), "STALE")

    def test_upgrade_docs_errors_on_a_directory_that_is_not_a_wiki(self):
        not_a_wiki = self.tmp / "not-a-wiki"
        not_a_wiki.mkdir()
        with self.assertRaises(SystemExit) as ctx:
            cli._init(["--upgrade-docs", str(not_a_wiki)])
        self.assertEqual(ctx.exception.code, 2)
        # Never scaffolds a new wiki as a side effect of the error.
        self.assertFalse((not_a_wiki / "AGENTS.md").exists())

    def test_upgrade_docs_is_idempotent(self):
        cli._init([str(self.wiki)])
        cli._init(["--upgrade-docs", str(self.wiki)])
        cli._init(["--upgrade-docs", str(self.wiki)])  # re-run: no crash, same content
        self.assertTrue((self.wiki / "AGENTS.md").is_file())


class TestConfigResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"
        # mock.patch.dict restores any pre-existing SHAPA_MEMORY on tearDown,
        # instead of permanently popping it from the real environment.
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.dict("os.environ", {}, clear=False),
            mock.patch.object(config.Path, "cwd", staticmethod(lambda: self.tmp)),
        ]
        for p in self.patches:
            p.start()
        import os
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
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

    def _write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_orphan_prune_skips_arch_file_with_no_frontmatter(self):
        # Root-cause regression: AGENTS.md promises "Files in arch/ ... are
        # never pruned" as a PATH guarantee, but a real arch/ note can be
        # committed with no frontmatter at all (no `type: reference` to key
        # off). arch/ location alone must still protect it.
        arch_file = self.tmp / "arch" / "no-frontmatter-note.md"
        self._write(arch_file, "# A note with zero frontmatter\n\nno links at all\n")
        graph = build_graph(load_nodes(self.tmp))
        self.assertNotIn("no-frontmatter-note", heartbeat.find_orphans(graph))
        heartbeat.heartbeat(self.tmp, seed=1)
        self.assertTrue(arch_file.exists())

    def test_orphan_prune_skips_arch_file_with_nested_type(self):
        # Root-cause regression: `type: reference` nested under a `metadata:`
        # block (as some agent-authored notes do) is not parsed as a
        # top-level `type` key by shapa.frontmatter. arch/ location alone
        # must still protect the file, independent of frontmatter shape.
        arch_file = self.tmp / "arch" / "nested-type-note.md"
        self._write(
            arch_file,
            "---\n"
            "name: nested-type-note\n"
            "metadata:\n"
            "  type: reference\n"
            "uses: 0\n"
            "---\n"
            "no links at all\n",
        )
        graph = build_graph(load_nodes(self.tmp))
        self.assertNotIn("nested-type-note", heartbeat.find_orphans(graph))
        heartbeat.heartbeat(self.tmp, seed=1)
        self.assertTrue(arch_file.exists())

    def test_orphan_prune_still_deletes_unlinked_root_note(self):
        # Regression guard: arch/ protection must not become blanket - a
        # linkless root-level note (no type: reference, not under arch/) is
        # still pruned exactly as before.
        root_file = self.tmp / "unlinked-root-note.md"
        self._write(root_file, "no links at all\n")
        graph = build_graph(load_nodes(self.tmp))
        self.assertIn("unlinked-root-note", heartbeat.find_orphans(graph))
        heartbeat.heartbeat(self.tmp, seed=1)
        self.assertFalse(root_file.exists())


if __name__ == "__main__":
    unittest.main()
