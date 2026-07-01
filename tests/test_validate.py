"""Tests for shapa.validate: uniform frontmatter-schema validation."""

import unittest
from pathlib import Path

from shapa.validate import validate_node

FM = Path(__file__).parent / "fixtures" / "frontmatter"


class TestValidate(unittest.TestCase):
    def _rules(self, result):
        return {v.rule for v in result.violations}

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


if __name__ == "__main__":
    unittest.main()
