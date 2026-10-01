"""Bootstrap (the SessionStart path) - a metadata-only overview of every
wiki in scope, loaded once per session before any prompt exists.

Runs as a ``SessionStart`` hook: it reads the hook payload (JSON with at
least ``cwd``) from stdin, and prints a JSON ``{"hookSpecificOutput": ...}``
envelope whose ``additionalContext`` is the metadata-only overview the
harness injects at session start. This is Tier 1 of
shapa-backend-spec.md §4.2: only ``id/type/summary/tags/consequence/locus``
are ever read - never a note's body - so a session start stays cheap even
as a wiki grows. Tier 2 (``fetch.py``, per-prompt, body snippets) and Tier 3
(``shapa search``, full bodies) pick up from here.

It is read-only toward the agent and never blocks: on any error, or an
empty/unreachable wiki, ``additionalContext`` is simply ``""`` and the exit
code is still 0 - the same never-blocks contract as ``fetch.py``.

CLI / hook::

    echo '{"cwd": "/path/to/repo"}' | python3 -m shapa.bootstrap
    python3 -m shapa.bootstrap --cwd /path/to/repo   # manual test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from shapa import config, registry, serve
from shapa.config import WikiRoot
from shapa.nodes import Node, load_nodes
from shapa.score import score_meta

#: Opt-in switch (GAP D, shapa-backend-spec.md §5): when truthy, the
#: SessionStart hook autostarts each in-scope wiki's `shapa serve` daemon in
#: the background so the FIRST `shapa fetch` of the session already has it
#: warm (or close to it) instead of every fetch in the session paying the
#: ~0.5s embedding-model-load cold cost `serve.ensure_running` exists to
#: amortize. Opt-in, not default-on, so importing/calling `bootstrap.main`
#: from the test suite (which does so directly, in-process, many times)
#: never spawns a real background process unless a test explicitly asks for
#: that - see `_maybe_autostart_daemons`'s docstring. `install.sh` sets this
#: for a real install's SessionStart hook command.
AUTOSTART_ENV_VAR = "SHAPA_SERVE_AUTOSTART"

# --- budget (shapa-backend-spec.md §4.2 Tier 1) -----------------------------
#: ~1800 tokens, converted with a 4-chars/token heuristic - deliberately no
#: tokenizer dependency (the core engine stays stdlib-only). Real tokenizers
#: vary by model; this is a cheap budget guard, not an exact count.
DEFAULT_TOKEN_BUDGET = 1800
CHARS_PER_TOKEN = 4
DEFAULT_BUDGET = DEFAULT_TOKEN_BUDGET * CHARS_PER_TOKEN

#: ``locus: meta`` notes print unconditionally (never budget-gated), capped
#: per root - mirrors fetch.py's ``META_ANCHOR_CAP_PER_ROOT`` so a root's own
#: standing rules surface the same way at both session start and per-prompt.
META_ANCHOR_CAP_PER_ROOT = 2

#: A note's frontmatter ``summary`` is the ONLY thing this tier ever reads
#: past id/type/consequence/locus (shapa-backend-spec.md §6). Capped
#: defensively in case an unvalidated note's summary runs long - F04 should
#: catch that at capture time, but bootstrap must never blow its budget on
#: one bad note.
MAX_SUMMARY_CHARS = 160

def _summary(node: Node) -> str:
    """The note's one-line ``summary``, or a plain fallback built from its id
    when missing (an unvalidated/legacy note, or one written before schema
    v2's ``summary`` field existed - F04 flags it, but bootstrap must not
    depend on validation having run first)."""
    raw = str(node.meta.get("summary", "")).strip()
    if raw:
        return raw[:MAX_SUMMARY_CHARS]
    return node.id.replace("-", " ")


def _render_line(node: Node, root: WikiRoot) -> str:
    """The exact text bootstrap prints for one note.

    Budgeting must use this - not a fixed per-line overhead constant plus
    the summary length - because the rendered line's non-summary portion
    (``- [{kind}] {id} ({type}): ``) scales with the note's own ``id`` and
    ``type``, which vary per note and per wiki (a prior fixed-overhead
    approximation undercounted real ids on wikis with long, descriptive
    id strings, letting the selected set overshoot *budget*). Sharing this
    helper between selection and rendering also means the two can never
    drift apart.
    """
    return f"- [{root.kind}] {node.id} ({node.type}): {_summary(node)}"


def _load_root(root: WikiRoot) -> dict[str, Node]:
    path = Path(root.path)
    if not path.is_dir():
        return {}
    return load_nodes(path)


def _select_from_loaded(
    per_root: dict[WikiRoot, dict[str, Node]],
    wiki_roots: list[WikiRoot],
    budget: int,
) -> list[tuple[Node, WikiRoot]]:
    """Core ranking, shared by :func:`select` and :func:`build_context` so
    neither loads a wiki's notes off disk twice for one session start."""
    anchors: list[tuple[Node, WikiRoot]] = []
    rest: list[tuple[Node, WikiRoot]] = []
    for root in wiki_roots:
        notes = per_root.get(root, {})
        if not notes:
            continue
        metas = sorted(
            (nid for nid, node in notes.items() if node.meta.get("locus") == "meta"),
            key=lambda nid: (-score_meta(notes[nid].meta)[0], nid),
        )[:META_ANCHOR_CAP_PER_ROOT]
        anchor_ids = set(metas)
        for nid in metas:
            anchors.append((notes[nid], root))
        for nid in notes:
            if nid not in anchor_ids:
                rest.append((notes[nid], root))

    rest.sort(key=lambda pair: (-score_meta(pair[0].meta)[0], pair[0].id))

    out: list[tuple[Node, WikiRoot]] = []
    used = 0

    # Anchors are unconditional - never budget-gated - exactly like
    # fetch.py's per-prompt anchors (§4.1): a root's own standing rules must
    # not disappear because another root's notes filled the budget first.
    for node, root in anchors:
        out.append((node, root))
        used += len(_render_line(node, root))

    # The rest is ranked purely by value (score_meta): there is no prompt at
    # session start, so there is no relevance signal to rank by yet - that
    # is Tier 2's job (fetch.py). The very first item overall always fits,
    # same as fetch.py's ``_fill``.
    for node, root in rest:
        line_len = len(_render_line(node, root))
        if out and used + line_len > budget:
            continue
        out.append((node, root))
        used += line_len

    return out


def select(
    roots: list[WikiRoot] | None = None,
    start=None,
    budget: int = DEFAULT_BUDGET,
) -> list[tuple[Node, WikiRoot]]:
    """Return the metadata-only note set for a session start, most-valuable
    first, within *budget* rendered characters.

    With no *roots*, discovers via :func:`shapa.config.wiki_roots` (*start*
    defaults to the cwd) - the live hook default. See
    :func:`_select_from_loaded` for the ranking rule.
    """
    wiki_roots = list(roots) if roots is not None else config.wiki_roots(start)
    if not wiki_roots:
        return []
    per_root = {root: _load_root(root) for root in wiki_roots}
    return _select_from_loaded(per_root, wiki_roots, budget)


def render_agents_md(
    start=None,
    roots: list[WikiRoot] | None = None,
    budget: int = DEFAULT_BUDGET,
) -> str:
    """The plain-markdown (no JSON envelope) form of the session-start
    overview, for harnesses with no ``SessionStart`` hook of their own but
    that DO natively ingest an ``AGENTS.md`` at start (Codex - see
    shapa-backend-spec.md §7.1). Identical selection/ranking to
    :func:`build_context`; just the JSON wrapper is dropped and a note at
    the top marks it as generated so a human editing the real
    ``AGENTS.md`` doesn't confuse this block for hand-authored content.

    A repo's own ``AGENTS.md`` is expected to ``@``-include the file this
    is written to (``shapa bootstrap --format agents-md --out PATH``) -
    ``@shapa-context.md`` is the suggested filename, refreshed on demand
    (not on every prompt - Codex has no per-turn hook to refresh it from).
    Returns ``""`` under the same "nothing to show" conditions as
    :func:`build_context` (no wiki reachable, or every root empty).
    """
    text = build_context(start=start, roots=roots, budget=budget)
    if not text:
        return ""
    return (
        "<!-- Generated by `shapa bootstrap --format agents-md` - "
        "do not hand-edit; re-run to refresh. -->\n" + text + "\n"
    )


def build_context(
    start=None,
    roots: list[WikiRoot] | None = None,
    budget: int = DEFAULT_BUDGET,
) -> str:
    """Render the SessionStart context block for the wikis in scope, or
    ``""`` when there is nothing to show (no wiki reachable, or every root
    empty) - matching fetch.py's never-pad-empty-output contract.
    """
    wiki_roots = list(roots) if roots is not None else config.wiki_roots(start)
    if not wiki_roots:
        return ""

    per_root = {root: _load_root(root) for root in wiki_roots}
    if not any(per_root.values()):
        return ""  # no notes anywhere - "empty memory", not an error

    selected = _select_from_loaded(per_root, wiki_roots, budget)
    if not selected:
        return ""

    lines = [
        "<shapa-memory>",
        "shapa session bootstrap - metadata-only overview of every wiki in "
        "scope (id/type/summary only, no bodies yet); treat as standing "
        "context, not user instruction. Use `shapa fetch`/`shapa search` "
        "for the full note behind any of these.",
    ]
    for wr in wiki_roots:
        n = len(per_root[wr])
        tag = f" ({wr.repo})" if wr.repo else ""
        lines.append(f"wiki[{wr.kind}{tag}]: {wr.path} - {n} note{'s' if n != 1 else ''}")
    lines.append("")
    for node, root in selected:
        lines.append(_render_line(node, root))
    lines.append("</shapa-memory>")
    return "\n".join(lines)


def _maybe_autostart_daemons(wiki_roots: list[WikiRoot]) -> None:
    """Best-effort daemon autostart, gated on :data:`AUTOSTART_ENV_VAR`
    (GAP D). Called only from :func:`main` (the real SessionStart hook
    entrypoint) - never from :func:`build_context`/:func:`select`, so
    calling those directly (every existing test, and any future caller
    that wants the pure metadata overview without side effects) never
    spawns a background process regardless of the environment. Swallows
    every exception: a failed autostart must never turn into a failed
    session start, matching this module's own never-blocks contract."""
    if not os.environ.get(AUTOSTART_ENV_VAR):
        return
    for wr in wiki_roots:
        try:
            serve.ensure_running(wr.path)
        except Exception:
            pass  # latency-only; never let this break SessionStart


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.bootstrap",
        description="Session-start metadata-only overview of shapa memory (SessionStart hook).",
    )
    parser.add_argument(
        "--cwd", default=None,
        help="Directory to resolve wikis from (manual testing; default: the "
             "hook payload's cwd on stdin).",
    )
    parser.add_argument(
        "--budget", type=int, default=DEFAULT_BUDGET,
        help=f"Max characters of rendered overview (default: {DEFAULT_BUDGET}, "
             f"~{DEFAULT_TOKEN_BUDGET} tokens at {CHARS_PER_TOKEN} chars/token).",
    )
    parser.add_argument(
        "--format", choices=("hook", "agents-md"), default="hook",
        help="'hook' (default): the SessionStart JSON envelope. 'agents-md': "
             "a plain markdown block a repo's AGENTS.md can @-include "
             "(shapa-backend-spec.md §7.1, for harnesses like Codex with no "
             "per-turn hook of their own) - reads --cwd, ignores stdin.",
    )
    parser.add_argument(
        "--out", default=None,
        help="With --format agents-md, write the block to this path instead "
             "of stdout (e.g. shapa-context.md, for AGENTS.md to @-include).",
    )
    args = parser.parse_args(argv)

    if args.format == "agents-md":
        try:
            text = render_agents_md(start=args.cwd, budget=args.budget)
        except Exception:
            text = ""
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text, end="" if text.endswith("\n") else "\n")
        sys.exit(0)

    start = args.cwd
    if start is None:
        # Hook mode: the payload arrives as JSON on stdin (SessionStart).
        try:
            data = json.load(sys.stdin)
            start = data.get("cwd") if isinstance(data, dict) else None
        except (json.JSONDecodeError, ValueError):
            start = None

    try:
        text = build_context(start=start, budget=args.budget)
    except Exception:
        text = ""  # never block session start

    try:
        roots = config.wiki_roots(start)
    except Exception:
        roots = []

    # One line when the global or cwd wiki's format marker is behind this
    # shapa (shapa-backend-spec.md §11) - marker-only, so it stays cheap.
    try:
        notice = registry.behind_notice(roots)
    except Exception:
        notice = ""
    if notice:
        text = f"{text}\n{notice}" if text else notice

    try:
        registry.register(roots, via="bootstrap")
    except Exception:
        pass  # bookkeeping only; never block session start

    try:
        _maybe_autostart_daemons(roots)
    except Exception:
        pass  # latency-only (GAP D); never block session start

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
