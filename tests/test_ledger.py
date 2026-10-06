"""Format 4's work ledger: features hold jobs hold tasks, IDs mean kind +
counter, closing cascades, the after-feature sweep cleans the database."""

import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shapa import db, ledger


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wiki = self.tmp / ".shapa"
        self.wiki.mkdir()
        self.conn = db.connect(self.wiki, create=True)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestIds(LedgerCase):
    def test_ids_are_kind_letter_plus_a_per_kind_counter(self):
        f1 = db.add_item(self.conn, "F", "storage upgrade")
        j1 = db.add_item(self.conn, "J", "schema", parent=f1)
        j2 = db.add_item(self.conn, "J", "hooks", parent=f1)
        t1 = db.add_item(self.conn, "T", "write the table", parent=j1)
        f2 = db.add_item(self.conn, "F", "second feature")
        self.assertEqual([f1, j1, j2, t1, f2], ["F1", "J1", "J2", "T1", "F2"])

    def test_ids_are_never_reused_after_a_delete(self):
        f1 = db.add_item(self.conn, "F", "one")
        db.delete(self.conn, [f1])
        self.assertEqual(db.add_item(self.conn, "F", "two"), "F2")

    def test_deleted_and_edited_text_leaves_no_trace_in_the_file(self):
        rid, _ = db.add_row(self.conn, "M", "zqxdeletedzqx event", "zqxdeletedzqx body")
        kept, _ = db.add_row(self.conn, "R", "zqxeditedzqx rule", "body")
        db.delete(self.conn, [rid])
        db.update_row(self.conn, kept, summary="plain rule")
        data = db.db_path(self.wiki).read_bytes()
        self.assertNotIn(b"zqxdeletedzqx", data)
        self.assertNotIn(b"zqxeditedzqx", data)

    def test_parent_kind_is_enforced_and_standalone_is_allowed(self):
        f1 = db.add_item(self.conn, "F", "feature")
        j1 = db.add_item(self.conn, "J", "job", parent=f1)
        with self.assertRaises(db.LedgerError):
            db.add_item(self.conn, "T", "task under a feature", parent=f1)
        with self.assertRaises(db.LedgerError):
            db.add_item(self.conn, "J", "job under a job", parent=j1)
        self.assertEqual(db.add_item(self.conn, "J", "standalone job"), "J2")
        self.assertEqual(db.add_item(self.conn, "T", "standalone task"), "T1")

    def test_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with db.write(self.conn):
                self.conn.execute("INSERT INTO items(id, kind, parent, title, created, updated) "
                                  "VALUES ('J9', 'J', 'F404', 'x', 'now', 'now')")

    def test_branch_slug_cuts_at_a_word(self):
        f1 = db.add_item(self.conn, "F", "shapa format 4 in the harness: global wiki path, ledger hooks")
        self.assertEqual(ledger.branch_name(db.get_item(self.conn, f1)),
                         "F1-shapa-format-4-in-the-harness-global")

    def test_branch_and_commit_parsers(self):
        self.assertEqual(db.feature_of_branch("F12-sqlite-ledger"), "F12")
        self.assertEqual(db.feature_of_branch("origin/f3-x"), "F3")
        self.assertIsNone(db.feature_of_branch("feature-12"))
        self.assertEqual(db.job_of_message("J7: add the table\n\nbody"), "J7")
        self.assertEqual(db.job_of_message("j7 add the table"), "J7")
        self.assertIsNone(db.job_of_message("fix J7 later"))


class TestLifecycle(LedgerCase):
    def test_claim_release_and_close_cascades_to_open_children(self):
        f1 = db.add_item(self.conn, "F", "feature")
        j1 = db.add_item(self.conn, "J", "job", parent=f1)
        t1 = db.add_item(self.conn, "T", "task", parent=j1)
        db.claim(self.conn, j1, "sess1")
        self.assertEqual(db.items(self.conn, claimed_by="sess1")[0].id, j1)
        db.release(self.conn, j1)
        self.assertEqual(db.get_item(self.conn, j1).status, "open")
        closed = db.close(self.conn, f1, "merge", git_ref="F1-feature")
        self.assertEqual(closed, [f1, j1, t1])
        self.assertEqual(db.get_item(self.conn, t1).closed_reason, "parent")
        self.assertEqual(db.close(self.conn, f1, "merge"), [])

    def test_alias_finds_a_migrated_item(self):
        f1 = db.add_item(self.conn, "F", "plan")
        db.add_item(self.conn, "J", "old item", parent=f1, alias="LH10b")
        self.assertEqual(db.get_item(self.conn, "LH10b").id, "J1")

    def test_close_runs_the_verify_and_refuses_on_failure(self):
        repo = self.wiki.parent
        j1 = db.add_item(self.conn, "J", "needs proof", verify="test -f done.txt")
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(ledger.cmd_close(self.wiki, j1, "agent", None, False), 1)
        self.assertIn("stays open", err.getvalue())
        (repo / "done.txt").write_text("ok")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ledger.cmd_close(self.wiki, j1, "agent", None, False), 0)
        self.assertEqual(db.get_item(self.conn, j1).status, "closed")


class TestSweep(LedgerCase):
    def test_expiry_cleanup_and_the_triggering_merge_keeps_its_rows(self):
        old = datetime.now(timezone.utc) - timedelta(days=45)
        stale = db.add_item(self.conn, "J", "abandoned", now=old)
        fresh = db.add_item(self.conn, "J", "recent")
        done_before = db.add_item(self.conn, "T", "done long ago")
        db.close(self.conn, done_before, "agent", now=datetime.now(timezone.utc) - timedelta(days=2))
        started = db.now_iso()
        merged = db.add_item(self.conn, "F", "just merged")
        db.close(self.conn, merged, "merge")

        report = db.sweep(self.conn, keep_closed_since=started)
        self.assertEqual(report.expired, [stale])
        self.assertIn(done_before, report.deleted_items)
        self.assertIsNone(db.get_item(self.conn, done_before))
        self.assertEqual(db.get_item(self.conn, merged).status, "closed")
        self.assertEqual(db.get_item(self.conn, fresh).status, "open")
        # The next sweep (a later merge; timestamps are whole seconds) removes
        # what this one kept.
        report = db.sweep(self.conn, now=datetime.now(timezone.utc) + timedelta(seconds=2))
        self.assertIn(merged, report.deleted_items)

    def test_duplicates_superseded_and_stale_memories(self):
        r1, _ = db.add_row(self.conn, "R", "Never force-push shared branches", "body a")
        r2, created = db.add_row(self.conn, "R", "Never force-push shared branches", "body a")
        self.assertFalse(created)
        self.assertEqual(r1, r2)
        r3, _ = db.add_row(self.conn, "R", "Never force push a shared branch", "body b", tags=["git"])
        old = datetime.now(timezone.utc) - timedelta(days=90)
        m1, _ = db.add_row(self.conn, "M", "an old remark nobody used", now=old)
        m2, _ = db.add_row(self.conn, "M", "an old remark that was used", now=old)
        m3, _ = db.add_row(self.conn, "M", "replacement note")
        m4, _ = db.add_row(self.conn, "M", "outdated note")
        db.link(self.conn, m3, m4, "supersedes")
        vectors = {r1: [1.0, 0.0, 0.0, 0.0], r3: [0.99, 0.05, 0.0, 0.0],
                   m1: [0.0, 1.0, 0.0, 0.0], m2: [0.0, 0.0, 1.0, 0.0],
                   m3: [0.0, 0.0, 0.0, 1.0], m4: [0.0, 0.7, 0.7, 0.0]}
        uses = {m2: (3, db.now_iso())}
        report = db.sweep(self.conn, vectors=vectors, uses=uses)
        self.assertEqual(report.duplicates, [(r3, r1)])
        self.assertEqual(db.get_row(self.conn, r1).tags, ["git"])  # moved onto the kept row
        self.assertEqual(report.superseded, [m4])
        self.assertEqual(report.stale_memories, [m1])
        self.assertIsNotNone(db.get_row(self.conn, m2))

    def test_similar_rules_are_flagged_never_deleted(self):
        r1, _ = db.add_row(self.conn, "R", "Deploy only from tags")
        r2, _ = db.add_row(self.conn, "R", "Deploy from main on every push")
        report = db.sweep(self.conn, vectors={r1: [1.0, 0.0], r2: [0.85, 0.53]})
        self.assertEqual(report.conflicts, [(r1, r2)])
        self.assertIsNotNone(db.get_row(self.conn, r2))
        self.assertEqual(db.conflicts(self.conn), [(r1, r2)])

    def test_without_vectors_only_near_identical_text_merges(self):
        r1, _ = db.add_row(self.conn, "R", "alpha beta gamma delta", "epsilon zeta eta theta kappa lambda")
        r2, _ = db.add_row(self.conn, "R", "alpha beta gamma delta",
                           "epsilon zeta eta theta kappa lambda iota")
        r3, _ = db.add_row(self.conn, "R", "something else entirely", "unrelated words")
        report = db.sweep(self.conn)
        self.assertEqual(report.duplicates, [(r2, r1)])
        self.assertIsNotNone(db.get_row(self.conn, r3))


class TestSweepSafety(LedgerCase):
    def test_claimed_work_in_progress_and_its_feature_never_expire(self):
        old = datetime.now(timezone.utc) - timedelta(days=35)
        f1 = db.add_item(self.conn, "F", "long feature", now=old)
        j1 = db.add_item(self.conn, "J", "still being worked", parent=f1, now=old)
        db.claim(self.conn, j1, "sess")
        db.update_item(self.conn, j1, body="touched today")
        report = db.sweep(self.conn)
        self.assertEqual(report.expired, [])
        self.assertEqual(db.get_item(self.conn, j1).status, "claimed")

    def test_a_claim_left_for_30_days_expires(self):
        old = datetime.now(timezone.utc) - timedelta(days=35)
        j1 = db.add_item(self.conn, "J", "abandoned claim", now=old)
        db.claim(self.conn, j1, "sess")
        self.conn.execute("UPDATE items SET updated = ? WHERE id = ?", (db.now_iso(old), j1))
        self.assertEqual(db.sweep(self.conn).expired, [j1])

    def test_one_sweep_never_deletes_what_it_just_closed(self):
        old = datetime.now(timezone.utc) - timedelta(days=45)
        j1 = db.add_item(self.conn, "J", "abandoned", now=old)
        report = db.sweep(self.conn)
        self.assertEqual(report.expired, [j1])
        self.assertNotIn(j1, report.deleted_items)
        self.assertEqual(db.get_item(self.conn, j1).closed_reason, "expired")


class TestScrub(LedgerCase):
    def test_scrub_replaces_terms_everywhere_and_rehashes(self):
        rid, _ = db.add_row(self.conn, "M", "Acme launch notes", "the Acme team said so",
                            tags=["acme-repo"])
        db.add_item(self.conn, "F", "Ship Acme v2")
        self.assertGreater(db.scrub(self.conn, ["acme"]), 0)
        row = db.get_row(self.conn, rid)
        self.assertNotIn("Acme", row.summary + row.body)
        self.assertEqual(row.tags, ["[workspace]-repo"])
        self.assertEqual(row.hash, db.content_hash(row.summary, row.body))
        self.assertEqual(db.get_item(self.conn, "F1").title, "Ship [workspace] v2")


if __name__ == "__main__":
    unittest.main()
