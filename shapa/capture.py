"""Capture (the write path) - distil a finished session into a memory note.

Runs as a Stop / SubagentStop hook: at the end of an agent's work it reads the
session transcript, summarises what the operator asked for, and writes (or
updates) ONE memory note per session into the connected wiki root, linking it to [[memory]]
and to any existing notes/topics it mentions so it joins the graph.

This is a heuristic, standard-library-only capture - it records the operator's
requests as a memory, it does not call a language model to judge salience. A
smarter LLM-distilled mode could be layered on later. It is write-only toward
the agent and always exits 0, so it never blocks.

Hook usage (stdin JSON from Stop/SubagentStop):
    echo '{"transcript_path":"...","session_id":"abc"}' | python3 -m shapa.capture
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from shapa import config, frontmatter
from shapa.maintain import _shingles, _tokens
from shapa.nodes import load_nodes
from shapa.validate import validate_node

DEDUP_THRESHOLD = 0.9  # containment: skip if this much of the new note already exists

_WORD_RE = re.compile(r"[a-z][a-z0-9-]{2,}")


def _user_texts(transcript_path: str) -> list[str]:
    """Best-effort extraction of user-message texts from a transcript .jsonl."""
    out: list[str] = []
    p = Path(transcript_path)
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        msg = obj.get("message", obj)
        role = msg.get("role") or obj.get("role") or obj.get("type")
        if role != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = ""
        text = text.strip()
        # Skip tool-result / hook-injected noise.
        if text and not text.startswith(("<", "{")):
            out.append(text)
    return out


def _summary(texts: list[str], limit: int = 600) -> str:
    if not texts:
        return "A session with no recorded operator request."
    joined = " / ".join(" ".join(t.split()) for t in texts)
    if len(joined) > limit:
        joined = joined[:limit].rsplit(" ", 1)[0] + " ..."
    return joined


def _topic_links(summary: str, existing_ids: set[str], k: int = 6) -> list[str]:
    """Link the capture note to existing notes/topics it mentions (so it is not
    an orphan and lands in the right cluster)."""
    words = set(_WORD_RE.findall(summary.lower()))
    hits = [i for i in sorted(existing_ids) if i.lower() in words]
    return hits[:k]


def capture_session(transcript_path: str, session_id: str, root=None,
                    now: datetime | None = None) -> Path | None:
    """Write/update the per-session memory note. Returns the path, or None."""
    root = config.resolve(root)
    root.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)

    sid = (session_id or "unknown")[:8]
    note_id = f"memory-session-{sid}"
    path = root / f"{note_id}.md"

    summary = _summary(_user_texts(transcript_path))

    # Preserve created/uses if the note already exists (idempotent updates).
    created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    uses = 0
    if path.is_file():
        meta = frontmatter.parse(path).meta
        created = str(meta.get("created") or created)
        try:
            uses = max(0, int(str(meta.get("uses", 0))))
        except ValueError:
            uses = 0

    existing = {nid for nid in load_nodes(root) if nid != note_id}
    links = _topic_links(summary, existing)
    related = " ".join(f"[[{i}]]" for i in links)

    body = (
        f"Captured at the end of session {sid}. The operator's requests this "
        f"session: {summary}\n\n"
        f"Type: [[memory]]." + (f" Related: {related}." if related else "")
    )

    # Dedup: if an existing note already CONTAINS ~all of this note's salient
    # content (the operator's requests, ignoring the per-session wrapper), do
    # not double it. Containment (new ∩ other / new) handles longer existing notes.
    new_sh = _shingles(_tokens(summary))
    if new_sh:
        for other_id, other in load_nodes(root).items():
            if other_id == note_id:
                continue
            other_sh = _shingles(_tokens(frontmatter.parse(other.path).body))
            if len(new_sh & other_sh) / len(new_sh) >= DEDUP_THRESHOLD:
                return None  # content already captured elsewhere; skip

    text = (
        "---\n"
        f"id: {note_id}\n"
        "type: memory\n"
        f'created: "{created}"\n'
        "consequence: 4\n"
        "locus: output-meta\n"
        f"uses: {uses}\n"
        "---\n"
        f"{body}\n"
    )
    path.write_text(text, encoding="utf-8")

    # Self-check: if somehow invalid, remove rather than leave a broken note.
    if not validate_node(path).valid:
        path.unlink(missing_ok=True)
        return None
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.capture",
        description="Capture a finished session into a memory note (write path).",
    )
    parser.add_argument("--transcript", help="Transcript .jsonl path (manual test).")
    parser.add_argument("--session", default="manual", help="Session id (manual test).")
    parser.add_argument("--root", default=None,
                        help="Memory directory (default: $SHAPA_MEMORY or ~/.shapa/memory).")
    args = parser.parse_args(argv)

    transcript = args.transcript
    session = args.session
    if transcript is None:
        try:
            data = json.load(sys.stdin)
            if isinstance(data, dict):
                transcript = data.get("transcript_path", "")
                session = data.get("session_id", session)
        except (json.JSONDecodeError, ValueError):
            transcript = ""

    try:
        if transcript:
            capture_session(transcript, session, root=args.root)
    except Exception:
        pass  # never block the agent
    sys.exit(0)


if __name__ == "__main__":
    main()
