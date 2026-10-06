"""Format 4's memory/rule/issue rows: corrections become issues, operator
statements become memories, and every read surface sees the rows."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from shapa import bootstrap, capture, config, db, fetch, get, ledger, mcp, save


def msg(role: str, text: str) -> str:
    return json.dumps({"message": {"role": role, "content": [{"type": "text", "text": text}]}})


class WikiCase(unittest.TestCase):
    """A global wiki and one repo wiki, both format 4, isolated pointer."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.global_wiki = self.tmp / "global"
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.wiki = self.repo / ".shapa"
        for w in (self.global_wiki, self.wiki):
            w.mkdir(parents=True)
            (w / "AGENTS.md").write_text("# marker\n", encoding="utf-8")
            db.connect(w, create=True).close()
        pointer = self.tmp / "config.json"
        pointer.write_text(json.dumps({"memory": str(self.global_wiki)}), encoding="utf-8")
        self.patches = [mock.patch.object(config, "CONFIG_FILE", pointer)]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conn(self, wiki=None):
        return db.connect(wiki or self.wiki)


class TestCorrections(WikiCase):
    CORRECTIONS = [
        "no, that's not it",
        "No. Use the other table.",
        "wrong file",
        "what the fuck did you just delete",
        "not like this, put it in the repo wiki",
        "not quite my tempo",
        "I said keep it in one file",
        "you didn't run the tests",
        "why did you push to main?",
        "stop",
    ]
    NOT_CORRECTIONS = [
        "add a ledger row for the trials",
        "now build the hook",
        "nothing else to add, ship it",
        "notes go in research",
        "known issue: the socket tests need a real shell",
    ]

    def test_cue_words(self):
        for text in self.CORRECTIONS:
            self.assertTrue(ledger.is_correction(text), text)
        for text in self.NOT_CORRECTIONS:
            self.assertFalse(ledger.is_correction(text), text)

    def test_a_correction_becomes_an_issue_linked_to_claimed_work(self):
        conn = self.conn()
        f1 = db.add_item(conn, "F", "storage")
        j1 = db.add_item(conn, "J", "schema", parent=f1)
        db.claim(conn, j1, "s1")
        conn.close()
        transcript = self.tmp / "t.jsonl"
        transcript.write_text("\n".join([msg("user", "build it"),
                                         msg("assistant", "I put the table in the global wiki.")]) + "\n")
        hit = ledger.record_correction("No, wrong wiki. Tables go in the repo wiki.", cwd=self.repo,
                                       session="s1abcdef", transcript_path=str(transcript))
        self.assertIsNotNone(hit)
        root, rid = hit
        self.assertEqual(root, self.wiki.resolve())
        conn = self.conn()
        try:
            row = db.get_row(conn, rid)
            self.assertEqual(row.kind, "I")
            self.assertEqual(row.summary, "No, wrong wiki.")
            self.assertIn("Agent before: I put the table in the global wiki.", row.body)
            self.assertIn((rid, j1, "about"), db.links_of(conn, rid))
            self.assertEqual([r.id for _, r in db.related_issues(conn, f1)], [rid])
        finally:
            conn.close()

    def test_hook_entry_is_silent_and_never_fails(self):
        buf = io.StringIO()
        stdin = io.StringIO(json.dumps({"prompt": "add the hook", "cwd": str(self.repo)}))
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(buf):
            self.assertEqual(ledger.correction_main([]), 0)
        self.assertEqual(buf.getvalue(), "")
        stdin = io.StringIO(json.dumps({"prompt": "wrong, not like this", "cwd": str(self.repo)}))
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(buf):
            self.assertEqual(ledger.correction_main([]), 0)
        self.assertIn("logged this correction as issue I1", buf.getvalue())


class TestRelatedIssues(WikiCase):
    def test_link_beats_tag_beats_nothing(self):
        conn = self.conn()
        try:
            f1 = db.add_item(conn, "F", "hooks")
            j1 = db.add_item(conn, "J", "post-commit", parent=f1)
            db.tag(conn, j1, ["scripts/hooks"])
            linked, _ = db.add_row(conn, "I", "closed the wrong job", about=[f1])
            tagged, _ = db.add_row(conn, "I", "hook path quoting broke", tags=["scripts/hooks"])
            db.add_row(conn, "I", "unrelated mistake", tags=["frontend"])
            ranked = [r.id for _, r in db.related_issues(conn, j1)]
            self.assertEqual(ranked, [linked, tagged])
        finally:
            conn.close()


class TestCaptureRouting(WikiCase):
    def test_operator_statements_become_memories_and_reports_do_not(self):
        t = self.tmp / "t.jsonl"
        t.write_text("\n".join([
            msg("user", "the task brief is never stored"),
            msg("user", "From now on always keep scrap notes under the feature."),
            msg("user", "No, wrong place, never put them in the root."),
            msg("assistant", "Moved the notes.\n\nFootprint: temp/.\n\nOpen decisions: none."),
        ]) + "\n")
        written = capture.capture_session(str(t), "sessV4CAP1", root=self.wiki)
        self.assertEqual([r.kind for r in written], ["memory"])
        conn = self.conn()
        try:
            rows = db.rows(conn)
        finally:
            conn.close()
        self.assertEqual([r.kind for r in rows], ["M"])
        self.assertIn("scrap notes under the feature", rows[0].summary)
        self.assertFalse((self.wiki / "memory").exists(), "format 4 never writes a JSONL log")


class TestReadSurfaces(WikiCase):
    def setUp(self):
        super().setUp()
        conn = self.conn()
        db.add_row(conn, "R", "Run the linter before every commit", "Lint first, always.",
                   alias="lint-first")
        f1 = db.add_item(conn, "F", "sqlite ledger")
        db.add_item(conn, "J", "write the schema", parent=f1)
        conn.close()
        gconn = self.conn(self.global_wiki)
        db.add_row(gconn, "R", "Never force-push a shared branch", "History rewrites go to the operator.")
        gconn.close()

    def test_bootstrap_shows_the_ledger_and_rows(self):
        ctx = bootstrap.build_context(start=self.repo)
        self.assertIn("ledger repo: 1 features, 1 jobs, 0 tasks open", ctx)
        self.assertIn("- F1 sqlite ledger (1 open jobs)", ctx)
        self.assertIn("R1 (rule): Run the linter before every commit", ctx)
        self.assertIn("R1 (rule): Never force-push a shared branch", ctx)

    def test_fetch_ranks_rows_for_a_matching_prompt(self):
        sel = fetch.select_multi("should I lint before I commit", start=self.repo)
        self.assertIn("Run the linter before every commit", fetch.render(sel))

    def test_get_by_id_and_alias_prefers_the_repo_wiki(self):
        for key in ("R1", "lint-first"):
            result = mcp.tool_get({"id": key}, str(self.repo))
            self.assertEqual(result["source"], "row", key)
            self.assertEqual(result["meta"]["summary"], "Run the linter before every commit")
        self.assertEqual(mcp.tool_get({"id": "R1"}, str(self.repo))["also_in"],
                         [str(self.global_wiki)])
        item = mcp.tool_get({"id": "F1"}, str(self.repo))
        self.assertEqual(item["source"], "item")
        self.assertIn("J1 [open] write the schema", item["body"])
        self.assertIn("feature", get.render(item))

    def test_save_writes_a_row_not_a_file(self):
        result = save.save_note("repo", "Tasks never commit", "Only jobs commit.",
                                note_type="rule", start=self.repo)
        self.assertEqual(result["id"], "R2")
        self.assertEqual(list(self.wiki.glob("*.md")), [self.wiki / "AGENTS.md"])
        result = mcp.tool_save({"scope": "global", "summary": "Issues outrank memories",
                                "body": "Corrections are the most important rows.",
                                "type": "rule"}, str(self.repo))
        self.assertEqual(result["id"], "R2")
        self.assertTrue(result["path"].endswith("global/shapa.db"))


if __name__ == "__main__":
    unittest.main()
