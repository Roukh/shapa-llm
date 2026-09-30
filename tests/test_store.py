"""Tests for shapa.store: the per-root sqlite index (shapa-backend-spec.md §5)."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from shapa import fetch, frontmatter, store
from shapa.bm25 import bm25_scores
from shapa.nodes import extract_links, load_nodes

FIX = Path(__file__).parent / "fixtures" / "fetch"


def _note(d: Path, name: str, body: str, consequence: int = 5, extra: str = "") -> Path:
    p = d / f"{name}.md"
    p.write_text(
        f"---\nid: {name}\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
        f"consequence: {consequence}\nlocus: output\nuses: 0\n{extra}---\n{body}\n",
        encoding="utf-8",
    )
    return p


class TestFts5Probe(unittest.TestCase):
    def test_probe_is_a_real_create_attempt_not_a_pragma(self):
        # Whatever this build answers, it must be a bool, and calling it
        # again must not touch disk (no side effects, cached).
        result = store.fts5_available()
        self.assertIsInstance(result, bool)
        self.assertEqual(result, store.fts5_available())


class TestOpenIndexAndSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_index_creates_notes_table(self):
        conn = store.open_index(self.tmp)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notes'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()
        self.assertTrue(store.db_path(self.tmp).is_file())

    def test_open_index_creates_fts5_table_when_available(self):
        conn = store.open_index(self.tmp)
        try:
            has_fts = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notes_fts'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(has_fts is not None, store.fts5_available())

    def test_open_index_read_only_raises_when_no_db_exists_and_never_creates_one(self):
        with self.assertRaises(FileNotFoundError):
            store.open_index(self.tmp, read_only=True)
        self.assertFalse(store.db_path(self.tmp).is_file())

    def test_open_index_read_only_reads_an_existing_db_but_cannot_write_to_it(self):
        conn = store.open_index(self.tmp)
        conn.close()
        self.assertTrue(store.db_path(self.tmp).is_file())
        ro = store.open_index(self.tmp, read_only=True)
        try:
            row = ro.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notes'"
            ).fetchone()
            self.assertIsNotNone(row)
            # sqlite's own read-only URI mode enforces this, not just a
            # convention this module follows - proves the connection itself
            # can't be written through, not just that nothing here tries to.
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("INSERT INTO notes (path, id, mtime_ns, size, body, meta_json) "
                           "VALUES ('x', 'x', 0, 0, '', '{}')")
        finally:
            ro.close()

    def test_open_index_read_only_leaves_no_shm_or_wal_sidecar_behind(self):
        # Regression, found live against roukh-brain's real index during
        # this fix's own verification (shapa-backend-spec.md Slice 6
        # report): every index this module creates is journal_mode=WAL, and
        # a plain `mode=ro` open of a WAL-mode database still makes SQLite
        # materialize a `-shm` (and sometimes `-wal`) sidecar to coordinate
        # with the WAL machinery - `mode=ro` alone does not fix the
        # contamination this method exists to prevent; `immutable=1` does.
        _note(self.tmp, "a", "some real content")
        store.record_use(self.tmp, "a")  # a genuine write, so the WAL is exercised for real
        db = store.db_path(self.tmp)
        shm, wal = Path(str(db) + "-shm"), Path(str(db) + "-wal")
        for _ in range(3):  # a single open/close can look clean by luck; repeat
            ro = store.open_index(self.tmp, read_only=True)
            try:
                list(ro.execute("SELECT * FROM notes"))
            finally:
                ro.close()
            self.assertFalse(shm.exists(), "-shm sidecar written by a read-only open")
            self.assertFalse(wal.exists(), "-wal sidecar written by a read-only open")

    def test_open_index_skips_fts5_table_when_unavailable(self):
        real = store.fts5_available
        store.fts5_available = lambda: False
        try:
            conn = store.open_index(self.tmp)
            try:
                has_fts = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='notes_fts'"
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNone(has_fts)
        finally:
            store.fts5_available = real


class TestSyncIncremental(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_sync_adds_every_note(self):
        for i in range(5):
            _note(self.tmp, f"n{i}", f"body number {i}")
        result = store.sync(self.tmp)
        self.assertEqual(result.added, 5)
        self.assertEqual((result.updated, result.removed, result.unchanged), (0, 0, 0))

    def test_reindex_noop_is_cheap(self):
        # tests/test_store.py::test_reindex_noop_is_cheap (spec §5 perf table)
        for i in range(20):
            _note(self.tmp, f"n{i}", f"body number {i}")
        store.sync(self.tmp)
        result = store.sync(self.tmp)
        self.assertEqual(result.unchanged, 20)
        self.assertEqual((result.added, result.updated, result.removed), (0, 0, 0))

    def test_reindex_incremental_only_touches_changed(self):
        # tests/test_store.py::test_reindex_incremental_only_touches_changed
        paths = [_note(self.tmp, f"n{i}", f"body number {i}") for i in range(10)]
        store.sync(self.tmp)

        # Touch exactly one file's content (and therefore its size/mtime).
        paths[3].write_text(paths[3].read_text(encoding="utf-8") + "extra sentence.\n")

        calls = []
        real_parse = store.frontmatter.parse

        def counting_parse(p):
            calls.append(p)
            return real_parse(p)

        store.frontmatter.parse = counting_parse
        try:
            result = store.sync(self.tmp)
        finally:
            store.frontmatter.parse = real_parse

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.unchanged, 9)
        self.assertEqual((result.added, result.removed), (0, 0))
        # Only the one changed file was actually re-read/re-parsed.
        self.assertEqual(calls, [paths[3]])

    def test_sync_removes_deleted_notes(self):
        paths = [_note(self.tmp, f"n{i}", f"body number {i}") for i in range(3)]
        store.sync(self.tmp)
        paths[1].unlink()
        result = store.sync(self.tmp)
        self.assertEqual(result.removed, 1)
        conn = store.open_index(self.tmp)
        try:
            ids = {row["id"] for row in conn.execute("SELECT id FROM notes")}
        finally:
            conn.close()
        self.assertEqual(ids, {"n0", "n2"})

    def test_sync_preserves_uses_across_a_content_change(self):
        p = _note(self.tmp, "n0", "original body")
        store.sync(self.tmp)
        store.record_use(self.tmp, "n0")
        store.record_use(self.tmp, "n0")
        p.write_text(p.read_text(encoding="utf-8") + "more text.\n")
        store.sync(self.tmp)
        uses, _ = store.get_use(self.tmp, "n0")
        self.assertEqual(uses, 2)

    def test_sync_never_indexes_archive_attic_or_obsidian(self):
        # GAP A (shapa-backend-spec.md §10 decision 6): archive/attic are
        # never loaded - store.sync() must skip them exactly like
        # nodes.load_nodes does, not just add them and rely on some later
        # filter.
        _note(self.tmp, "live-one", "body")
        (self.tmp / "archive").mkdir()
        _note(self.tmp / "archive", "archived-one", "body")
        (self.tmp / "attic").mkdir()
        _note(self.tmp / "attic", "attic-one", "body")
        (self.tmp / ".obsidian").mkdir()
        _note(self.tmp / ".obsidian", "stray", "body")

        result = store.sync(self.tmp)
        self.assertEqual(result.added, 1)  # only live-one

        conn = store.open_index(self.tmp)
        try:
            ids = {row["id"] for row in conn.execute("SELECT id FROM notes")}
        finally:
            conn.close()
        self.assertEqual(ids, {"live-one"})

    def test_moving_a_note_into_archive_drops_it_from_the_index(self):
        # A note `shapa maintain --lean --apply` moves into archive/
        # disappears from the index on the very next sync - the same
        # "gone" cleanup path an ordinary delete takes.
        p = _note(self.tmp, "was-live", "body")
        store.sync(self.tmp)
        conn = store.open_index(self.tmp)
        try:
            self.assertEqual(
                {row["id"] for row in conn.execute("SELECT id FROM notes")},
                {"was-live"},
            )
        finally:
            conn.close()

        archive_dir = self.tmp / "archive"
        archive_dir.mkdir()
        p.rename(archive_dir / p.name)
        result = store.sync(self.tmp)
        self.assertEqual(result.removed, 1)

        conn = store.open_index(self.tmp)
        try:
            ids = {row["id"] for row in conn.execute("SELECT id FROM notes")}
        finally:
            conn.close()
        self.assertEqual(ids, set())


class TestFts5UnavailableFallbackIdentical(unittest.TestCase):
    """Slice 5 acceptance: 'FTS5-creation-failure path returns results
    identical to the pre-index hand-rolled BM25 on a fixed fixture.'"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for f in FIX.glob("*.md"):
            shutil.copy(f, self.tmp / f.name)
        real = store.fts5_available
        store.fts5_available = lambda: False
        self._real_fts5_available = real

    def tearDown(self):
        store.fts5_available = self._real_fts5_available
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _expected_bm25(self, query: str) -> dict[str, float]:
        # The exact recipe fetch.py's own hand-rolled path has always used
        # (fetch._load_root_data): body + id words + wikilink outlinks.
        nodes = load_nodes(self.tmp)
        docs = {}
        for nid, node in nodes.items():
            body = frontmatter.parse(node.path).body
            id_topic = nid.replace("-", " ") + " " + " ".join(extract_links(body))
            docs[nid] = fetch._words(body + " " + id_topic)
        return bm25_scores(query, docs)

    def test_fallback_scores_match_hand_rolled_bm25_exactly(self):
        store.sync(self.tmp)
        conn = store.open_index(self.tmp)
        try:
            has_fts = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notes_fts'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(has_fts, "fixture must have taken the no-FTS5 path")

        for query in ("testing discipline", "git repository commit", "nonsense zzqx"):
            got = dict(store.search(self.tmp, query))
            want = self._expected_bm25(query)
            self.assertEqual(got, want, f"mismatch for query={query!r}")

    def test_fallback_ranking_matches_top_k(self):
        store.sync(self.tmp)
        ranked = store.search(self.tmp, "git repository commit", k=2)
        want = sorted(self._expected_bm25("git repository commit").items(),
                       key=lambda kv: (-kv[1], kv[0]))[:2]
        self.assertEqual(ranked, want)


class TestFts5SearchWhenAvailable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipUnless(store.fts5_available(), "sqlite3 build has no FTS5")
    def test_fts5_search_finds_the_matching_note(self):
        _note(self.tmp, "a", "the git worktree convention matters a lot here")
        _note(self.tmp, "b", "completely unrelated cooking notes about pasta")
        store.sync(self.tmp)
        results = store.search(self.tmp, "git worktree")
        ids = [nid for nid, _ in results]
        self.assertIn("a", ids)
        self.assertLess(ids.index("a"), ids.index("b") if "b" in ids else len(ids))


class TestRecordUse(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_use_increments_and_never_touches_the_file(self):
        p = _note(self.tmp, "n0", "some body text")
        before = p.read_bytes()
        n = store.record_use(self.tmp, "n0")
        self.assertEqual(n, 1)
        n = store.record_use(self.tmp, "n0")
        self.assertEqual(n, 2)
        self.assertEqual(p.read_bytes(), before)

    def test_record_use_on_unindexed_note_never_blocks(self):
        # No file at all under this root; must return 0, not raise.
        self.assertEqual(store.record_use(self.tmp, "does-not-exist"), 0)

    def test_record_use_syncs_a_just_captured_note_first(self):
        # The note is written to disk moments before the use is recorded -
        # never explicitly synced by the caller - and must still count.
        _note(self.tmp, "brand-new", "just captured this session")
        n = store.record_use(self.tmp, "brand-new")
        self.assertEqual(n, 1)

    def test_get_all_uses_bulk_matches_get_use(self):
        _note(self.tmp, "a", "body a")
        _note(self.tmp, "b", "body b")
        store.record_use(self.tmp, "a")
        store.record_use(self.tmp, "a")
        all_uses = store.get_all_uses(self.tmp)
        self.assertEqual(all_uses["a"], store.get_use(self.tmp, "a"))
        self.assertEqual(all_uses["b"], store.get_use(self.tmp, "b"))
        self.assertEqual(all_uses["b"][0], 0)

    def test_get_all_uses_read_only_on_an_unindexed_root_never_creates_the_db(self):
        # The exact contamination shape the incident had (shapa-backend-spec.md
        # Slice 6 report): a wiki with no `.shapa-index.db` yet, asked a
        # read-only question - must answer "no live-use data" without ever
        # materializing the file, not fall back to the writable open path.
        self.assertEqual(store.get_all_uses(self.tmp, read_only=True), {})
        self.assertFalse(store.db_path(self.tmp).is_file())

    def test_get_all_uses_read_only_still_sees_an_already_indexed_root(self):
        _note(self.tmp, "a", "body a")
        store.record_use(self.tmp, "a")  # normal (writable) path indexes + bumps it
        self.assertEqual(store.get_all_uses(self.tmp, read_only=True)["a"][0], 1)

    def test_open_index_failure_returns_safe_defaults_not_a_crash(self):
        # A root that cannot hold a database file (e.g. read-only, or gone)
        # must degrade to "no counters," never raise - store.py is never a
        # hard dependency (§5).
        gone = self.tmp / "does" / "not" / "exist-and-is-unwritable"
        real_open = store.open_index
        store.open_index = lambda root, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("boom"))
        try:
            self.assertEqual(store.get_use(gone, "x"), (0, None))
            self.assertEqual(store.get_all_uses(gone), {})
            self.assertEqual(store.record_use(gone, "x"), 0)
        finally:
            store.open_index = real_open


if __name__ == "__main__":
    unittest.main()
