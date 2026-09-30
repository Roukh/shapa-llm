"""``shapa serve`` - the optional per-root warm daemon (shapa-backend-spec.md §5).

Latency only; correctness is unaffected either way. One Unix domain socket
per wiki root (mode ``0600``, owner-checked, named by a hash of the root's
resolved path under a short per-user tmp directory - see ``SOCKET_DIR``
below; not literally ``<root>/...`` because ``AF_UNIX`` path lengths are
capped by the OS and a wiki root nested inside a deep worktree checkout
blows through that), holding an open sqlite connection to that root's
index warm.
``fetch``/``bootstrap``/``mcp`` try the socket first (:func:`request`, a
short connect timeout) and fall back to the in-process cold path on any
failure - the daemon's absence never breaks a read, only its latency.

**Staleness fix**: every request re-syncs the index (a cheap
``(mtime_ns, size)`` stat pass, :func:`shapa.store.sync`) before answering -
not a re-embed, just a directory scan - so a ``capture``/``save`` written to
disk mid-session by some other process is reflected on THIS daemon's very
next request, without a restart. This is the fix for design 1's flagged gap
(a warm daemon that kept answering from a snapshot taken at startup).

CLI::

    shapa serve [ROOT]         # foreground; Ctrl-C / SIGTERM to stop
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

from shapa import config, store

#: NOT ``<root>/.shapa-daemon.sock``: ``AF_UNIX`` socket paths are capped at
#: ~108 bytes on Linux (``sizeof(sun_path)``), and a wiki root nested inside
#: a deep worktree checkout (exactly this project's own layout - see any
#: ``.worktrees/<branch>/`` path) blows straight through that with
#: ``OSError: AF_UNIX path too long`` - caught by this slice's own live
#: check, not a fixture. The socket instead lives in a short, per-user,
#: owner-only (0700) directory under the system tmp dir, named by a hash of
#: the root's resolved path - one socket per root, independent of how deep
#: that root happens to live.
SOCKET_DIR = Path(tempfile.gettempdir()) / f"shapa-{os.getuid()}"
#: Kept short deliberately (§5: "~0.25s connect timeout") - a slow/hung
#: daemon must fall back to the cold path fast, not stall the prompt.
CONNECT_TIMEOUT = 0.25
#: Once connected, a real request may take longer than the connect probe
#: (a cold sync on a large wiki) - generous but bounded.
READ_TIMEOUT = 5.0


def _root_key(root) -> str:
    try:
        resolved = str(Path(root).expanduser().resolve())
    except OSError:
        resolved = str(Path(root).expanduser())
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]


def socket_path(root) -> Path:
    return SOCKET_DIR / f"{_root_key(root)}.sock"


def _answer(root: Path, payload: dict) -> dict:
    """Re-sync *root*'s index (the staleness check), then answer one
    request. Never raises past this - callers get an ``{"ok": False, ...}``
    envelope instead."""
    try:
        conn = store.open_index(root)
    except Exception as exc:  # never let the accept loop die on one root
        return {"ok": False, "error": str(exc)}
    try:
        store.sync(root, conn=conn)
        cmd = payload.get("cmd")
        if cmd == "ping":
            return {"ok": True}
        if cmd == "stats":
            n = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
            return {"ok": True, "notes": n}
        if cmd == "search":
            query = str(payload.get("query", ""))
            k = payload.get("k")
            results = store.search(root, query, k=k, conn=conn)
            return {"ok": True, "results": [{"id": nid, "score": s} for nid, s in results]}
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            self.request.settimeout(READ_TIMEOUT)
            chunks = []
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if len(chunk) < 65536:
                    break  # a JSON-line client sends one write then half-closes/waits
            payload = json.loads(b"".join(chunks).decode("utf-8")) if chunks else {}
            result = _answer(self.server.shapa_root, payload)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        try:
            self.request.sendall(json.dumps(result).encode("utf-8"))
        except OSError:
            pass


class Daemon:
    """A running per-root daemon: owns the socket and a background accept
    thread. Use as a context manager (mainly tests); :func:`main` runs one
    in the foreground for a real install."""

    def __init__(self, root):
        self.root = Path(root)
        self.sock_path = socket_path(self.root)
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SOCKET_DIR, 0o700)  # owner-checked (§5) - also for a dir that pre-existed
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass

        server = socketserver.ThreadingUnixStreamServer(str(self.sock_path), _Handler)
        server.daemon_threads = True
        server.shapa_root = self.root
        os.chmod(self.sock_path, 0o600)  # owner-checked (§5)

        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self.sock_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "Daemon":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def request(root, payload: dict, timeout: float = CONNECT_TIMEOUT) -> dict | None:
    """Try the warm daemon for *root*. ``None`` on any failure (no socket,
    connect refused/timed out, malformed reply) - the caller's cold-path
    fallback, never a correctness dependency (§5)."""
    sock_path = socket_path(root)
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            sock.sendall(json.dumps(payload).encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)  # tell the handler "that's the whole request"
            sock.settimeout(READ_TIMEOUT)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            if not chunks:
                return None
            return json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.serve",
        description="Run the warm per-root daemon (latency only, optional).",
    )
    parser.add_argument("root", nargs="?", default=None,
                        help="Wiki root (default: $SHAPA_MEMORY or the discovered wiki).")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else config.resolve(None)
    daemon = Daemon(root)
    daemon.start()
    print(f"shapa serve: {root} -> {daemon.sock_path}", file=sys.stderr)

    stop_event = threading.Event()

    def _handle_signal(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        stop_event.wait()
    finally:
        daemon.stop()


if __name__ == "__main__":
    main()
