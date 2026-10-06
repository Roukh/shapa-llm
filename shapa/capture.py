"""Capture (the write path) - distil a finished session into memory-v3 records.

Runs as a Stop / SubagentStop hook: at the end of an agent turn it reads the
session transcript and extracts a handful of ATOMIC memories (never a whole
note) from two places only. A SubagentStop call is skipped entirely by
default (``capture_subagents=True`` / ``--capture-subagents`` /
``SHAPA_CAPTURE_SUBAGENTS=1`` opts back in): a subagent's final report is an
intermediate work product FOR the parent agent, not a session's own job
report, and capturing it by default turned every subagent turn's status
fragments ("**HK1** ... NOT STARTED.") into junk records - the parent's own
Stop capture over the main transcript already turns the finished work into
memories, in the standard job-report shape below.

- the session's FINAL assistant message - the operator's standard 3-paragraph
  job report (after-action / footprint / open decisions) maps to one
  ``outcome``, one ``fact``, and zero or more ``open_question`` records; any
  other shape falls back to up to three long paragraphs classified by
  :func:`shapa.memlog.classify_kind`.
- the operator's own requests (every user turn except the very first - the
  raw task brief is never stored) - sentences that carry a decision/
  preference cue (never, always, from now on, prefer, instead of, must, ...)
  become ``preference`` records (or ``gotcha`` when a gotcha keyword fires).

Every record is built with :func:`shapa.memlog.make_record` (redaction,
length clamping, content-derived id) and written with
:func:`shapa.memlog.append` (exact/near-dup skip, supersede detection) - this
module never writes a log line directly and never writes a ``.md`` note
(unlike the v2 ``memory-session-<sid>.md`` this replaces).

Routing is the placement rule applied per record, not per session: a repo
session writes its own repo wiki; a ``preference`` pulled from an operator
request lands in the global wiki instead only when it carries a global cue
(always/never/from now on/every repo/...) AND no repo-specific token (path,
file extension, repo name) - everything else from a repo with no wiki of its
own is dropped rather than parked in the global wiki. ``--root``/``--scope``
(manual overrides, matching ``shapa save``'s CLI scopes) bypass all of this
and send every record to one explicit target.

Parsing is incremental: a per-``(session, transcript)`` byte offset lives in
a capture-owned table inside the target root's gitignored
``.shapa-index.db`` (:mod:`shapa.store`), so a long-running session's
transcript is only ever re-parsed from where the last Stop left off; the
first-operator-message skip is tracked the same way so it survives across
runs. The hook never blocks and never prints (``--dry-run`` is the only mode
that prints, and it writes nothing): every exception is swallowed, every
malformed/missing/binary/huge transcript is tolerated, and
:func:`main` always exits 0.

An optional LLM-distill mode (off by default; ``--distill`` or
``SHAPA_CAPTURE_DISTILL=1``) asks a model for the memory list instead of the
heuristics above, falling back to them on any failure. The hook spawns a
detached background worker for it so the model call never adds hook latency.

Hook usage (stdin JSON from Stop/SubagentStop)::

    echo '{"transcript_path":"...","session_id":"abc"}' | python3 -m shapa.capture
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime

from shapa import config, db, ledger, memlog, redact, store

#: The scopes a manual capture invocation may target (mirrors `shapa save`).
#: "external" is CLI-only convenience: it writes into a DIFFERENT repo's own
#: wiki (found by name), stored as scope "repo" there - never "external" as a
#: stored value (same convention as shapa/save.py).
CAPTURE_SCOPES = ("global", "repo", "external")

#: Hard caps on what one capture call ever emits - a flood guard independent
#: of memlog.append's own MAX_APPEND (which still applies on top of these).
CAPTURE_MAX_RECORDS = 12
REQUEST_MAX = 4

#: How long the optional distill worker waits for `claude -p` before giving
#: up and falling back to the heuristic records.
DISTILL_TIMEOUT = 25

#: Input bounds that keep the hook's cost independent of how big a session
#: got: a transcript line past MAX_ENTRY_BYTES (an attachment, a pasted dump)
#: is skipped unparsed, and the final message / each operator request is
#: read only up to these many chars. A job report is ~1 KB.
MAX_ENTRY_BYTES = 2_000_000
MAX_FINAL_CHARS = 32_768
MAX_REQUEST_CHARS = 8_192


# --- text shaping --------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+][ \t]+|#{1,6}[ \t]+|\d+[.)][ \t]+)", re.M)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_OPEN_DECISIONS_PREFIX_RE = re.compile(r"(?i)^open decisions\s*:\s*")
_NUM_ITEM_RE = re.compile(r"\d+\)\s*")
_PASTED_RE = re.compile(r"<pasted_content[^>]*>.*?</pasted_content>", re.S | re.I)
#: An opening tag with no matching close (the transcript never finished
#: recording the paste) - everything from here to the end of the text is
#: dropped too, since none of it is safe to treat as the operator's own
#: words (see :func:`_strip_pasted_content`).
_PASTED_OPEN_RE = re.compile(r"<pasted_content\b[^>]*>", re.I)

#: Cue a sentence pulled from an operator request must carry to be captured
#: as a `preference` at all (spec: never/always/don't.../stop/from now
#: on/prefer/instead of/must/"use X not Y").
_PREF_CUE_RE = re.compile(
    r"\b(never|always|don'?t|do not|stop|from now on|prefer(?:s|red)?|instead of|must)\b"
    r"|\buse\b[^.?!\n]{0,60}\bnot\b", re.I)
#: A "clearly global" cue (routing rule) - distinct from the pref cue above:
#: a preference without one of these never leaves its repo's own wiki.
_GLOBAL_CUE_RE = re.compile(
    r"\b(always|never|from now on|every repo|all repos|globally|across repos|in any repo|"
    r"all sessions|every session|any session|across sessions)\b", re.I)
_FILE_EXT_RE = re.compile(r"\b[\w-]{2,}\.[A-Za-z]{1,5}\b")
_PATHLIKE_RE = re.compile(r"[\w.-]+/[\w./-]+")

#: A fragment too thin to be worth its own memory record: bare filler
#: ("yes."/"none."/"ok", optionally with trailing punctuation) or - once
#: normalized - just a handful of characters. Catches junk like an
#: ``outcome``/``open_question`` pair reduced to "Yes."/"none." (spec: a
#: fragment this short is never atomic content, it is a leftover of
#: whatever shape the final message almost had).
_FILLER_ONLY_RE = re.compile(
    r"^(?:yes|no|none|n/?a|ok(?:ay)?|sure|done|fine|good|agreed|correct|confirmed|true|false)"
    r"[.!?]*$", re.I)
MIN_FRAGMENT_CHARS = 12

#: Interim progress/status chatter about work still in flight ("Four
#: verification agents are running in parallel", "The CI/CD group is
#: back.") - never a completed-work outcome, even though it default-
#: classifies as one (no gotcha/open/preference cue fires). Only consulted
#: when the candidate's kind would otherwise be "outcome" (see
#: :func:`_report_kind`): a paragraph this phrasing shares with a real
#: gotcha/decision is never dropped on its account.
_PROGRESS_CHATTER_RE = re.compile(
    r"\b(?:is|are|was|were)\s+(?:currently\s+)?(?:running|back|ongoing|in[- ]progress|"
    r"still\s+(?:running|going|working)|waiting|pending|under\s?way)\b"
    r"|\brunning in parallel\b", re.I)


def _is_trivial_fragment(text: str) -> bool:
    """True for filler-only text, or text too short (once collapsed to one
    line and stripped of trailing punctuation) to be an atomic memory on
    its own."""
    one = memlog.one_line(text)
    if _FILLER_ONLY_RE.match(one.strip()):
        return True
    return len(one.strip(" .!?")) < MIN_FRAGMENT_CHARS


def _is_in_flight_chatter(text: str) -> bool:
    return bool(_PROGRESS_CHATTER_RE.search(text))


def _report_kind(text: str, default: str) -> str:
    """:func:`shapa.memlog.classify_kind` for report-derived text (the
    final job report, never an operator request), with one override: a
    report paragraph is never classified ``preference`` - that kind is
    reserved for a stated operator request (see the module docstring's
    routing rule), and a work report can trip ``_PREF_KW`` incidentally
    (e.g. "the fix must always re-run after a seed step") without actually
    being one."""
    kind = memlog.classify_kind(text, default)
    return default if kind == "preference" else kind


def _strip_markdown(text: str) -> str:
    """Drop fenced code blocks and leading bullet/header/numbered-list
    markers before paragraph splitting (never touches inline text like the
    report's own "Open decisions: 1) ... 2) ..." - that numbering is never
    at the start of a line)."""
    return _BULLET_RE.sub("", _CODE_FENCE_RE.sub(" ", text or ""))


def _paragraphs(text: str) -> list[str]:
    cleaned = _strip_markdown(text)
    blocks = re.split(r"\n\s*\n", cleaned)
    return [p for p in (memlog.one_line(b) for b in blocks) if p]


def _sentences(text: str) -> list[str]:
    text = memlog.one_line(text)
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()] if text else []


def _first_sentence(text: str) -> str:
    sents = _sentences(text)
    return sents[0] if sents else memlog.one_line(text)


def _is_standard_report(paras: list[str]) -> bool:
    """Exactly 3 plain paragraphs, the third opening "Open decisions:" - the
    operator's standard end-of-task job report shape."""
    return len(paras) == 3 and bool(_OPEN_DECISIONS_PREFIX_RE.match(paras[2]))


#: Below this many characters, a numbered-ledger item reads as a bare
#: fragment ("Oxlint or ESLint", "herdr hook scope") rather than a decision
#: clause someone could act on alone.
_MIN_LEDGER_ITEM_CHARS = 25


def _open_decisions_items(p3: str) -> list[str]:
    """P3's numbered items ("1) ... 2) ..."), or [] for "Open decisions:
    none." (any spelling of "none") or bare filler. A ledger of two or more
    SHORT fragments ("1) pick a linter 2) fix hook scope 3) sort billing")
    reads as one decision list, not N atomic decisions each worth its own
    record - it stays a single item (the whole list, minus the prefix) so
    it is captured as one record instead of exploding into a record per
    fragment. A ledger whose items are each a substantial clause on their
    own (the common case) still splits, unchanged."""
    body = _OPEN_DECISIONS_PREFIX_RE.sub("", p3).strip()
    if not body or _is_trivial_fragment(body):
        return []
    parts = [x for x in (memlog.one_line(y) for y in _NUM_ITEM_RE.split(body)) if x]
    if not parts:
        return []
    if len(parts) > 1 and any(len(p) < _MIN_LEDGER_ITEM_CHARS for p in parts):
        return [memlog.one_line(body)]
    return [p for p in parts if not _is_trivial_fragment(p)]


def _is_clearly_global(text: str, repo_name: str | None) -> bool:
    """The routing rule's "clearly global" test: a global cue, AND no
    repo-specific token (path, file extension, commit/PR-like token, or the
    repo's own name) anywhere in *text*."""
    if not _GLOBAL_CUE_RE.search(text):
        return False
    if _PATHLIKE_RE.search(text) or _FILE_EXT_RE.search(text) or memlog.extract_tags(text):
        return False
    if repo_name and re.search(r"\b" + re.escape(repo_name) + r"\b", text, re.I):
        return False
    return True


def _strip_pasted_content(text: str) -> str:
    """Strip every ``<pasted_content ...>...</pasted_content>`` block (kept
    text around a closed one survives). An opening tag with no matching
    close - a paste the transcript never finished recording - drops
    everything from that tag to the end of *text* too: there is no way to
    know where the paste would have ended, so none of what follows is safe
    to treat as the operator's/report's own words."""
    text = _PASTED_RE.sub(" ", text or "")
    return _PASTED_OPEN_RE.split(text, maxsplit=1)[0]


def _clean_user_text(raw: str) -> str:
    """Strip any embedded ``<pasted_content>`` blocks (kept text around them
    survives) before the noise checks below."""
    return _strip_pasted_content(raw or "").strip()


# --- transcript entries ---------------------------------------------------------

def _parse_entry(line: str) -> dict | None:
    line = line.strip()
    if not line or len(line) > MAX_ENTRY_BYTES:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _entry_role(obj: dict) -> str | None:
    msg = obj.get("message")
    role = msg.get("role") if isinstance(msg, dict) else None
    return role or obj.get("role") or obj.get("type")


def _entry_content_blocks(obj: dict) -> list[dict]:
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else obj.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _text_blocks(blocks: list[dict]) -> str:
    texts = [b.get("text") for b in blocks
             if b.get("type") == "text" and isinstance(b.get("text"), str)]
    return "\n".join(t for t in texts if t)


def _is_tool_result_entry(obj: dict, blocks: list[dict]) -> bool:
    if obj.get("toolUseResult") is not None:
        return True
    return any(b.get("type") == "tool_result" for b in blocks)


def _operator_messages(entries: list[dict]) -> list[str]:
    """Qualifying operator-request texts, in order: user-role, not a tool
    result, not isMeta/isSidechain, not a hook/system injection (starts with
    '<'), not interrupt noise. The caller drops the very first one ever seen
    in the session (the raw task brief is never stored)."""
    out = []
    for obj in entries:
        if obj.get("isMeta") or obj.get("isSidechain"):
            continue
        if _entry_role(obj) != "user":
            continue
        blocks = _entry_content_blocks(obj)
        if _is_tool_result_entry(obj, blocks):
            continue
        text = _clean_user_text(_text_blocks(blocks))
        if not text or text.startswith("<") or text.startswith("[Request interrupted"):
            continue
        out.append(text)
    return out


def _final_assistant_text(entries: list[dict]) -> str:
    """The text blocks of the last contiguous run of assistant-role entries
    (sidechain entries - a parallel subagent thread - are never part of
    it)."""
    main = [e for e in entries if not e.get("isSidechain")]
    run: list[dict] = []
    for obj in reversed(main):
        if _entry_role(obj) == "assistant":
            run.append(obj)
            continue
        break
    run.reverse()
    texts = [_text_blocks(_entry_content_blocks(obj)) for obj in run]
    return "\n\n".join(t for t in texts if t)


# --- candidate extraction -------------------------------------------------------

def _final_message_candidates(final_text: str) -> list[dict]:
    """Atomic candidates from the session's final assistant message. Each
    dict carries ``kind``/``summary``/``body``/``tags``/``report`` (the last
    marks it as report-derived - never eligible for the "clearly global"
    preference promotion, which is request-only)."""
    if not final_text or not final_text.strip():
        return []
    paras = _paragraphs(final_text)
    out: list[dict] = []
    if _is_standard_report(paras):
        p1, p2, p3 = paras
        p1_kind = _report_kind(p1, "outcome")
        if not _is_trivial_fragment(p1) and not (p1_kind == "outcome" and _is_in_flight_chatter(p1)):
            out.append({"kind": p1_kind, "summary": _first_sentence(p1),
                        "body": p1, "tags": [], "report": True})
        if not _is_trivial_fragment(p2):
            out.append({"kind": _report_kind(p2, "fact"), "summary": _first_sentence(p2),
                        "body": p2, "tags": memlog.extract_tags(p2), "report": True})
        for item in _open_decisions_items(p3):
            out.append({"kind": _report_kind(item, "open_question"),
                        "summary": _first_sentence(item), "body": item, "tags": [], "report": True})
        return out
    if len(memlog.one_line(final_text)) < 80:
        return []
    for p in paras:
        if len(p) < 80:
            continue
        kind = _report_kind(p, "outcome")
        if kind == "outcome" and _is_in_flight_chatter(p):
            continue
        out.append({"kind": kind, "summary": _first_sentence(p), "body": p, "tags": [], "report": True})
        if len(out) >= 3:
            break
    return out


def _request_candidates(texts: list[str]) -> list[dict]:
    """Up to :data:`REQUEST_MAX` preference/gotcha candidates, one per
    qualifying sentence across every operator-request text given."""
    out: list[dict] = []
    for text in texts:
        for sentence in _sentences(text):
            if not (20 <= len(sentence) <= 300):
                continue
            if not _PREF_CUE_RE.search(sentence):
                continue
            kind = memlog.classify_kind(sentence, "preference")
            out.append({"kind": kind, "summary": sentence, "body": sentence, "tags": [],
                        "report": False})
            if len(out) >= REQUEST_MAX:
                return out
    return out


def _source_for(cand: dict, is_subagent: bool) -> str:
    if is_subagent:
        return "capture:subagent"
    return "capture:report" if cand.get("report", True) else "capture:request"


# --- incremental transcript reading ---------------------------------------------

def _read_new_chunk(path_str: str, offset: int) -> tuple[str, int]:
    """Bytes of *path_str* from *offset* up to (and including) the last
    complete line - a line still being written is left for next time. Never
    raises: a missing file, a directory, or any OSError yields no new text."""
    if not path_str:
        return "", offset
    p = Path(path_str)
    try:
        if not p.is_file():
            return "", offset
        with p.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return "", offset
    nl = data.rfind(b"\n")
    end = nl + 1 if nl >= 0 else 0
    if end == 0:
        return "", offset
    return data[:end].decode("utf-8", errors="replace"), offset + end


def _file_size(path_str: str) -> int:
    try:
        return Path(path_str).stat().st_size if path_str else 0
    except OSError:
        return 0


# --- capture_state (in the target root's .shapa-index.db) ----------------------

_STATE_SCHEMA = """CREATE TABLE IF NOT EXISTS capture_state (
    session TEXT NOT NULL, transcript TEXT NOT NULL, offset INTEGER NOT NULL,
    first_prompt_seen INTEGER NOT NULL DEFAULT 0, updated TEXT,
    PRIMARY KEY (session, transcript))"""


def _load_state(root, session: str, transcript: str) -> tuple[int, bool]:
    try:
        conn = store.open_index(root, read_only=True)
    except (FileNotFoundError, OSError, sqlite3.Error):
        return 0, False
    try:
        row = conn.execute(
            "SELECT offset, first_prompt_seen FROM capture_state WHERE session=? AND transcript=?",
            (session, transcript)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    return (int(row[0] or 0), bool(row[1])) if row is not None else (0, False)


def _save_state(root, session: str, transcript: str, offset: int, first_prompt_seen: bool) -> None:
    try:
        conn = store.open_index(root)
    except (OSError, sqlite3.Error):
        return
    try:
        conn.execute(_STATE_SCHEMA)
        conn.execute(
            "INSERT INTO capture_state(session, transcript, offset, first_prompt_seen, updated) "
            "VALUES (?,?,?,?,?) ON CONFLICT(session, transcript) DO UPDATE SET "
            "offset=excluded.offset, first_prompt_seen=excluded.first_prompt_seen, "
            "updated=excluded.updated",
            (session, transcript, offset, int(first_prompt_seen), memlog.now_iso()))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


# --- routing ---------------------------------------------------------------------

def _git_root(start) -> Path | None:
    try:
        here = Path(start).resolve() if start is not None else Path.cwd().resolve()
    except OSError:
        return None
    return config._git_toplevel(here)


def _manual_root_target(root, start) -> tuple[Path, str, str | None]:
    resolved = config.resolve(root)
    try:
        is_global = resolved.resolve() == config.global_root().resolve()
    except OSError:
        is_global = resolved == config.global_root()
    if is_global:
        return resolved, "global", None
    git_root = _git_root(start)
    return resolved, "repo", (git_root.name if git_root else None)


def _manual_scope_target(scope: str, applies_to: str | None, start) -> tuple[Path, str, str | None]:
    """Mirrors `shapa save`'s scope resolution, but (like v2's capture) never
    errors - an unresolvable target degrades to the ordinary repo/global
    fallback instead (the hook must never block)."""
    if scope == "global":
        return config.global_root(), "global", None
    if scope == "external" and applies_to:
        repo_path = config.find_sibling_repo(applies_to, start)
        if repo_path is not None:
            root = config.discover(repo_path) or (repo_path / ".shapa")
            return root, "repo", repo_path.name
    found = config.discover(start)
    if found is not None:
        git_root = _git_root(start)
        return found, "repo", (git_root.name if git_root else None)
    return config.global_root(), "global", None


def _route(cand: dict, manual, base_root, base_scope: str, repo_name: str | None,
          has_git_no_wiki: bool, global_root_path: Path) -> tuple[Path, str, str | None] | None:
    """Where one candidate record lands, or ``None`` to drop it (never
    written anywhere)."""
    if manual is not None:
        return manual
    text = f"{cand['summary']} {cand['body']}"
    is_global_pref = (cand["kind"] == "preference" and not cand.get("report", True)
                      and _is_clearly_global(text, repo_name))
    if is_global_pref:
        return global_root_path, "global", None
    if has_git_no_wiki:
        return None  # never park a repo memory in the global wiki
    if base_root is None:
        return global_root_path, "global", None
    return base_root, base_scope, (repo_name if base_scope == "repo" else None)


# --- optional LLM distill --------------------------------------------------------

_MEMORIES_RE = re.compile(r"<memories>(.*?)</memories>", re.S)


def _distill_command() -> list[str]:
    raw = os.environ.get("SHAPA_DISTILL_CMD")
    if raw:
        return shlex.split(raw)
    return ["claude", "-p", "--settings", json.dumps({"hooks": {}})]


def _distill_prompt(request_texts: list[str], final_text: str) -> str:
    parts = []
    if request_texts:
        parts.append("Operator requests this session:\n" + "\n".join(f"- {t}" for t in request_texts))
    if final_text:
        parts.append("Final assistant message:\n" + final_text)
    body = "\n\n".join(parts) or "(no content captured this turn)"
    return (
        "Extract the atomic, durable memories worth keeping from this agent session.\n"
        "Return ONLY a JSON array of objects with keys kind (one of: decision, fact, "
        "gotcha, outcome, open_question, preference), summary, body, tags (list of "
        "strings), wrapped in <memories></memories> tags. No other output.\n\n" + body
    )


def _try_distill(request_texts: list[str], final_text: str) -> list[dict] | None:
    """Ask the configured distill command for the memory list; ``None`` on
    any failure at all (bad exit, timeout, unparsable output) - the caller
    falls back to the heuristic candidates."""
    try:
        # The prompt goes in on stdin, redacted: session text never reaches
        # argv (visible to every process) or the model unscrubbed.
        proc = subprocess.run(
            _distill_command(), input=redact.redact(_distill_prompt(request_texts, final_text)),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(), timeout=DISTILL_TIMEOUT, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    m = _MEMORIES_RE.search(proc.stdout or "")
    if not m:
        return None
    try:
        items = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(items, list):
        return None
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind") if it.get("kind") in memlog.KINDS else "fact"
        summary = str(it.get("summary") or "").strip()
        if not summary:
            continue
        body = str(it.get("body") or summary)
        tags = [t for t in (it.get("tags") or []) if isinstance(t, str)]
        out.append({"kind": kind, "summary": summary, "body": body, "tags": tags,
                    "report": kind != "preference"})
    return out or None


def _spawn_distill_worker(transcript, session, root, scope, applies_to, cwd,
                          agent_transcript, last_msg, capture_subagents=False) -> None:
    """Detached background re-invocation of this same module with
    ``--distill --_worker`` - the hook returns immediately; the worker does
    the (possibly slow) model call and writes on its own. The hook payload
    (which may carry the final assistant message) goes over a pipe, never
    argv."""
    argv = [sys.executable, "-m", "shapa.capture", "--_worker", "--distill"]
    if root:
        argv += ["--root", str(root)]
    if scope:
        argv += ["--scope", scope]
    if applies_to:
        argv += ["--applies-to", applies_to]
    if capture_subagents:
        argv += ["--capture-subagents"]
    payload = json.dumps({"transcript_path": str(transcript or ""), "session_id": session or "manual",
                          "cwd": str(cwd) if cwd else None,
                          "agent_transcript_path": str(agent_transcript) if agent_transcript else None,
                          "last_assistant_message": last_msg})
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.close()
    except (OSError, ValueError):
        pass


# --- the write path --------------------------------------------------------------

def _capture(transcript_path, session_id, root, now, scope, applies_to, cwd,
            agent_transcript_path, last_assistant_message, distill, dry_run,
            capture_subagents=False) -> list[memlog.Record]:
    sid = (str(session_id or "")[:8]) or "unknown"
    start = cwd  # None falls through to Path.cwd() wherever it is actually used

    is_subagent = bool(agent_transcript_path) and Path(str(agent_transcript_path)).is_file()
    if is_subagent and not capture_subagents:
        # A subagent's final report is an intermediate work product FOR
        # the parent agent, not a session's own job report - the parent's
        # own Stop capture (over the main transcript) is what should turn
        # the work into memories, already in the operator's "standard job
        # report" shape. Capturing every subagent turn by default produced
        # report-fragment junk (half a dozen "NOT STARTED"/status-line
        # records per session, several mis-kinded `preference`). Skipped
        # by default; opt in with capture_subagents=True / the CLI's
        # --capture-subagents / SHAPA_CAPTURE_SUBAGENTS=1 for a workflow
        # that genuinely wants standalone subagent memories.
        return []
    active_path = str(agent_transcript_path) if is_subagent else str(transcript_path or "")

    manual = None
    if root is not None:
        manual = _manual_root_target(root, start)
    elif scope is not None:
        manual = _manual_scope_target(scope, applies_to, start)

    repo_name = None
    base_root = None
    base_scope = "global"
    has_git_no_wiki = False
    if manual is None:
        wiki = config.discover(start)
        git_root = _git_root(start)
        repo_name = git_root.name if git_root else None
        if wiki is not None:
            base_root, base_scope = wiki, "repo"
        elif git_root is not None:
            has_git_no_wiki = True
        else:
            base_root, base_scope = config.global_root(), "global"
    global_root_path = config.global_root()
    state_root = manual[0] if manual is not None else (base_root or global_root_path)

    if dry_run:
        offset, first_prompt_seen = 0, False
    else:
        offset, first_prompt_seen = _load_state(state_root, sid, active_path)
        if _file_size(active_path) < offset:
            offset, first_prompt_seen = 0, False  # transcript truncated/replaced

    chunk_text, new_offset = _read_new_chunk(active_path, offset)
    entries = [e for e in (_parse_entry(l) for l in chunk_text.splitlines()) if e is not None]

    raw_msgs = _operator_messages(entries)
    if raw_msgs and not first_prompt_seen:
        raw_msgs = raw_msgs[1:]  # the raw task brief is never stored
        first_prompt_seen = True

    final_text = str(last_assistant_message or _final_assistant_text(entries))[:MAX_FINAL_CHARS]
    final_text = _strip_pasted_content(final_text)
    raw_msgs = [m[:MAX_REQUEST_CHARS] for m in raw_msgs]

    distilled = _try_distill(raw_msgs, final_text) if distill else None
    if distilled is not None:
        candidates = distilled[:CAPTURE_MAX_RECORDS]
    else:
        candidates = (_final_message_candidates(final_text)
                      + _request_candidates(raw_msgs))[:CAPTURE_MAX_RECORDS]

    by_root: dict[Path, list[memlog.Record]] = {}
    rows_by_root: dict[Path, list[tuple[dict, str]]] = {}
    preview: list[str] = []
    for cand in candidates:
        target = _route(cand, manual, base_root, base_scope, repo_name, has_git_no_wiki,
                        global_root_path)
        if target is None:
            continue
        troot, tscope, trepo = target
        source = f"{_source_for(cand, is_subagent)}#{sid}"
        if db.exists(troot):
            # Format 4: a memory is something the operator said, never the
            # agent's own report, and a correction is an issue the
            # UserPromptSubmit hook already logged.
            text = f"{cand['summary']} {cand['body']}"
            if source.startswith("capture:request") and not ledger.is_correction(text):
                if dry_run:
                    preview.append(f"M row -> {troot}: {cand['summary']}")
                else:
                    rows_by_root.setdefault(troot, []).append((cand, source))
            continue
        rec = memlog.make_record(
            kind=cand["kind"], summary=cand["summary"], body=cand["body"],
            tags=cand.get("tags") or [], source=source, session=sid,
            repo=trepo, scope=tscope, created=memlog.now_iso(now))
        if rec is None:
            continue
        if dry_run:
            preview.append(rec.to_json())
        else:
            by_root.setdefault(troot, []).append(rec)

    if dry_run:
        for line in preview:
            print(line)
        return []

    written: list[memlog.Record] = []
    for troot, recs in by_root.items():
        result = memlog.append(troot, recs, now=now)
        written.extend(result.written)
    for troot, cands in rows_by_root.items():
        conn = db.connect(troot)
        if conn is None:
            continue
        try:
            for cand, source in cands:
                rid, created = db.add_row(conn, "M", cand["summary"], cand["body"],
                                          tags=cand.get("tags") or [], source=source,
                                          session=sid, now=now)
                if created:
                    written.append(memlog.Record(
                        id=rid, created=memlog.now_iso(now), session=sid, repo=repo_name,
                        scope="repo", kind="memory", summary=cand["summary"],
                        body=cand["body"], tags=list(cand.get("tags") or []), source=source))
        finally:
            conn.close()
    _save_state(state_root, sid, active_path, new_offset, first_prompt_seen)
    return written


def capture_session(transcript_path, session_id, root=None, now: datetime | None = None,
                    scope: str | None = None, applies_to: str | None = None, *,
                    cwd=None, agent_transcript_path=None, last_assistant_message: str | None = None,
                    distill: bool = False, dry_run: bool = False,
                    capture_subagents: bool = False) -> list[memlog.Record]:
    """Extract and write this turn's new memories. Returns the records
    actually written (``[]`` when nothing qualified, when every candidate
    was a duplicate, when this is a SubagentStop call and
    *capture_subagents* is left ``False`` (the default), or in
    ``--dry-run`` - never ``None``, never raises)."""
    try:
        return _capture(transcript_path, session_id, root, now, scope, applies_to, cwd,
                        agent_transcript_path, last_assistant_message, distill, dry_run,
                        capture_subagents=capture_subagents)
    except Exception:
        return []


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.capture",
        description="Capture this turn's new memories into the v3 log (write path).",
    )
    parser.add_argument("--transcript", help="Transcript .jsonl path (manual test).")
    parser.add_argument("--session", default="manual", help="Session id (manual test).")
    parser.add_argument("--root", default=None,
                        help="Explicit target wiki root - overrides all routing.")
    parser.add_argument("--scope", choices=CAPTURE_SCOPES, default=None,
                        help="Explicit target scope (manual override) - overrides routing. "
                             "Default: per-record routing (see module docstring).")
    parser.add_argument("--applies-to", default=None, metavar="REPO",
                        help="With --scope external: write into a different repo's own wiki.")
    parser.add_argument("--cwd", default=None, help="cwd to route from (manual test).")
    parser.add_argument("--distill", action="store_true",
                        help="LLM-distill mode (off by default; see SHAPA_CAPTURE_DISTILL).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the extracted, redacted, routed records as JSON lines "
                             "and write nothing.")
    parser.add_argument("--capture-subagents", action="store_true",
                        help="Also turn a SubagentStop call's own final report into memory "
                             "records (off by default; see SHAPA_CAPTURE_SUBAGENTS). The "
                             "parent session's own Stop capture already covers the work.")
    parser.add_argument("--agent-transcript", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--last-assistant-message", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    transcript = args.transcript
    session = args.session
    cwd = args.cwd
    agent_transcript = args.agent_transcript
    last_msg = args.last_assistant_message
    if transcript is None:
        try:
            data = json.load(sys.stdin)
        except Exception:
            data = {}
        if isinstance(data, dict):
            transcript = data.get("transcript_path", "") or transcript
            session = data.get("session_id", session)
            cwd = data.get("cwd", cwd)
            agent_transcript = data.get("agent_transcript_path", agent_transcript)
            last_msg = data.get("last_assistant_message", last_msg)

    distill = args.distill or os.environ.get("SHAPA_CAPTURE_DISTILL") == "1"
    capture_subagents = args.capture_subagents or os.environ.get("SHAPA_CAPTURE_SUBAGENTS") == "1"

    try:
        if transcript or agent_transcript:
            if distill and not args._worker and not args.dry_run:
                _spawn_distill_worker(transcript, session, args.root, args.scope,
                                      args.applies_to, cwd, agent_transcript, last_msg,
                                      capture_subagents)
            else:
                capture_session(
                    transcript, session, root=args.root, scope=args.scope,
                    applies_to=args.applies_to, cwd=cwd, agent_transcript_path=agent_transcript,
                    last_assistant_message=last_msg, distill=distill, dry_run=args.dry_run,
                    capture_subagents=capture_subagents,
                )
    except Exception:
        pass  # never block the agent
    sys.exit(0)


if __name__ == "__main__":
    main()
