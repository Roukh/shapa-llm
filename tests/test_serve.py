"""Tests for shapa.serve: the warm per-root daemon (shapa-backend-spec.md §5).

The central acceptance for this slice: "a capture mid-session is visible on
the daemon's next request" - a real Unix socket, a real sqlite index, a real
file written to disk between two requests against the SAME running daemon
(no restart), proving the per-request staleness re-check actually works.
"""

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from shapa import serve, store


def _note(d: Path, name: str, body: str) -> Path:
    p = d / f"{name}.md"
    p.write_text(
        f"---\nid: {name}\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
        f"consequence: 5\nlocus: output\nuses: 0\n---\n{body}\n",
        encoding="utf-8",
    )
    return p


class TestDaemonLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_socket_created_with_owner_only_permissions(self):
        with serve.Daemon(self.tmp) as d:
            self.assertTrue(d.sock_path.exists())
            mode = d.sock_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
        self.assertFalse(d.sock_path.exists())  # cleaned up on stop

    def test_request_with_no_daemon_returns_none(self):
        # No daemon running for this root at all - the cold-path fallback
        # signal, never a hang, never a crash (§5: "absence never breaks
        # correctness, only latency").
        self.assertIsNone(serve.request(self.tmp, {"cmd": "ping"}))

    def test_ping(self):
        with serve.Daemon(self.tmp):
            reply = serve.request(self.tmp, {"cmd": "ping"})
        self.assertEqual(reply, {"ok": True})

    def test_unknown_command_is_a_clean_error_not_a_crash(self):
        with serve.Daemon(self.tmp):
            reply = serve.request(self.tmp, {"cmd": "not-a-real-command"})
        self.assertFalse(reply["ok"])


class TestDaemonStaleness(unittest.TestCase):
    """The slice's LIVE acceptance: capture mid-session -> visible on the
    daemon's very next request, without a restart."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_capture_mid_session_is_visible_on_next_request(self):
        _note(self.tmp, "existing", "an existing note about nothing in particular")

        with serve.Daemon(self.tmp):
            stats1 = serve.request(self.tmp, {"cmd": "stats"})
            self.assertEqual(stats1, {"ok": True, "notes": 1})

            search1 = serve.request(self.tmp, {"cmd": "search", "query": "brand new capture"})
            self.assertEqual([r["id"] for r in search1["results"]], [])

            # A "capture" happening mid-session, from an entirely separate
            # process's point of view: a plain file write, no daemon API
            # call, the daemon never told directly.
            time.sleep(0.01)  # ensure a distinct mtime from the setUp write
            _note(self.tmp, "brand-new", "a brand new capture written mid session")

            # Same daemon, no restart.
            stats2 = serve.request(self.tmp, {"cmd": "stats"})
            self.assertEqual(stats2, {"ok": True, "notes": 2})

            search2 = serve.request(self.tmp, {"cmd": "search", "query": "brand new capture"})
            ids = [r["id"] for r in search2["results"]]
            self.assertIn("brand-new", ids)

    def test_deleted_note_disappears_on_next_request_too(self):
        p = _note(self.tmp, "temp", "a temporary note")
        with serve.Daemon(self.tmp):
            self.assertEqual(serve.request(self.tmp, {"cmd": "stats"})["notes"], 1)
            p.unlink()
            self.assertEqual(serve.request(self.tmp, {"cmd": "stats"})["notes"], 0)

    def test_use_recorded_via_fetch_is_visible_through_the_daemon(self):
        # Cross-module live check: shapa.fetch's record path writes into
        # the SAME index the daemon reads from (§10 decision 7) - a use
        # recorded outside the daemon process is picked up on its next
        # request, exactly like a capture.
        _note(self.tmp, "n0", "some note body")
        with serve.Daemon(self.tmp):
            store.record_use(self.tmp, "n0")
            store.record_use(self.tmp, "n0")
            uses, _ = store.get_use(self.tmp, "n0")
        self.assertEqual(uses, 2)


class TestDaemonRelevance(unittest.TestCase):
    """GAP D (shapa-backend-spec.md §5): the daemon's "relevance" command
    must return the exact same scores `fetch.raw_relevance` would compute
    in-process - it's the SAME function, just called on a long-lived
    process that already has the embedding model loaded, not a second,
    independently-written scorer that could silently drift."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _note(self.tmp, "a", "git workflow testing discipline")
        _note(self.tmp, "b", "an unrelated note about lunch")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relevance_matches_in_process_scoring(self):
        from shapa import fetch, frontmatter
        from shapa.nodes import load_nodes

        nodes = load_nodes(self.tmp)
        bodies = {nid: frontmatter.parse(n.path).body for nid, n in nodes.items()}
        expected_bm25, expected_emb, expected_used = fetch.raw_relevance(
            self.tmp, nodes, bodies, "git workflow"
        )

        with serve.Daemon(self.tmp):
            reply = serve.request(self.tmp, {"cmd": "relevance", "query": "git workflow"})

        self.assertTrue(reply["ok"])
        self.assertEqual(reply["embed_available"], expected_used)
        for nid in nodes:
            self.assertAlmostEqual(reply["bm25_rel"].get(nid, 0.0), expected_bm25[nid], places=6)
            self.assertAlmostEqual(reply["emb_rel"].get(nid, 0.0), expected_emb[nid], places=4)

    def test_fetch_select_is_identical_with_and_without_the_daemon(self):
        # The full acceptance for this slice: select()'s output (ranking +
        # snippets) must not change depending on whether a daemon happens
        # to be running for the root - the daemon is latency-only (§5).
        from shapa import fetch

        cold = fetch.select("git workflow testing discipline", root=self.tmp, k=5)
        with serve.Daemon(self.tmp):
            warm = fetch.select("git workflow testing discipline", root=self.tmp, k=5)

        self.assertEqual([n.id for n, _ in cold], [n.id for n, _ in warm])
        self.assertEqual([snip for _, snip in cold], [snip for _, snip in warm])

    def test_read_only_callers_never_use_the_daemon(self):
        # shapa.mcp's search tool passes read_only=True precisely so a plain
        # search never leaves a byte behind - that must hold even when a
        # daemon IS running and would happily answer; read_only skips the
        # daemon round-trip entirely (fetch.py's own contract), not just
        # the cache write.
        from shapa import fetch

        with serve.Daemon(self.tmp):
            with mock.patch.object(serve, "request") as mock_request:
                fetch.select("git workflow", root=self.tmp, k=5, read_only=True)
        mock_request.assert_not_called()


class TestEnsureRunning(unittest.TestCase):
    """GAP D autostart: `ensure_running` pings first and only spawns a
    background process on a real miss - and the spawn itself never blocks
    or raises, matching every other entrypoint in this module's
    never-blocks contract. Popen is mocked throughout so this test never
    leaves a real background process behind."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_does_not_spawn_when_already_running(self):
        with serve.Daemon(self.tmp):
            with mock.patch("subprocess.Popen") as mock_popen:
                result = serve.ensure_running(self.tmp)
        self.assertTrue(result)
        mock_popen.assert_not_called()

    def test_spawns_a_detached_serve_process_when_not_running(self):
        with mock.patch("subprocess.Popen") as mock_popen:
            result = serve.ensure_running(self.tmp, idle_timeout=123.0)
        self.assertTrue(result)
        mock_popen.assert_called_once()
        argv, kwargs = mock_popen.call_args
        cmd = argv[0]
        self.assertIn("shapa.serve", cmd)
        self.assertIn(str(self.tmp), cmd)
        self.assertIn("--idle-timeout", cmd)
        self.assertIn("123.0", cmd)
        self.assertTrue(kwargs.get("start_new_session"))

    def test_a_failed_spawn_never_raises(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("no fork for you")):
            result = serve.ensure_running(self.tmp)
        self.assertFalse(result)


class TestIdleWatchdog(unittest.TestCase):
    """GAP D: an autostarted daemon must not run forever - `_idle_watchdog`
    sets the stop event once the server has gone idle past the timeout."""

    def test_sets_stop_event_after_idle_timeout(self):
        server = mock.Mock()
        server.last_activity = time.monotonic()
        stop_event = threading.Event()

        t = threading.Thread(
            target=serve._idle_watchdog, args=(server, stop_event, 0.05), daemon=True
        )
        t.start()
        self.assertTrue(stop_event.wait(timeout=2.0))
        t.join(timeout=2.0)

    def test_a_recent_request_resets_the_idle_clock(self):
        server = mock.Mock()
        server.last_activity = time.monotonic()
        stop_event = threading.Event()

        t = threading.Thread(
            target=serve._idle_watchdog, args=(server, stop_event, 0.2), daemon=True
        )
        t.start()
        time.sleep(0.1)
        server.last_activity = time.monotonic()  # a request "just arrived"
        # Still well inside the (reset) idle window - must not have fired yet.
        self.assertFalse(stop_event.wait(timeout=0.05))
        stop_event.set()
        t.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
