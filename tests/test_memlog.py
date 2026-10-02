"""Tests for shapa.memlog (format-3 memory records) and shapa.redact."""

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from shapa import embed, memlog, redact, store

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def rec(summary, body="", kind="fact", **kw):
    return memlog.make_record(kind=kind, summary=summary, body=body, created="2026-10-02T12:00:00Z", **kw)


class TestRedact(unittest.TestCase):
    def test_known_shapes_are_masked(self):
        samples = [
            "key sk-ant-api03-abcdefghijklmnopqrstuvwx",
            "token ghp_" + "a" * 36,
            "aws AKIA" + "B" * 16,
            "Authorization: Bearer abcdefghijklmnop.qrstu",
            "postgres://admin:hunter2secret@db.example.com/x",
            "OPENAI_API_KEY=sk-proj-" + "x" * 30,
            "jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
            "client_secret: 'Zm9vYmFyYmF6cXV4MTIzNDU2'",
        ]
        for s in samples:
            out, hits = redact.scrub(s)
            self.assertGreater(hits, 0, s)
            self.assertIn(redact.PLACEHOLDER, out, s)

    def test_prose_is_untouched_and_idempotent(self):
        prose = "the token budget is 1800; password rules live in placement.md"
        self.assertEqual(redact.scrub(prose), (prose, 0))
        once = redact.redact("Bearer abcdefghijklmnopqrst")
        self.assertEqual(redact.redact(once), once)


class TestRecords(unittest.TestCase):
    def test_limits_and_content_id(self):
        r = rec("x " * 200, "y " * 500)
        self.assertLessEqual(len(r.summary), memlog.SUMMARY_MAX)
        self.assertLessEqual(len(r.body), memlog.BODY_MAX)
        self.assertEqual(r.id, "m-" + r.hash[:10])
        self.assertEqual(rec("x " * 200, "y " * 500).id, r.id)

    def test_secret_redacted_before_hash(self):
        r = rec("deploy key sk-ant-api03-abcdefghijklmnopqrstuvwx", "body")
        self.assertNotIn("sk-ant", r.to_json())
        self.assertEqual(r.hash, memlog.content_hash(r.summary, r.body))

    def test_line_roundtrip_and_field_order(self):
        r = rec("summary one", "body one", tags=["a/b.py", "#12"], source="capture:report",
                session="abcdef123456", repo="repo-x")
        line = r.to_json()
        self.assertEqual(list(json.loads(line)), list(memlog.FIELDS))
        self.assertEqual(memlog.parse_line(line), r)
        self.assertEqual(r.session, "abcdef12")

    def test_malformed_lines_are_none(self):
        for bad in ("{", "[]", '{"id": 3}', '{"id": "m-1"}', '{"op": "delete", "target": "x"}'):
            self.assertIsNone(memlog.parse_line(bad), bad)

    def test_kinds(self):
        self.assertEqual(memlog.classify_kind("the hook silently broke"), "gotcha")
        self.assertEqual(memlog.classify_kind("is the floor right?"), "open_question")
        self.assertEqual(memlog.classify_kind("never push to main"), "preference")
        self.assertEqual(memlog.classify_kind("merged the fix"), "outcome")
        self.assertEqual(memlog.classify_kind("plain statement", "decision"), "decision")


class LogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / ".shapa"
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)


class TestAppend(LogCase):
    def test_append_writes_month_file_and_gitattributes(self):
        res = memlog.append(self.root, [rec("first memory about the parser")], now=NOW)
        self.assertEqual(len(res.written), 1)
        self.assertEqual(res.path, self.root / "memory" / "2026-10.jsonl")
        self.assertIn(memlog.GITATTRIBUTES_LINE, (self.root / ".gitattributes").read_text())
        self.assertEqual(len(memlog.read_log(self.root).records), 1)

    def test_exact_and_near_duplicates_are_skipped(self):
        a = rec("the index rebuilds from the log on a fresh clone", "the index rebuilds from the "
                "log on a fresh clone because the sqlite file is gitignored and derived")
        memlog.append(self.root, [a], now=NOW)
        res = memlog.append(self.root, [a], now=NOW)
        self.assertEqual(res.duplicates, [a.id])
        near = rec(a.summary, a.body + " entirely")
        res = memlog.append(self.root, [near], now=NOW)
        self.assertEqual(res.written, [])
        self.assertEqual(len(memlog.read_log(self.root).records), 1)

    def test_update_supersedes(self):
        base = ("the fetch hook injects summaries per prompt with ids and the wrapper header "
                "counted in tokens for the budget, rendered as one line per memory under the "
                "shapa memory tag with the full body served on demand through get")
        old = rec("fetch budget", base + " by the cli")
        memlog.append(self.root, [old], now=NOW)
        new = rec("fetch budget", base + " by the mcp tool and the cli alike, measured today")
        res = memlog.append(self.root, [new], now=NOW)
        self.assertEqual(res.superseded, {new.id: old.id})
        view = memlog.read_log(self.root)
        self.assertEqual(view.status(old.id), "superseded")
        self.assertEqual([r.id for r in view.active()], [new.id])

    def test_flood_is_capped(self):
        def word(i):
            return "".join(chr(97 + (i // 26 ** k) % 26) for k in range(3)) + "x"
        many = [rec(f"fact {word(i)} {word(i + 300)} {word(i + 600)} {word(i + 900)}")
                for i in range(200)]
        res = memlog.append(self.root, many, now=NOW)
        self.assertEqual(len(res.written), memlog.MAX_APPEND)

    def test_archive_op(self):
        r = rec("archivable memory")
        memlog.append(self.root, [r], now=NOW)
        self.assertTrue(memlog.archive(self.root, r.id, "promoted:x", now=NOW))
        self.assertFalse(memlog.archive(self.root, r.id, now=NOW))
        self.assertEqual(memlog.get(self.root, r.id)[1], "archived")
        self.assertEqual(memlog.read_log(self.root).active(), [])

    def test_concurrent_appends_never_interleave(self):
        script = self.tmp / "w.py"
        script.write_text(
            "import sys\nfrom shapa import memlog\n"
            "root, tag = sys.argv[1], sys.argv[2]\n"
            "for i in range(30):\n"
            "    r = memlog.make_record(kind='fact', summary=f'{tag} unique fact {i} '"
            " + 'q' * (i % 7) + f' {tag}{i}x')\n"
            "    memlog.append(root, [r])\n")
        import sys as _sys
        procs = [subprocess.Popen([_sys.executable, str(script), str(self.root), f"w{n}"],
                                  cwd=str(Path(memlog.__file__).parent.parent)) for n in range(4)]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        view = memlog.read_log(self.root)
        self.assertEqual(view.malformed, 0)
        self.assertEqual(view.lines, len(view.records))
        self.assertGreaterEqual(len(view.records), 100)


class TestIndex(LogCase):
    def _conn(self):
        conn = memlog.open_index(self.root)
        self.addCleanup(conn.close)
        return conn

    def test_no_log_means_no_index(self):
        self.assertIsNone(memlog.open_index(self.root))
        self.assertFalse(store.db_path(self.root).exists())

    def test_incremental_tail_then_rebuild_on_rewrite(self):
        memlog.append(self.root, [rec("alpha parser memory")], now=NOW)
        conn = memlog.open_index(self.root)
        conn.close()
        memlog.append(self.root, [rec("beta exporter memory")], now=NOW)
        conn = memlog.open_index(self.root)
        st = memlog.sync(self.root, conn)
        self.assertFalse(st.rebuilt)
        self.assertEqual(len(memlog.active_records(conn)), 2)
        conn.close()
        # A merge-style rewrite (line inserted before existing ones) rebuilds.
        log = self.root / "memory" / "2026-10.jsonl"
        lines = log.read_text().splitlines()
        log.write_text("\n".join([rec("gamma inserted memory").to_json(), *lines]) + "\n")
        conn = store.open_index(self.root)
        conn.row_factory = None
        memlog.ensure_schema(conn)
        st = memlog.sync(self.root, conn)
        self.assertTrue(st.rebuilt)
        self.assertEqual(len(memlog.active_records(conn)), 3)
        conn.close()

    def test_partial_line_waits_for_next_sync(self):
        memlog.append(self.root, [rec("complete line memory")], now=NOW)
        log = self.root / "memory" / "2026-10.jsonl"
        with log.open("a") as fh:
            fh.write(rec("half written memory").to_json()[:20])
        conn = self._conn()
        self.assertEqual(len(memlog.active_records(conn)), 1)

    def test_lexical_search_and_superseded_hidden(self):
        old = rec("pnpm lockfile gotcha", "the pnpm lockfile breaks when two branches add the "
                  "same dependency at different versions in the monorepo")
        memlog.append(self.root, [old, rec("unrelated note about colours")], now=NOW)
        new = rec("pnpm lockfile gotcha", "the pnpm lockfile breaks when two branches add the "
                  "same dependency at different versions in the workspace root")
        memlog.append(self.root, [new], now=NOW)
        conn = self._conn()
        scores = memlog.lexical_scores(conn, "pnpm lockfile")
        self.assertIn(new.id, scores)
        self.assertNotIn(old.id, scores)

    def test_read_only_never_writes_and_sees_fresh_lines(self):
        memlog.append(self.root, [rec("read only memory one")], now=NOW)
        for p in self.root.glob(store.db_path(self.root).name + "*"):
            p.unlink()  # a fresh clone: the log, no index
        conn = memlog.open_index(self.root, read_only=True)
        self.assertEqual(len(memlog.active_records(conn)), 1)
        conn.close()
        self.assertFalse(store.db_path(self.root).exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         [".gitattributes", "memory"])

    def test_read_only_over_a_stale_index_leaves_it_untouched(self):
        memlog.append(self.root, [rec("read only memory one")], now=NOW)
        db = store.db_path(self.root)

        def snapshot():
            return sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                          for p in self.root.glob(db.name + "*"))

        before = snapshot()
        with (self.root / "memory" / "2026-10.jsonl").open("a") as fh:
            fh.write(rec("a second line nobody synced yet").to_json() + "\n")
        conn = memlog.open_index(self.root, read_only=True)
        self.assertEqual(len(memlog.active_records(conn)), 2)
        conn.close()
        self.assertEqual(snapshot(), before)

    def test_append_dedups_through_the_index_and_its_shingle_cache(self):
        first = rec("Stripe webhook retries flood the queue when the handler returns 500")
        memlog.append(self.root, [first], now=NOW)
        conn = self._conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_shingles").fetchone()[0], 1)
        conn.close()
        again = rec("Stripe webhook retries flood the queue when the handler returns 500!")
        res = memlog.append(self.root, [again], now=NOW)
        self.assertEqual(res.duplicates, [again.id])
        with mock.patch.object(memlog, "open_index", side_effect=sqlite3.OperationalError("locked")):
            res = memlog.append(self.root, [again], now=NOW)  # fallback: parse the log
        self.assertEqual(res.duplicates, [again.id])

    def test_uses_survive_rebuild(self):
        r = rec("counted memory")
        memlog.append(self.root, [r], now=NOW)
        self._conn()
        self.assertEqual(memlog.record_use(self.root, r.id), 1)
        self.assertEqual(memlog.record_use(self.root, r.id), 2)
        (self.root / "memory" / "2026-10.jsonl").write_text(r.to_json() + "\n")  # rewrite
        conn = memlog.open_index(self.root)
        self.assertEqual(memlog.uses(conn)[r.id][0], 2)
        conn.close()
        self.assertEqual([t[0].id for t in memlog.hot(self.root, 2)], [r.id])

    def test_fts5_unavailable_falls_back_to_python_bm25(self):
        with mock.patch.object(store, "fts5_available", return_value=False):
            memlog.append(self.root, [rec("fallback lexical memory about webhooks")], now=NOW)
            conn = memlog.open_index(self.root, read_only=True)
            self.assertFalse(memlog._has_fts(conn))
            self.assertTrue(memlog.lexical_scores(conn, "webhooks"))
            conn.close()

    @unittest.skipUnless(embed.available(), "needs the [semantic] extra")
    def test_vectors_cached_by_hash(self):
        r = rec("vector memory about the payment webhook retry policy")
        memlog.append(self.root, [r], now=NOW)
        conn = memlog.open_index(self.root, embed_vectors=True)
        n = conn.execute("SELECT count(*) FROM memory_vectors").fetchone()[0]
        self.assertEqual(n, 1)
        qv = embed.embed_one("webhook retries")
        self.assertIn(r.id, memlog.vector_scores(conn, qv))
        self.assertEqual(memlog.sync(self.root, conn, embed_vectors=True).embedded, 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
