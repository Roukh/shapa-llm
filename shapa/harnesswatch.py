"""``shapa harness watch`` - notice when a harness changes its own default prompt.

shapa's adapters are written against a harness's default behavior; when a harness
release or a remote catalog changes that default, the adapter may need a review.
This module captures what it can *locally* and stores only fingerprints, never the
text (a vendor's prompt is theirs, and the repository that holds shapa is public):
a sha-256, a character count and the list of markdown headings, per item, in
``~/.shapa/harness-watch.json`` (``$SHAPA_HOME`` overrides ``~/.shapa``).

Sources, all offline:

* Codex: the live model catalog cache ``$CODEX_HOME/models_cache.json``; one item
  per model slug, from its ``instructions_template``.
* Claude Code: with ``--capture-claude`` only, ``claude -p`` runs once against a
  loopback endpoint (a scratch HOME, a dummy key, no hooks, closed stdin; nothing
  leaves the machine) and the system blocks and tool descriptions it sends are
  fingerprinted.
* Every detected harness: its ``--version`` output.

Gemini CLI and OpenCode keep their defaults inside the binary or behind a network
call, and Cursor's are closed: those are skipped with a note, as are upstream
repository diffs, which need the network.

The first run records the baseline. Later runs compare against it and keep the
differences as pending drift until ``--accept`` adopts the new capture, so
``shapa doctor`` keeps printing the drift line until someone reviewed the adapter.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from shapa import harness

HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
MAX_HEADINGS, HEADING_CHARS = 80, 80
CAPTURE_TIMEOUT = 120


def watch_path() -> Path:
    return harness.shapa_home() / "harness-watch.json"


def fingerprint(text: str) -> dict:
    heads = [h[:HEADING_CHARS] for h in HEADING.findall(text)][:MAX_HEADINGS]
    return {"sha256": harness.sha(text), "chars": len(text), "headings": heads}


def _load() -> dict:
    try:
        data = json.loads(watch_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    fs = harness.FS(False, False)
    fs.stage(watch_path(), json.dumps(data, indent=2, sort_keys=True) + "\n")
    fs.flush()


def _version(h: str) -> str | None:
    binary = shutil.which(harness.BINARIES[h])
    if not binary:
        return None
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5,
                           check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = next((ln.strip() for ln in (r.stdout or r.stderr).splitlines() if re.search(r"\d+\.\d+", ln)), "")
    return line[:80] or None


def capture_codex() -> tuple[dict, list[str]]:
    path = harness.config_root("codex") / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, [f"codex: no readable {path.name} (run codex once online to create it)"]
    models = data.get("models") if isinstance(data, dict) else data
    items: dict = {}
    for m in models if isinstance(models, list) else []:
        if isinstance(m, dict) and isinstance(m.get("slug"), str) and isinstance(m.get("instructions_template"), str):
            items[f"codex:{m['slug'][:60]}"] = fingerprint(m["instructions_template"])
    return items, ([] if items else ["codex: the model catalog cache holds no instruction templates"])


def capture_claude_defaults() -> tuple[dict, list[str]]:
    binary = shutil.which("claude")
    if not binary:
        return {}, ["claude-code: the claude binary is not on PATH"]
    bodies: list[bytes] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("content-length", 0) or 0)
            bodies.append(self.rfile.read(min(n, 8_000_000)))
            msg = b'{"type":"error","error":{"type":"invalid_request_error","message":"capture only"}}'
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    scratch = tempfile.mkdtemp(prefix="shapa-watch-")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": scratch, "ANTHROPIC_API_KEY": "sk-ant-dummy-capture-only",
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
           "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    try:
        subprocess.run([binary, "-p", "hi", "--settings", '{"hooks":{}}'], env=env, cwd=scratch,
                       capture_output=True, text=True, timeout=CAPTURE_TIMEOUT, check=False,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, [f"claude-code: capture failed ({exc.__class__.__name__})"]
    finally:
        server.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)
    for raw in bodies:
        try:
            body = json.loads(raw)
        except ValueError:
            continue
        sysv = body.get("system")
        blocks = [b.get("text", "") for b in sysv if isinstance(b, dict)] if isinstance(sysv, list) \
            else ([sysv] if isinstance(sysv, str) else [])
        tools = body.get("tools") if isinstance(body.get("tools"), list) else []
        names = sorted(str(t.get("name")) for t in tools if isinstance(t, dict))
        tool_fp = {"sha256": harness.sha(json.dumps(tools, sort_keys=True)),
                   "chars": len(json.dumps(tools)), "headings": names[:MAX_HEADINGS]}
        return {"claude-code:system": fingerprint("\n\n".join(blocks)), "claude-code:tools": tool_fp}, []
    return {}, ["claude-code: the capture produced no request"]


def compare(baseline: dict, items: dict) -> list[str]:
    out = []
    for key, fp in sorted(items.items()):
        old = baseline.get(key)
        if old is None or old.get("sha256") == fp["sha256"]:
            continue
        a, b = set(old.get("headings", [])), set(fp["headings"])
        out.append(f"{key}: changed ({old.get('chars', 0)} -> {fp['chars']} chars, "
                   f"headings +{len(b - a)} -{len(a - b)})")
    return out


def run(capture_claude: bool = False, accept: bool = False, as_json: bool = False) -> int:
    state = _load()
    base_items = dict(state.get("items", {}))
    base_versions = dict(state.get("versions", {}))
    items: dict = {}
    notes: list[str] = []
    versions: dict = {}
    captured: set[str] = set()
    for h in harness.SUPPORTED:
        v = _version(h)
        if v:
            versions[h] = v
    got, n = capture_codex()
    items.update(got)
    notes += n
    if got:
        captured.add("codex")
    if capture_claude:
        got, n = capture_claude_defaults()
        items.update(got)
        notes += n
        if got:
            captured.add("claude-code")
    else:
        notes.append("claude-code: default prompt not captured (needs --capture-claude, which runs `claude -p` "
                     "against a loopback endpoint)")
    notes += ["gemini, opencode: defaults live in the binary or behind the network; skipped",
              "cursor: closed source; skipped",
              "upstream repository diffs need the network and are not fetched here"]

    pending = [p for p in state.get("pending", []) if p.split(":", 1)[0] not in captured | set(versions)]
    if base_items:
        pending += compare(base_items, items)
    for h, v in versions.items():
        old = base_versions.get(h)
        if old and old != v:
            pending.append(f"{h}: version {old} -> {v}")
    first = not base_items and not base_versions
    new_state = {"version": 1, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if first or accept:
        new_state["items"] = {**base_items, **items}
        new_state["versions"] = {**base_versions, **versions}
        new_state["pending"] = []
        pending = []
    else:  # the baseline stays until accepted; new items extend it quietly
        new_state["items"] = {**{k: v for k, v in items.items() if k not in base_items}, **base_items}
        new_state["versions"] = {**{k: v for k, v in versions.items() if k not in base_versions}, **base_versions}
        new_state["pending"] = sorted(dict.fromkeys(pending))
    try:
        _save(new_state)
    except (OSError, harness.HarnessError) as exc:
        notes.append(f"could not store {watch_path()} ({exc})")
    report = {"first_run": first, "accepted": accept, "drift": new_state["pending"], "versions": versions,
              "items": {k: {"chars": v["chars"], "headings": len(v["headings"])} for k, v in items.items()},
              "notes": notes}
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print("harness watch: " + ("baseline recorded" if first else ("baseline updated" if accept else
              ("DRIFT" if new_state["pending"] else "no change"))))
        for h, v in versions.items():
            print(f"  {h} {v}")
        for p in new_state["pending"]:
            print(f"  drift: {p}")
        for n in notes:
            print(f"  note: {n}")
        if new_state["pending"]:
            print("  review the adapter, then `shapa harness watch --accept`")
    return 1 if new_state["pending"] else 0


def pending() -> list[str]:
    """The drift lines `shapa doctor` prints (empty when nothing is pending)."""
    return [str(p) for p in _load().get("pending", [])][:6]
