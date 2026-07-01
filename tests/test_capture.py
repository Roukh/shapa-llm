"""Tests for shapa.capture: the write path (session -> memory note)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from shapa import capture
from shapa.validate import validate_node


def _make_memory(tmp: Path):
    for t in ("git", "testing", "python", "memory"):
        (tmp / f"{t}.md").write_text(
            "---\n"
            f"id: {t}\ntype: reference\ncreated: \"2026-07-01T00:00:00Z\"\n"
            "consequence: 5\nlocus: output\nuses: 0\n---\nhub [[memory]]\n",
            encoding="utf-8",
        )


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mem = self.tmp / "memory"
        self.mem.mkdir()
        _make_memory(self.mem)
        self.transcript = self.tmp / "t.jsonl"
        self.transcript.write_text(
            json.dumps({"message": {"role": "user", "content": [
                {"type": "text", "text": "fix the git workflow and add python testing"}]}}) + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_writes_valid_memory_note(self):
        path = capture.capture_session(str(self.transcript), "sess1234ab", root=self.mem)
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertTrue(validate_node(path).valid)
        body = path.read_text()
        self.assertIn("type: memory", body)
        self.assertIn("[[memory]]", body)

    def test_links_mentioned_topics(self):
        path = capture.capture_session(str(self.transcript), "sess1234ab", root=self.mem)
        body = path.read_text()
        for topic in ("[[git]]", "[[python]]", "[[testing]]"):
            self.assertIn(topic, body)

    def test_dedup_skips_near_identical(self):
        # A long, distinctive transcript so two sessions produce ~identical bodies.
        long = ("always validate every input at the system boundary, prefer "
                "parameterized queries, and write deterministic isolated tests "
                "before touching legacy code, then refactor in small steps")
        self.transcript.write_text(
            json.dumps({"message": {"role": "user",
                       "content": [{"type": "text", "text": long}]}}) + "\n",
            encoding="utf-8")
        first = capture.capture_session(str(self.transcript), "sessAAAA11", root=self.mem)
        self.assertIsNotNone(first)
        # A different session with the same content should be skipped as a dup.
        second = capture.capture_session(str(self.transcript), "sessBBBB22", root=self.mem)
        self.assertIsNone(second)
        self.assertEqual(len(list(self.mem.glob("memory-session-*.md"))), 1)

    def test_idempotent_preserves_created(self):
        from shapa import frontmatter
        p1 = capture.capture_session(str(self.transcript), "sess1234ab", root=self.mem)
        created1 = frontmatter.parse(p1).meta["created"]
        p2 = capture.capture_session(str(self.transcript), "sess1234ab", root=self.mem)
        created2 = frontmatter.parse(p2).meta["created"]
        self.assertEqual(created1, created2)
        self.assertEqual(len(list(self.mem.glob("memory-session-*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
