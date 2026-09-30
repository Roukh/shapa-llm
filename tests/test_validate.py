"""Tests for shapa.validate: uniform frontmatter-schema validation."""

import unittest
from pathlib import Path

from shapa.validate import validate_frontmatter, validate_node

FM = Path(__file__).parent / "fixtures" / "frontmatter"


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


if __name__ == "__main__":
    unittest.main()
