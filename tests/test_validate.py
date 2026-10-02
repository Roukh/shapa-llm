"""Tests for shapa.validate: uniform frontmatter-schema validation."""

import shutil
import tempfile
import unittest
from pathlib import Path

from shapa.validate import (
    check_agenda,
    check_lean_shape,
    validate_frontmatter,
    validate_node,
)

FM = Path(__file__).parent / "fixtures" / "frontmatter"


def _note(d: Path, nid: str, body: str = "body text", note_type: str = "memory") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{nid}.md"
    p.write_text(
        "---\n"
        f"id: {nid}\ntype: {note_type}\ncreated: \"2026-01-01T00:00:00Z\"\n"
        "consequence: 5\nlocus: output\nuses: 0\n---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return p


class TestValidate(unittest.TestCase):
    def _rules(self, result):
        return {v.rule for v in result.violations}

    def _severities(self, result):
        return {v.rule: v.severity for v in result.violations}

    def test_valid(self):
        result = validate_node(FM / "valid.md")
        self.assertTrue(result.valid, f"should be valid: {result.violations}")
        self.assertEqual(result.violations, [])

    def test_bad_type(self):
        result = validate_node(FM / "bad-type.md")
        self.assertFalse(result.valid)
        self.assertIn("F02", self._rules(result))

    def test_bad_consequence_and_locus(self):
        result = validate_node(FM / "bad-consequence.md")
        self.assertFalse(result.valid)
        rules = self._rules(result)
        self.assertIn("S01", rules)  # consequence 99
        self.assertIn("S02", rules)  # locus banana

    def test_id_must_match_stem(self):
        result = validate_node(FM / "bad-id.md")
        self.assertFalse(result.valid)
        self.assertIn("F01", self._rules(result))

    def test_missing_uses_is_not_s03(self):
        # shapa-backend-spec.md §10 decision 7: `uses`/`last_used` are
        # tolerated legacy fields now (the live counter lives in the index
        # store) - a note `maintain --backfill` has stripped them from must
        # stay exactly as valid as one that never had them.
        meta = {"id": "n", "type": "memory", "created": "2026-01-01T00:00:00Z",
                "consequence": 5, "locus": "output"}
        violations = validate_frontmatter(meta, "n")
        self.assertNotIn("S03", self._rules_of(violations))

    def test_present_but_malformed_uses_is_still_s03(self):
        # A field that IS present but not a non-negative integer is still a
        # real mistake (hand-authored, or a pre-backfill note mid-migration)
        # - only its ABSENCE is tolerated, not a garbage value.
        meta = {"id": "n", "type": "memory", "created": "2026-01-01T00:00:00Z",
                "consequence": 5, "locus": "output", "uses": "banana"}
        violations = validate_frontmatter(meta, "n")
        self.assertIn("S03", self._rules_of(violations))

    def _rules_of(self, violations):
        return {v.rule for v in violations}

    # ---- Schema v2 (spec §6): F04-F09/S04, warnings-first this release ----

    def test_v1_note_missing_schema_v2_fields_is_still_valid(self):
        """A v1 note with none of the new fields gets F04/F05 warnings but
        stays valid=True - the backward-compat grace period from §6."""
        result = validate_node(FM / "bad-id.md")  # no summary/scope of its own
        rules = self._rules(result)
        self.assertIn("F04", rules)
        self.assertIn("F05", rules)
        severities = self._severities(result)
        self.assertEqual(severities["F04"], "warning")
        self.assertEqual(severities["F05"], "warning")

    def test_bad_summary_too_long_is_warning(self):
        result = validate_node(FM / "schema-v2-bad-summary.md")
        self.assertTrue(result.valid, f"F04 must be a warning, not an error: {result.violations}")
        self.assertIn("F04", self._rules(result))
        self.assertEqual(self._severities(result)["F04"], "warning")

    def test_bad_scope_enum_is_warning(self):
        """'external' is retired (decision 6) - it is now an invalid enum
        value, not a third valid option."""
        result = validate_node(FM / "schema-v2-bad-scope.md")
        self.assertTrue(result.valid)
        self.assertIn("F05", self._rules(result))
        self.assertEqual(self._severities(result)["F05"], "warning")

    def test_scope_mismatched_with_physical_bucket_is_warning(self):
        """scope: global but the file does not resolve under global_root()."""
        result = validate_node(FM / "schema-v2-mismatched-scope.md")
        self.assertTrue(result.valid)
        self.assertIn("F06", self._rules(result))
        self.assertEqual(self._severities(result)["F06"], "warning")
        self.assertNotIn("F05", self._rules(result))  # scope itself is a valid enum value

    def test_scope_matching_physical_bucket_has_no_f06(self):
        result = validate_node(FM / "valid.md")  # scope: repo, lives outside global_root()
        self.assertNotIn("F06", self._rules(result))

    def test_body_over_word_ceiling_is_warning(self):
        result = validate_node(FM / "schema-v2-long-body.md")
        self.assertTrue(result.valid)
        self.assertIn("F07", self._rules(result))
        self.assertEqual(self._severities(result)["F07"], "warning")

    def test_body_under_ceiling_has_no_f07(self):
        result = validate_node(FM / "valid.md")
        self.assertNotIn("F07", self._rules(result))

    def test_ideas_and_checklist_are_exempt_from_f07(self):
        # Both are append-only/ever-growing convention files by design -
        # ideas.md a dated log, checklist.md a running `- [ ] item` list -
        # so neither is ever flagged for passing the ordinary word ceiling.
        long_body = " ".join(f"word{i}" for i in range(400))
        for stem in ("ideas", "checklist"):
            meta = {"id": stem, "type": "memory", "created": "2026-01-01T00:00:00Z",
                    "consequence": 5, "locus": "meta", "uses": 0}
            violations = validate_frontmatter(meta, stem, body=long_body)
            self.assertNotIn("F07", {v.rule for v in violations}, stem)

    def test_dangling_supersedes_is_warning_when_known_ids_supplied(self):
        result = validate_node(FM / "schema-v2-dangling-supersedes.md", known_ids={"some-other-id"})
        self.assertTrue(result.valid)
        self.assertIn("F08", self._rules(result))
        self.assertEqual(self._severities(result)["F08"], "warning")

    def test_dangling_supersedes_skipped_without_known_ids(self):
        """No known_ids supplied -> F08 can't be checked, so it is skipped
        rather than guessed at (a single arbitrary file has no way to know
        what else exists)."""
        result = validate_node(FM / "schema-v2-dangling-supersedes.md")
        self.assertNotIn("F08", self._rules(result))

    def test_supersedes_existing_id_has_no_f08(self):
        result = validate_node(
            FM / "schema-v2-dangling-supersedes.md",
            known_ids={"this-id-does-not-exist-anywhere"},
        )
        self.assertNotIn("F08", self._rules(result))

    def test_bad_status_enum_is_warning(self):
        result = validate_node(FM / "schema-v2-bad-status.md")
        self.assertTrue(result.valid)
        self.assertIn("S04", self._rules(result))
        self.assertEqual(self._severities(result)["S04"], "warning")

    def test_default_status_absent_has_no_s04(self):
        result = validate_node(FM / "valid.md")  # no status field at all
        self.assertNotIn("S04", self._rules(result))

    def test_no_schema_v2_checks_without_optional_context(self):
        """The v1 call signature (meta + stem only) still runs exactly the
        core checks - no path/body/known_ids means F06/F07/F08 can't fire."""
        meta = {
            "id": "x", "type": "memory", "created": "2026-01-01T00:00:00Z",
            "consequence": 5, "locus": "output", "uses": 0,
        }
        violations = validate_frontmatter(meta, "x")
        rules = {v.rule for v in violations}
        self.assertNotIn("F06", rules)
        self.assertNotIn("F07", rules)
        self.assertNotIn("F08", rules)


class TestLeanShape(unittest.TestCase):
    """F10/F11: lean wiki shape (shapa-backend-spec.md §10 decision 6)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_within_every_limit_is_clean(self):
        _note(self.tmp, "agenda", "1. one fire\n")
        _note(self.tmp, "a-note")
        self.assertEqual(check_lean_shape(self.tmp), [])
        self.assertEqual(check_agenda(self.tmp), [])

    def test_convention_files_dont_count_toward_the_root_cap(self):
        # agenda.md/ideas.md/checklist.md recur at every wiki root by
        # construction (shapa.nodes.CONVENTION_IDS) - they are not the
        # "note count creeping up" the 40-note cap exists to catch, the
        # same way a type: reference doc at the root already isn't.
        _note(self.tmp, "agenda", "1. one fire\n")
        _note(self.tmp, "ideas", "2026-01-01 an idea worth keeping\n")
        _note(self.tmp, "checklist", "- [ ] **X1** do the thing - verify: `true`\n")
        for i in range(3):
            _note(self.tmp, f"n{i}")
        violations = check_lean_shape(self.tmp, max_root_notes=3)
        self.assertEqual(violations, [])

    def test_too_many_root_notes_is_f10(self):
        for i in range(5):
            _note(self.tmp, f"n{i}")
        violations = check_lean_shape(self.tmp, max_root_notes=4)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "F10")
        self.assertIn("root notes", violations[0].message)

    def test_too_many_arch_notes_is_f10(self):
        for i in range(5):
            _note(self.tmp / "arch", f"a{i}", note_type="reference")
        violations = check_lean_shape(self.tmp, max_arch_notes=4)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "F10")
        self.assertIn("arch/", violations[0].message)

    def test_live_kb_over_limit_is_f10(self):
        _note(self.tmp, "big", "x" * 2000)
        violations = check_lean_shape(self.tmp, max_live_kb=1)
        self.assertEqual(len(violations), 1)
        self.assertIn("KB", violations[0].message)

    def test_reference_docs_at_root_are_exempt_from_the_root_count(self):
        # AGENTS.md/placement.md/agenda.md-style schema docs (type:
        # reference) at the wiki root don't count toward the 40 live-note
        # limit - only memory/rule/issue do.
        for i in range(3):
            _note(self.tmp, f"ref{i}", note_type="reference")
        violations = check_lean_shape(self.tmp, max_root_notes=2)
        self.assertEqual(violations, [])

    def test_archive_and_attic_never_count_toward_any_limit(self):
        # Even wildly over any limit, archive/attic contents are invisible
        # to F10 entirely - GAP A's exclusion, not a separate carve-out.
        for i in range(50):
            _note(self.tmp / "archive", f"old{i}", "x" * 10000)
        for i in range(50):
            _note(self.tmp / "attic", f"scratch{i}", "x" * 10000)
        violations = check_lean_shape(self.tmp, max_root_notes=1, max_arch_notes=1, max_live_kb=1)
        self.assertEqual(violations, [])

    def test_missing_agenda_is_f11(self):
        violations = check_agenda(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "F11")
        self.assertIn("missing", violations[0].message)

    def test_agenda_with_exactly_three_items_is_clean(self):
        _note(self.tmp, "agenda", "1. one\n2. two\n3. three\n")
        self.assertEqual(check_agenda(self.tmp), [])

    def test_agenda_with_too_many_items_is_f11(self):
        _note(self.tmp, "agenda", "1. one\n2. two\n3. three\n4. four\n")
        violations = check_agenda(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "F11")
        self.assertIn("4", violations[0].message)

    def test_agenda_indented_sub_items_dont_count_as_top_level(self):
        _note(self.tmp, "agenda", "1. one\n   - a sub-point\n2. two\n3. three\n")
        self.assertEqual(check_agenda(self.tmp), [])

    def test_nonexistent_root_reports_nothing(self):
        # No wiki initialized yet is "no wiki," not "a shape violation."
        missing = self.tmp / "does-not-exist"
        self.assertEqual(check_lean_shape(missing), [])
        self.assertEqual(check_agenda(missing), [])


class TestCliOnADirectory(unittest.TestCase):
    """`shapa validate DIR` validates the wiki's live notes - it used to hand
    the directory itself to validate_node and crash (IsADirectoryError)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_directory_argument_validates_its_notes(self):
        import io
        from contextlib import redirect_stdout

        from shapa import validate

        _note(self.tmp, "agenda", "1. the one fire")
        _note(self.tmp, "a-note")
        _note(self.tmp / "archive", "retired", note_type="bogus")
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as exc:
            validate.main([str(self.tmp)])
        self.assertEqual(exc.exception.code, 0)
        out = buf.getvalue()
        self.assertIn("a-note.md", out)
        self.assertNotIn("retired.md", out)


class TestManagedDocsSkipped(unittest.TestCase):
    """AGENTS.md/placement.md are shipped and rewritten by shapa itself, so
    `shapa validate` never note-validates them (no F04/F05/F07 warnings on a
    freshly scaffolded wiki's own schema docs)."""

    def setUp(self):
        from unittest import mock

        from shapa import config

        self.tmp = Path(tempfile.mkdtemp())
        self.wiki = self.tmp / "wiki"
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.tmp / "config.json"),
            mock.patch.object(config.Path, "cwd", staticmethod(lambda: self.tmp)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def _run(self, argv):
        import io
        from contextlib import redirect_stdout

        from shapa import validate

        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as exc:
            validate.main(argv)
        return exc.exception.code, buf.getvalue()

    def _init(self):
        import io
        from contextlib import redirect_stdout

        from shapa import cli

        with redirect_stdout(io.StringIO()):
            cli._init([str(self.wiki)])

    @staticmethod
    def _blocks(out: str) -> dict[str, list[str]]:
        """``{file header line: [its indented violation lines]}``."""
        blocks: dict[str, list[str]] = {}
        current = None
        for ln in out.splitlines():
            if ln.startswith("  [") and current is not None:
                blocks[current].append(ln)
            else:
                current = ln
                blocks[current] = []
        return blocks

    def _managed_blocks(self, out: str) -> dict[str, list[str]]:
        return {head: v for head, v in self._blocks(out).items()
                if "/AGENTS.md:" in head or "/placement.md:" in head}

    def test_fresh_wiki_schema_docs_raise_no_warnings(self):
        self._init()
        code, out = self._run([str(self.wiki)])
        self.assertEqual(code, 0)
        managed = self._managed_blocks(out)
        self.assertEqual(len(managed), 2, out)
        for head, violations in managed.items():
            self.assertIn("SKIPPED", head)
            self.assertEqual(violations, [])
        # The shipped AGENTS.md really would warn if it were note-validated.
        self.assertTrue(validate_node(self.wiki / "AGENTS.md").warnings)

    def test_named_managed_doc_is_skipped(self):
        self._init()
        code, out = self._run([str(self.wiki / "AGENTS.md")])
        self.assertEqual(code, 0)
        self.assertIn("SKIPPED", out)
        self.assertNotIn("WARNING", out)

    def test_all_roots_skips_managed_docs(self):
        from shapa import config

        self._init()
        config.set_memory_dir(self.wiki)
        _code, out = self._run(["--all-roots", str(self.tmp)])
        managed = self._managed_blocks(out)
        self.assertEqual(len(managed), 2, out)
        for head, violations in managed.items():
            self.assertIn("SKIPPED", head)
            self.assertEqual(violations, [])

    def test_same_name_outside_a_wiki_is_still_validated(self):
        loose = _note(self.tmp / "not-a-wiki", "placement", note_type="bogus")
        code, out = self._run([str(loose)])
        self.assertEqual(code, 1)
        self.assertIn("[F02]", out)


if __name__ == "__main__":
    unittest.main()
