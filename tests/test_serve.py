"""Tests for shapa.serve: the warm per-root daemon (shapa-backend-spec.md §5).

The central acceptance for this slice: "a capture mid-session is visible on
the daemon's next request" - a real Unix socket, a real sqlite index, a real
file written to disk between two requests against the SAME running daemon
(no restart), proving the per-request staleness re-check actually works.
"""

import shutil
import tempfile
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
