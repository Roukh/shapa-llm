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
        # markdown wiki nodes only. This is the dry-run half of the pair below.
        r = maintain.maintain(self.tmp, prune=True, max_age_days=1, now=NOW, dry_run=True)
        self.assertTrue(self.sidecar.exists(), "non-md sidecar was deleted by --prune")
        self.assertNotIn("adr-constraints", r["pruned"])
        self.assertNotIn("adr-constraints.json", r["pruned"])
        self.assertTrue(r["dry_run"])

    def test_prune_never_deletes_a_non_md_sidecar_non_dry_run_real_files(self):
        # Belt-and-braces: same assertion via the real (non-dry-run) unlink path,
        # with every OTHER stale/orphaned file actually pruned (used-old.md
        # survives separately, on its own merit — it has uses=3), so this proves
        # survival is per-file selection, not an accidental global no-op.
        r = maintain.maintain(self.tmp, prune=True, max_age_days=1, now=NOW, dry_run=False)
        self.assertIn("lonely", r["pruned"])  # sanity: real pruning did happen
        self.assertTrue(self.sidecar.exists())
        self.assertEqual(
            self.sidecar.read_text(encoding="utf-8"),
            '{"id": "adr-fitness", "kind": "path", "pattern": "^legacy/"}\n',
        )


def _plain_note(d, nid, status=None, note_type="memory", body="body text"):
    lines = [
        "---", f"id: {nid}", f"type: {note_type}",
        'created: "2026-01-01T00:00:00Z"', "consequence: 5", "locus: output", "uses: 0",
    ]
    if status:
        lines.append(f"status: {status}")
    lines.append("---")
    (d / f"{nid}.md").write_text("\n".join(lines) + f"\n{body}\n", encoding="utf-8")


class TestLeanMaintenance(unittest.TestCase):
    """``shapa maintain --lean``/``--apply`` (shapa-backend-spec.md §10
    decision 6): report F10/F11 + archive status:superseded notes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_find_superseded_lists_only_superseded_live_notes(self):
        _plain_note(self.tmp, "live-active", status="active")
        _plain_note(self.tmp, "live-superseded", status="superseded")
        _plain_note(self.tmp, "no-status")
        nodes = load_nodes(self.tmp)
        self.assertEqual(maintain.find_superseded(nodes), ["live-superseded"])

    def test_lean_report_never_moves_anything(self):
        _plain_note(self.tmp, "old-note", status="superseded")
        report = maintain.lean_report(self.tmp)
        self.assertEqual(report["superseded"], ["old-note"])
        self.assertTrue((self.tmp / "old-note.md").exists())
        self.assertFalse((self.tmp / "archive").exists())

    def test_apply_lean_moves_superseded_notes_into_archive_and_deletes_nothing(self):
        _plain_note(self.tmp, "old-note", status="superseded", body="the actual content")
        _plain_note(self.tmp, "current-note", status="active")

        moved = maintain.apply_lean(self.tmp)

        self.assertEqual([nid for nid, _ in moved], ["old-note"])
        self.assertFalse((self.tmp / "old-note.md").exists())
        dest = self.tmp / "archive" / "old-note.md"
        self.assertTrue(dest.exists())
        self.assertIn("the actual content", dest.read_text(encoding="utf-8"))
        # Never touched a non-superseded note.
        self.assertTrue((self.tmp / "current-note.md").exists())

    def test_apply_lean_falls_back_to_a_plain_move_outside_a_git_checkout(self):
        # This tmp dir is not a git checkout - git mv must fail cleanly and
        # archive_note must still succeed via shutil.move.
        _plain_note(self.tmp, "old-note", status="superseded")
        dest = maintain.archive_note(self.tmp / "old-note.md", self.tmp / "archive")
        self.assertEqual(dest, self.tmp / "archive" / "old-note.md")
        self.assertTrue(dest.exists())
        self.assertFalse((self.tmp / "old-note.md").exists())

    def test_apply_lean_never_overwrites_an_already_archived_note(self):
        archive_dir = self.tmp / "archive"
        archive_dir.mkdir()
        (archive_dir / "old-note.md").write_text("previously archived\n", encoding="utf-8")
        _plain_note(self.tmp, "old-note", status="superseded", body="new content")

        moved = maintain.apply_lean(self.tmp)

        self.assertEqual(len(moved), 1)
        dest = moved[0][1]
        self.assertNotEqual(dest, archive_dir / "old-note.md")
        self.assertEqual((archive_dir / "old-note.md").read_text(encoding="utf-8"),
                          "previously archived\n")
        self.assertIn("new content", dest.read_text(encoding="utf-8"))

    def test_apply_lean_is_idempotent_when_nothing_superseded(self):
        _plain_note(self.tmp, "current-note", status="active")
        self.assertEqual(maintain.apply_lean(self.tmp), [])

    def test_apply_lean_does_not_re_archive_already_archived_notes(self):
        # A note living under archive/ is never loaded (GAP A), so it can
        # never appear in find_superseded's input in the first place.
        archive_dir = self.tmp / "archive"
        archive_dir.mkdir()
        _plain_note(archive_dir, "already-gone", status="superseded")
        self.assertEqual(maintain.apply_lean(self.tmp), [])

    def test_cli_lean_reports_violations_without_apply(self):
        with self.assertRaises(SystemExit) as cm:
            maintain.main(["--lean", str(self.tmp)])
        self.assertEqual(cm.exception.code, 0)
        # F11: agenda.md is missing from this bare tmp dir.
        self.assertFalse((self.tmp / "archive").exists())

    def test_cli_lean_apply_archives_superseded_notes(self):
        _plain_note(self.tmp, "old-note", status="superseded")
        with self.assertRaises(SystemExit):
            maintain.main(["--lean", "--apply", str(self.tmp)])
        self.assertTrue((self.tmp / "archive" / "old-note.md").exists())
        self.assertFalse((self.tmp / "old-note.md").exists())


class TestBackfillLegacyUses(unittest.TestCase):
    """``shapa maintain --backfill`` (GAP E, shapa-backend-spec.md §10
    decision 7): strip the legacy `uses`/`last_used` frontmatter lines -
    those counters live in the index store now, never in the file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_strips_uses_and_last_used_leaves_everything_else_intact(self):
        (self.tmp / "legacy.md").write_text(
            "---\nid: legacy\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
            "consequence: 5\nlocus: output\nuses: 7\nlast_used: \"2026-06-01T00:00:00Z\"\n"
            "---\nsome body text with [[legacy-link]]\n",
            encoding="utf-8",
        )
        changed = maintain.backfill_strip_legacy_uses(self.tmp)
        self.assertEqual([nid for nid, _ in changed], ["legacy"])
        text = (self.tmp / "legacy.md").read_text(encoding="utf-8")
        self.assertNotIn("uses:", text)
        self.assertNotIn("last_used:", text)
        self.assertIn("id: legacy", text)
        self.assertIn("consequence: 5", text)
        self.assertIn("some body text with [[legacy-link]]", text)

    def test_note_with_neither_field_is_never_rewritten(self):
        p = self.tmp / "clean.md"
        p.write_text(
            "---\nid: clean\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
            "consequence: 5\nlocus: output\n---\nbody\n",
            encoding="utf-8",
        )
        before = p.stat().st_mtime_ns
        before_bytes = p.read_bytes()
        changed = maintain.backfill_strip_legacy_uses(self.tmp)
        self.assertEqual(changed, [])
        self.assertEqual(p.stat().st_mtime_ns, before, "a clean note must never be rewritten")
        self.assertEqual(p.read_bytes(), before_bytes)

    def test_is_idempotent(self):
        _plain_note(self.tmp, "a")  # _plain_note writes uses: 0, no last_used
        first = maintain.backfill_strip_legacy_uses(self.tmp)
        self.assertEqual([nid for nid, _ in first], ["a"])
        second = maintain.backfill_strip_legacy_uses(self.tmp)
        self.assertEqual(second, [])

    def test_never_touches_archive_or_attic(self):
        archive_dir = self.tmp / "archive"
        archive_dir.mkdir()
        _plain_note(archive_dir, "archived")
        before = (archive_dir / "archived.md").read_bytes()
        maintain.backfill_strip_legacy_uses(self.tmp)
        self.assertEqual((archive_dir / "archived.md").read_bytes(), before)

    def test_backfilled_note_still_validates(self):
        # shapa-backend-spec.md §10 decision 7: uses/last_used are tolerated
        # legacy, so a backfilled note must not newly fail validation.
        from shapa.validate import validate_node
        _plain_note(self.tmp, "a")
        maintain.backfill_strip_legacy_uses(self.tmp)
        result = validate_node(self.tmp / "a.md")
        self.assertTrue(result.valid, result.violations)

    def test_cli_backfill_reports_and_strips(self):
        _plain_note(self.tmp, "a")
        with self.assertRaises(SystemExit) as cm:
            maintain.main(["--backfill", str(self.tmp)])
        self.assertEqual(cm.exception.code, 0)
        self.assertNotIn("uses:", (self.tmp / "a.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
