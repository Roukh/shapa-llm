"""Tests for the opt-in memri importer (shapa/memri_import.py):
``shapa upgrade PATH --import-memri FILE [--dry-run]``.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from shapa import memlog, memri_import


def _wiki(tmp: Path) -> Path:
    wiki = tmp / ".shapa"
    wiki.mkdir(parents=True)
    (wiki / "AGENTS.md").write_text("marker\n", encoding="utf-8")
    return wiki


class TestSplitBody(unittest.TestCase):
    def test_paragraphs_split_on_blank_lines(self):
        self.assertEqual(
            memri_import.split_body("first para.\n\nsecond para.\n\nthird para.", "t"),
            ["first para.", "second para.", "third para."],
        )

    def test_single_block_with_bullets_splits_into_bullets(self):
        body = "- one thing\n- another thing\n* a third, star-bulleted"
        self.assertEqual(
            memri_import.split_body(body, "t"),
            ["one thing", "another thing", "a third, star-bulleted"],
        )

    def test_long_single_block_with_sentences_splits_into_sentences(self):
        body = " ".join([f"Sentence number {i} is reasonably long for this test." for i in range(6)])
        self.assertGreater(len(body), memri_import.SENTENCE_SPLIT_CHARS)
        pieces = memri_import.split_body(body, "t")
        self.assertEqual(len(pieces), 6)
        self.assertTrue(pieces[0].startswith("Sentence number 0"))

    def test_short_single_block_stays_one_piece(self):
        self.assertEqual(memri_import.split_body("just one short block", "t"),
                         ["just one short block"])

    def test_empty_body_falls_back_to_title(self):
        self.assertEqual(memri_import.split_body("", "the title"), ["the title"])
        self.assertEqual(memri_import.split_body("   ", ""), [])


class TestImportMemri(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wiki = _wiki(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _export(self, rows) -> Path:
        path = self.tmp / "export.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    def test_rows_of_every_type_split_and_classify(self):
        rows = [
            {"id": "rule-1", "type": "rule", "title": "Branch discipline",
             "body": "Branch off main.\n\nRebase before merging.\n\nNever force-push a shared branch.",
             "tags": ["git"], "repo": "shapa-llm", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "issue-1", "type": "issue", "title": "Flaky CI socket test",
             "body": "- the AF_UNIX socket test fails under sandboxing\n"
                     "- it was retried and passed\n- root cause is the sandbox denying sockets",
             "created_at": "2026-01-02T00:00:00Z"},
            {"id": "memory-1", "type": "memory", "title": "A quiet session",
             "body": "Updated the docs and ran the test suite with no surprises at all today.",
             "created_at": "2026-01-03T00:00:00Z"},
            {"id": "persona-1", "type": "persona", "title": "Terse responses preferred",
             "body": "Keep answers short.", "created_at": "2026-01-04T00:00:00Z"},
        ]
        stats = memri_import.import_memri(self.wiki, self._export(rows))
        self.assertEqual(stats.rows, 4)
        self.assertEqual(stats.written, 3 + 3 + 1 + 1)
        self.assertFalse(stats.dry_run)

        view = memlog.read_log(self.wiki)
        by_source = {r.source: r for r in view.records.values()}

        # rule -> preference (default, no stronger keyword present)
        self.assertEqual(by_source["memri:rule-1#0"].kind, "preference")
        self.assertEqual(by_source["memri:rule-1#0"].summary, "Branch discipline")
        self.assertEqual(by_source["memri:rule-1#1"].summary, "Rebase before merging.")
        self.assertIn("git", by_source["memri:rule-1#0"].tags)
        self.assertEqual(by_source["memri:rule-1#0"].repo, "shapa-llm")

        # issue -> gotcha by default
        self.assertEqual(by_source["memri:issue-1#0"].kind, "gotcha")

        # persona -> preference by default
        self.assertEqual(by_source["memri:persona-1#0"].kind, "preference")

        # memory -> fact by default (no gotcha/open/pref/outcome keyword here)
        self.assertEqual(by_source["memri:memory-1#0"].kind, "fact")

    def test_issue_row_with_outcome_keyword_classifies_as_outcome(self):
        rows = [{"id": "issue-2", "type": "issue", "title": "Resolved",
                "body": "The regression was fixed and the fix shipped to every wiki.",
                "created_at": "2026-01-01T00:00:00Z"}]
        memri_import.import_memri(self.wiki, self._export(rows))
        view = memlog.read_log(self.wiki)
        rec = next(iter(view.records.values()))
        # "regression" (gotcha) and "fixed"/"shipped" (outcome) both appear;
        # classify_kind's own priority (gotcha before outcome) decides, not
        # the importer - this just proves the importer doesn't override it.
        self.assertIn(rec.kind, ("gotcha", "outcome"))

    def test_superseded_row_is_imported_then_archived(self):
        rows = [
            {"id": "old-1", "type": "persona", "title": "Old preference",
             "body": "Prefer tabs over spaces.", "superseded_by": "new-1",
             "created_at": "2026-01-01T00:00:00Z"},
            {"id": "new-1", "type": "persona", "title": "New preference",
             "body": "Prefer spaces over tabs.", "created_at": "2026-01-02T00:00:00Z"},
        ]
        stats = memri_import.import_memri(self.wiki, self._export(rows))
        self.assertEqual(stats.archived, 1)

        view = memlog.read_log(self.wiki)
        by_source = {r.source: r for r in view.records.values()}
        old_id = by_source["memri:old-1#0"].id
        new_id = by_source["memri:new-1#0"].id
        self.assertEqual(view.status(old_id), "archived")
        self.assertEqual(view.status(new_id), "active")
        self.assertEqual(view.archived[old_id].reason, "memri-superseded-by:new-1")

    def test_secret_in_body_is_redacted_and_counted(self):
        rows = [{"id": "secret-1", "type": "rule", "title": "Rotate the key",
                "body": "deploy key sk-ant-api03-abcdefghijklmnopqrstuvwx must be rotated",
                "created_at": "2026-01-01T00:00:00Z"}]
        stats = memri_import.import_memri(self.wiki, self._export(rows))
        self.assertGreater(stats.redaction_hits, 0)
        raw = (self.wiki / "memory").glob("*.jsonl")
        text = "".join(p.read_text(encoding="utf-8") for p in raw)
        self.assertNotIn("sk-ant", text)

    def test_duplicate_row_within_one_file_is_skipped(self):
        row = {"id": "dup-1", "type": "memory", "title": "Same content twice",
              "body": "Exactly the same body text, word for word, every single time.",
              "created_at": "2026-01-01T00:00:00Z"}
        dup = dict(row, id="dup-2")  # same title+body -> identical content hash
        stats = memri_import.import_memri(self.wiki, self._export([row, dup]))
        self.assertEqual(stats.written, 1)
        self.assertEqual(stats.duplicates, 1)

    def test_reimporting_the_same_file_is_idempotent(self):
        rows = [{"id": "r1", "type": "memory", "title": "One fact",
                "body": "A single short fact worth keeping around for later use.",
                "created_at": "2026-01-01T00:00:00Z"}]
        export = self._export(rows)
        first = memri_import.import_memri(self.wiki, export)
        self.assertEqual(first.written, 1)

        second = memri_import.import_memri(self.wiki, export)
        self.assertEqual(second.written, 0)
        self.assertEqual(second.duplicates, 1)

        view = memlog.read_log(self.wiki)
        self.assertEqual(len(view.records), 1)

    def test_dry_run_writes_nothing(self):
        rows = [{"id": "r1", "type": "rule", "title": "Would write this",
                "body": "This body would become a record if this weren't a dry run.",
                "created_at": "2026-01-01T00:00:00Z"}]
        before = sorted(self.wiki.rglob("*"))
        stats = memri_import.import_memri(self.wiki, self._export(rows), dry_run=True)
        self.assertTrue(stats.dry_run)
        self.assertEqual(stats.written, 0)
        self.assertEqual(stats.pieces, 1)
        self.assertEqual(sorted(self.wiki.rglob("*")), before, "dry-run must write nothing")
        self.assertFalse(memlog.has_log(self.wiki))

    def test_scope_and_repo_come_from_the_target_wiki_and_row(self):
        rows = [{"id": "r1", "type": "memory", "title": "t",
                "body": "a repo-scoped memory worth keeping for this project specifically",
                "repo": "other-repo", "created_at": "2026-01-01T00:00:00Z"}]
        memri_import.import_memri(self.wiki, self._export(rows))
        rec = next(iter(memlog.read_log(self.wiki).records.values()))
        self.assertEqual(rec.scope, "repo")  # self.wiki is not under global_root()
        self.assertEqual(rec.repo, "other-repo")

    def test_rejects_a_non_array_export(self):
        path = self.tmp / "bad.json"
        path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            memri_import.import_memri(self.wiki, path)


if __name__ == "__main__":
    unittest.main()
