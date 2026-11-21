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
import re
import sys
import time
from pathlib import Path

from shapa import (codemap, config, db, embed, focus, ledger, memlog, partition,
                   reconfigure, registry, serve)
from shapa.config import WikiRoot
from shapa.nodes import Node, load_nodes
from shapa.redact import redact
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
#: Raised from 1800: the baseline alone may hold 6,000 characters (db.BASELINE_BUDGET) of one-line
#: summaries, served inside its share of this budget.
DEFAULT_TOKEN_BUDGET = 3000
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

#: Memory v3: at most this many captured memory records per root enter the
#: session-start overview (ranked by value with the notes), so a busy
#: memory log never crowds out curated notes.
MEMORY_CAP_PER_ROOT = 6

def _summary(node: Node) -> str:
    """The note's one-line ``summary``, or a plain fallback built from its id
    when missing (an unvalidated/legacy note, or one written before schema
    v2's ``summary`` field existed - F04 flags it, but bootstrap must not
    depend on validation having run first)."""
    raw = memlog.display_line(str(node.meta.get("summary", ""))).strip()
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
    return (f"- [{root.kind}] {memlog.display_line(str(node.id))} "
            f"({memlog.display_line(str(node.type))}): {_summary(node)}")


def _load_root(root: WikiRoot) -> dict[str, Node]:
    """*root*'s md notes plus its most valuable live v3 memory records
    (at most :data:`MEMORY_CAP_PER_ROOT`), ranked together by value below."""
    path = Path(root.path)
    if not path.is_dir():
        return {}
    nodes = load_nodes(path)
    nodes.update({nid: n for nid, n in _load_memories(path).items() if nid not in nodes})
    return nodes


def _load_memories(path: Path) -> dict[str, Node]:
    """The top live memory records of *path* by value (uses, recency,
    kind), as metadata-only Nodes. Never embeds (no model load at session
    start) and never raises."""
    if not memlog.has_log(path):
        return {}
    try:
        conn = memlog.open_index(path)
        if conn is None:
            return {}
        try:
            live = memlog.active_records(conn)
            counts = memlog.uses(conn)
        finally:
            conn.close()
    except Exception:
        return {}
    metas = {rid: {**memlog.value_meta(rec, counts.get(rid, (0, None))),
                   "summary": rec.summary, "_v3": True}
             for rid, rec in live.items()}
    # Rules and issues from a format-4 database are standing context, ranked
    # with the notes under the budget; only memories are capped.
    standing = [rid for rid in metas if live[rid].kind in ("rule", "issue")]
    capped = [rid for rid in metas if live[rid].kind not in ("rule", "issue")]
    top = standing + sorted(capped, key=lambda rid: (-score_meta(metas[rid])[0], rid)
                            )[:MEMORY_CAP_PER_ROOT]
    _MEMORY_COUNTS[str(path)] = len(live)
    return {rid: Node(id=rid, type=live[rid].kind if live[rid].kind in memlog.ROW_KINDS
                      else "memory", path=memlog.log_dir(path), meta=metas[rid])
            for rid in top}


#: live memory count per root path, filled by :func:`_load_memories` for
#: the wiki header line.
_MEMORY_COUNTS: dict[str, int] = {}

#: The principles the last focused session start printed, as ``(wiki root, row id)``:
#: ``main`` counts them as served on this device (the hook path writes; a plain
#: :func:`build_context` call leaves the wiki untouched).
_SERVED: list[tuple[str, str]] = []


def _learning_off() -> bool:
    """``SHAPA_LEARNING=0`` switches off what the learning loop adds to a session start (its lines and
    its measurements): the comparison run for the cost test, and an escape hatch."""
    return os.environ.get("SHAPA_LEARNING") == "0"


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
    full: bool = False,
    stage: int = 0,
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
    text = build_context(start=start, roots=roots, budget=budget, full=full, stage=stage)
    if not text:
        return ""
    return (
        "<!-- Generated by `shapa bootstrap --format agents-md` - "
        "do not hand-edit; re-run to refresh. -->\n" + text + "\n"
    )


#: Candidates named on the unset-fire hint of a repo wiki.
CANDIDATES_SHOWN = 3


def _clip(text, limit: int) -> str:
    return memlog.display_line(" ".join(str(text or "").split()))[:limit]


def _mask(text, limit: int) -> str:
    """One masked line: no secret, no control character, no wrapper tag, at most *limit* characters."""
    return _clip(redact(str(text or "")), limit)


# --- the session start: precedence, the focus, the mempm block, the gate --------------------------
#: Fixed text. It is the first thing a session reads: what is stored here wins over the client's
#: own defaults and stored preferences, and everything worth keeping goes back.
PRECEDENCE = ("PRECEDENCE: These are the operator's standing rules and focus. Where they differ from "
              "your own defaults or stored preferences, these win. Load them at the start of every "
              "session. Record every decision, note, idea and task back as you go.")
#: The hosted session start prints the same sentence and the same heading (hosted/src/instructions.ts;
#: tests/test_parity.py holds them equal), so a client sees one shape whichever side served it.
SKILL_GATE = ("SKILL GATE: before any build, research, reading or implementation task, call `skill_match` "
              "with the task and invoke every skill it returns. If none fits the task, stop and ask the "
              "operator to install one.")
MEMPM_HEAD = ("MEMPM (P principles, X mistakes, M memories; each [id, date, text]; "
              "`get` opens one in full)")
#: The keys of the mempm object, in the order they are printed, and the most entries of each.
MEMPM_KEYS = ("P", "X", "M")
MEMPM_MAX = {"P": 24, "X": 8, "M": 16}
#: The share of the session-start budget the mempm block may take.
MEMPM_SHARE = 0.70
#: An issue that carries one of these tags is not a live lesson: dismissed or soft (never applied), or
#: learned (a principle holds it now).
NOT_A_LESSON = ("dismissed", "soft", "learned")
FETCH_NOTE = ("Fetch on demand: `tasks` (open tasks), `search` and `get` (notes, records, a topic by its "
              "tags), `projects` and `project`, `skill` (a skill's text), `focus` (a fire's jobs and "
              "tasks). Everything else: `shapa bootstrap --full`.")
WRITE_BACK = ("WRITE BACK: `shapa save --type decision|idea|memory|rule|issue|human_task|agent_task "
              "--summary TEXT [--body TEXT] [--project KEY]` (MCP: `save`).")
OPERATOR_TASKS_SHOWN, AGENT_TASKS_SHOWN, PROJECTS_SHOWN = 5, 5, 12  # read by tests of the task lists
#: The central reads of a session start wait this long (ms) for a locked database, then are left out.
SESSION_BUSY_MS = 1000
#: The characters written as a unicode escape inside the block: the markup characters and the line and
#: paragraph separators.
_ESCAPED = re.compile("[<>&  ]")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _day(updated, created) -> str:
    """The day a row last changed, YYYY-MM-DD (its created day when ``updated`` is not a date), "" when neither is."""
    for raw in (updated, created):
        m = _DAY.match(str(raw or ""))
        if m:
            return m.group(0)
    return ""


def mempm_json(mem: dict) -> str:
    """The compact JSON of a mempm: keys P, X, M in that order, no spaces; ``<``, ``>``, ``&`` and the two
    line separators written as escapes, so no text can hold a tag, and no string can end the block early."""
    body = json.dumps({k: mem[k] for k in MEMPM_KEYS}, ensure_ascii=False, separators=(",", ":"))
    return _ESCAPED.sub(lambda m: "\\u%04x" % ord(m.group(0)), body)


def mempm_block(mem: dict, room: int) -> tuple[str, dict]:
    """The block as printed: heading line, then the JSON. Entries are cut from the end of the longest list
    until it fits *room*. Returns the block ("" when nothing fits) and how many entries of each key it kept."""
    lists = {k: list(mem[k]) for k in MEMPM_KEYS}
    while len(MEMPM_HEAD) + 1 + len(mempm_json(lists)) > room:
        longest = max(("M", "X", "P"), key=lambda k: len(lists[k]))
        if not lists[longest]:
            return "", {k: 0 for k in MEMPM_KEYS}
        lists[longest].pop()
    return f"{MEMPM_HEAD}\n{mempm_json(lists)}", {k: len(lists[k]) for k in MEMPM_KEYS}


def _staged_rows(conn, letters: str, stage: int) -> list[tuple]:
    """The global wiki's rows of these kinds at stage 0..*stage*, lowest stage first and then by number:
    ``(stage, id, summary, created, updated)``. By the tag index, never a scan."""
    wanted = [f"{db.STAGE_PREFIX}{n}" for n in range(0, max(0, min(db.STAGE_MAX, stage)) + 1)]
    found = conn.execute(
        f"SELECT t.tag, m.id, m.summary, m.created, m.updated FROM tags t JOIN mri m ON m.id = t.id "
        f"WHERE t.tag IN ({','.join('?' * len(wanted))}) AND m.kind IN ({','.join('?' * len(letters))})",
        [*wanted, *letters]).fetchall()
    rows = [(int(tag[-1]), int(rid[1:] or 0), rid, summary, created, updated)
            for tag, rid, summary, created, updated in found]
    return [(s, rid, summary, created, updated) for s, _, rid, summary, created, updated in sorted(rows)]


def _lesson_issues(conn, limit: int = 40) -> list[tuple]:
    """The newest issues that still carry a lesson: ``(id, summary, created, updated)``. Bounded by *limit*,
    walking the newest rows first, so a wiki of fifty thousand corrections costs forty reads."""
    return conn.execute(
        "SELECT id, summary, created, updated FROM mri WHERE kind = 'I' AND NOT EXISTS "
        f"(SELECT 1 FROM tags x WHERE x.id = mri.id AND x.tag IN ({','.join('?' * len(NOT_A_LESSON))})) "
        "ORDER BY rowid DESC LIMIT ?", [*NOT_A_LESSON, limit]).fetchall()


def build_mempm(global_conn, scoped: list[tuple], stage: int, ranked: list[dict]) -> tuple[dict, list[dict]]:
    """The principles, mistakes and memories of a session start.

    *global_conn* is the global wiki's connection (or None), *scoped* the ``(label, conn)`` of every wiki in
    scope (issues come from each), *ranked* the learned principles best first (``learn.rank_principles``
    order, then the rest by recency; each carries ``id``, ``text``, ``day``, ``root``). Returns the mempm
    (``{"P": [[id, date, text]], "X": ..., "M": ...}``) and the principle dicts it holds, in P order.

    P: stage-0 rules, then the learned principles, then the other rules up to *stage*. X: issues that still
    carry a lesson, newest first. M: the global wiki's memories up to *stage*. One masked line of text each,
    never a body."""
    mem: dict = {k: [] for k in MEMPM_KEYS}
    taken: list[tuple[int, dict]] = []  # (position in P, the learned principle there)
    seen: set = set()

    def put(key: str, ident, rid: str, day: str, text: str) -> bool:
        if len(mem[key]) >= MEMPM_MAX[key] or (key, ident) in seen or not text:
            return False
        seen.add((key, ident))
        mem[key].append([rid, day, text])
        return True

    rules = _staged_rows(global_conn, "R", stage) if global_conn is not None else []
    for s, rid, summary, created, updated in rules:
        if s == 0:
            put("P", ("global", rid), rid, _day(updated, created), _mask(summary, db.SUMMARY_MAX))
    for p in ranked:
        from shapa import learn

        if put("P", (p["wiki"], p["id"]), p["id"], p["day"], learn.flatten(p["text"], db.SUMMARY_MAX)):
            taken.append((len(mem["P"]) - 1, p))
    for s, rid, summary, created, updated in rules:
        if s > 0:
            put("P", ("global", rid), rid, _day(updated, created), _mask(summary, db.SUMMARY_MAX))

    issues = []
    for n, (label, conn) in enumerate(scoped):
        for rid, summary, created, updated in _lesson_issues(conn):
            issues.append((_day(updated, created), -n, rid, label, summary))
    for day, _, rid, label, summary in sorted(issues, key=lambda x: x[:2], reverse=True):
        put("X", (label, rid), rid, day, _mask(summary, db.SUMMARY_MAX))

    if global_conn is not None:
        for s, rid, summary, created, updated in _staged_rows(global_conn, "M", stage):
            put("M", ("global", rid), rid, _day(updated, created), _mask(summary, db.SUMMARY_MAX))
    return mem, taken


#: The levels of the optional lines, best first: a fire's partition, then the unset-fire hints.
LEVEL_NAMES, LEVEL_POINTER = 2, 3


def build_focused(wiki_roots: list[WikiRoot], budget: int = DEFAULT_BUDGET, stage: int = 0) -> str:
    """The default session start: the precedence sentence, each wiki scope's northstar, then its (up to
    three) fires, one line each, then the mempm block (principles, mistakes and memories as compact JSON:
    id, date and one masked line, never a body), the line that says which tool fetches the rest, what the
    learning loop has to say, the SKILL GATE and the write-back pointer. Tasks, notes, projects, topics and
    skills are not in it; ``--full`` brings back the old overview.

    *budget* is in characters. The directive, the precedence, the northstars, the fires, the gate and the
    write-back line are never cut. The mempm block takes at most its share of what is left, shortest list
    last first, and the optional lines (a fire's partition, the unset-fire hints) take the rest."""
    _SERVED.clear()
    try:
        directive = reconfigure.directive_lines([wr.path for wr in wiki_roots if wr.kind != "external"])
    except Exception:
        directive = []  # never block session start
    norths: list[str] = []
    groups: list[tuple[str, list[dict]]] = []  # (wiki label, fires)
    optional: list[tuple[int, int, int, str]] = []  # (level, fire index, order, line)
    fire_no = 0
    hints: list[str] = []
    pool: list[dict] = []         # learned principles of every wiki in scope
    fire_text: list[str] = []
    awaiting = 0
    activation = 0
    drift_lines: list[str] = []
    churn_ids: list[str] = []
    fire_feats: list[tuple] = []
    scoped: list[tuple[str, object]] = []  # (label, open connection): issues come from each
    global_conn = None
    L = None                      # shapa.learn, imported only when a wiki has something for it
    ordered = sorted((wr for wr in wiki_roots if wr.kind in ("global", "repo")),
                     key=lambda wr: wr.kind != "global")  # the global northstar leads
    try:
        for wr in ordered:
            label = _clip(wr.repo or wr.kind, 60)
            try:
                conn = db.connect(wr.path, busy_ms=SESSION_BUSY_MS)
            except Exception:
                conn = None
            if conn is None:
                continue
            graph = None
            keep = False
            try:
                f = focus.load(conn)
                try:  # learning: pending corrections, principles, drift (each its own line; never blocks)
                    # a wiki with no correction, principle or fire has nothing for the learning loop to say:
                    # nothing is imported or asked
                    if not _learning_off() and (any(f.fires) or conn.execute(
                            "SELECT 1 FROM tags WHERE tag IN ('correction','principle','churn') LIMIT 1").fetchone()):
                        if L is None:
                            from shapa import learn as L
                        awaiting += L.awaiting_count(conn)
                        for c in L.principle_candidates(conn, wr.kind, wr.path):
                            row = conn.execute("SELECT created, updated FROM mri WHERE id = ?", (c["id"],)).fetchone()
                            c["day"] = _day(row[1], row[0]) if row else ""
                            pool.append(c)
                        activation += len(L.awaiting_activation(conn=conn))
                        churn_ids += [f"{wr.kind}/{r[0]}" for r in conn.execute("SELECT id FROM tags WHERE tag = 'churn'")]
                        if wr.kind == "repo":
                            drift_lines += L.drift(conn, wr.path)
                except Exception:
                    pass
                if wr.kind == "repo" and any(f.fires):
                    try:
                        graph = codemap.open_graph(wr.path)  # read-only; absent until `shapa map`
                    except Exception:
                        graph = None
                if f.northstar:
                    norths.append(f"northstar [{label}]: {_clip(f.northstar, focus.NORTHSTAR_MAX)}")
                shown: list[dict] = []
                for i, fire in enumerate(f.fires, 1):
                    if fire is None:
                        continue
                    t = focus.tree(conn, fire, i)
                    if fire.feature and t.status in ("closed", "missing"):
                        continue  # closed behind our back: `shapa focus` heals it
                    fire_text.append(" ".join([t.title, *(j.title for j in t.jobs)]))
                    if t.feature:
                        fire_feats.append((wr.path, t.feature))
                    head = f"{i}. " + (f"{t.feature} " if t.feature else "") + _clip(t.title, 160)
                    if t.status == "claimed":
                        head += " (claimed)"
                    entry = {"head": head, "order": [], "no": fire_no}
                    if graph is not None:
                        try:
                            line = partition.session_line(partition.partition_fire(conn, graph, fire, i))
                        except Exception:
                            line = ""  # a broken map never blocks session start
                        if line:
                            entry["order"].append((LEVEL_NAMES, 0, line))
                    shown.append(entry)
                    fire_no += 1
                if shown:
                    groups.append((label, shown))
                elif wr.kind == "repo":
                    cands = focus.candidates(conn)[:CANDIDATES_SHOWN]
                    if cands:
                        hints.append("no fires set here: `shapa focus fire 1 F<n> --scope repo`; "
                                     "candidates: " + "; ".join(f"{c.id} {_clip(c.title, 70)}" for c in cands))
                scoped.append((label, conn))
                if wr.kind == "global":
                    global_conn = conn
                keep = True  # the connection stays open for the mempm; closed below
            except Exception:
                continue  # a broken database never blocks session start
            finally:
                if graph is not None:
                    graph.close()
                if not keep:
                    conn.close()
        ranked: list[dict] = []
        if pool and L is not None:
            from shapa import metrics

            fire_ids = {f"{metrics.repo_key(path)}/{feat}" for path, feat in fire_feats}
            ranked = L.rank_principles(pool, " ".join(fire_text), fire_ids, k=len(pool))
            rest = sorted((c for c in pool if c not in ranked), key=lambda c: (c["day"], c["id"]), reverse=True)
            ranked += rest
        try:
            mem, served = build_mempm(global_conn, scoped, max(0, min(db.STAGE_MAX, int(stage))), ranked)
        except Exception:
            mem, served = {k: [] for k in MEMPM_KEYS}, []  # never block session start
    finally:
        for _, conn in scoped:
            try:
                conn.close()
            except Exception:
                pass
    learn_lines: list[str] = []
    if awaiting or activation:
        parts = []
        if awaiting:
            parts.append(f"{awaiting} correction{' awaits' if awaiting == 1 else 's await'} learning")
        if activation:
            parts.append(f"{activation} skill version{' awaits' if activation == 1 else 's await'} activation")
        learn_lines.append(", ".join(parts) + ": shapa learn pending")
    if churn_ids:
        drift_lines.append(f"drift: principle {churn_ids[0]} needs enforcement, not rewording "
                           "(shapa learn pending)")
    if drift_lines:  # at most one: the first, and a count of the rest
        learn_lines.append(drift_lines[0] + (f" (+{len(drift_lines) - 1} more: shapa metrics)"
                                             if len(drift_lines) > 1 else ""))
    has_mem = any(mem[k] for k in MEMPM_KEYS)
    if not (norths or groups or hints or directive or has_mem or learn_lines):
        return ""
    head = (["<shapa-action>", *directive, "</shapa-action>"] if directive else [])
    top = [*head, "<shapa-memory>", PRECEDENCE]
    if not norths and not groups:
        top.append("no northstar or fires set: `shapa focus northstar TEXT --scope global`")
    top += norths
    learned = [_clip(x, 220) for x in learn_lines]
    fire_lines: list[str] = []
    for label, fires in groups:
        fire_lines.append(f"fires [{label}]:")
        fire_lines += [e["head"] for e in fires]
    tail = [FETCH_NOTE, *learned, SKILL_GATE, WRITE_BACK, "</shapa-memory>"]
    fixed = len("\n".join([*top, *fire_lines, *tail])) + 2
    room = max(0, budget - fixed)
    block, kept = mempm_block(mem, min(room, int(budget * MEMPM_SHARE))) if has_mem else ("", {})
    if block:
        room -= len(block) + 1
        for at, p in served:
            if at < kept["P"]:
                _SERVED.append((p["root"], p["id"]))
    # Optional lines, best first: a fire's partition, then the unset-fire hints; the first fire's before the second's.
    for _, fires in groups:
        for e in fires:
            for level, n, line in e["order"]:
                optional.append((level, e["no"], n, line))
    for k, line in enumerate(hints):
        optional.append((LEVEL_POINTER, 0, k, line))
    keep_lines: set[tuple[int, int, int]] = set()
    for level, no, n, line in sorted(optional, key=lambda x: x[:3]):
        if len(line) + 1 <= room:
            keep_lines.add((level, no, n))
            room -= len(line) + 1
    lines = [*top]
    for label, fires in groups:
        lines.append(f"fires [{label}]:")
        for e in fires:
            lines.append(e["head"])
            for level, n, line in e["order"]:
                if (level, e["no"], n) in keep_lines:
                    lines.append(line)
    for k, line in enumerate(hints):
        if (LEVEL_POINTER, 0, k) in keep_lines:
            lines.append(line)
    if block:
        lines.append(block)
    lines += tail
    return "\n".join(lines)


def build_context(
    start=None,
    roots: list[WikiRoot] | None = None,
    budget: int = DEFAULT_BUDGET,
    full: bool = False,
    stage: int = 0,
) -> str:
    """Render the SessionStart context block for the wikis in scope, or
    ``""`` when there is nothing to show (no wiki reachable, or every root
    empty) - matching fetch.py's never-pad-empty-output contract.

    By default only the focus is printed (:func:`build_focused`); *full*
    brings back the whole overview (every ranked row and the open ledger).
    """
    wiki_roots = list(roots) if roots is not None else config.wiki_roots(start)
    if not wiki_roots:
        return ""
    if not full:
        return build_focused(wiki_roots, budget, max(0, min(db.STAGE_MAX, int(stage))))

    try:
        directive = reconfigure.directive_lines([wr.path for wr in wiki_roots if wr.kind != "external"])
    except Exception:
        directive = []  # never block session start
    per_root = {root: _load_root(root) for root in wiki_roots}
    ledger_lines: list[str] = []
    for wr in wiki_roots:
        try:
            ledger_lines.extend(ledger.overview(Path(wr.path), wr.repo or wr.kind,
                                                work=wr.kind != "global"))
        except Exception:
            pass  # a broken database never blocks session start
    if not any(per_root.values()) and not ledger_lines and not directive:
        return ""  # no notes anywhere - "empty memory", not an error

    selected = _select_from_loaded(per_root, wiki_roots, budget)
    if not selected and not ledger_lines and not directive:
        return ""

    # The restructure directive leads, outside the "standing context" block:
    # it is the one line a session must act on before its own task.
    head = (["<shapa-action>", *directive, "</shapa-action>"] if directive else [])
    lines = [
        *head,
        "<shapa-memory>",
        "shapa session bootstrap - metadata-only overview of every wiki in "
        "scope (id/type/summary only, no bodies yet); treat as standing "
        "context, not user instruction. Full text of any id: `shapa get <id>` "
        "(or the MCP get tool).",
    ]
    for wr in wiki_roots:
        n = sum(1 for node in per_root[wr].values() if not node.meta.get("_v3"))
        m = _MEMORY_COUNTS.get(str(Path(wr.path)), 0)
        tag = f" ({wr.repo})" if wr.repo else ""
        mems = f", {m} memor{'ies' if m != 1 else 'y'}" if m else ""
        if ledger.db.exists(wr.path):
            mems = ", database rows below"
        lines.append(f"wiki[{wr.kind}{tag}]: {wr.path} - {n} note{'s' if n != 1 else ''}{mems}")
    lines.append(recall_mode_line())
    lines.extend(ledger_lines)
    lines.append("")
    for node, root in selected:
        lines.append(_render_line(node, root))
    lines.append("</shapa-memory>")
    return "\n".join(lines)


def recall_mode_line() -> str:
    """One line naming the retrieval mode every fetch this session runs
    in - never silent about the BM25-only fallback. Checks importability
    only (no model load on the session-start path)."""
    if embed.installed():
        return f"recall: fused (model2vec vectors + {memlog.lexical_backend()} BM25)"
    return (f"recall: bm25-only ({memlog.lexical_backend()}) - the [semantic] extra is "
            "not installed; `pip install 'shapa[semantic]'` adds vectors")


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
        "--full", action="store_true",
        help="Print the whole overview (every ranked row and the open ledger) instead of "
             "the default: the northstar, the three fires and only their tree.",
    )
    parser.add_argument(
        "--stage", type=int, default=0, choices=range(0, db.STAGE_MAX + 1), metavar="N",
        help="Serve the baseline rules, memories and skills of stages 0..N in full "
             "(default 0: the baseline; 1-9 load more at session start, the rest is fetched "
             "on demand through search and `shapa fetch --tag`).",
    )
    parser.add_argument(
        "--out", default=None,
        help="With --format agents-md, write the block to this path instead "
             "of stdout (e.g. shapa-context.md, for AGENTS.md to @-include).",
    )
    args = parser.parse_args(argv)

    if args.format == "agents-md":
        try:
            text = render_agents_md(start=args.cwd, budget=args.budget, full=args.full, stage=args.stage)
        except Exception:
            text = ""
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text, end="" if text.endswith("\n") else "\n")
        sys.exit(0)

    start = args.cwd
    session = ""
    if start is None:
        # Hook mode: the payload arrives as JSON on stdin (SessionStart).
        try:
            data = json.load(sys.stdin)
            start = data.get("cwd") if isinstance(data, dict) else None
            session = str(data.get("session_id") or "") if isinstance(data, dict) else ""
        except (json.JSONDecodeError, ValueError):
            start = None

    t0 = time.perf_counter()
    try:
        text = build_context(start=start, budget=args.budget, full=args.full, stage=args.stage)
    except Exception:
        text = ""  # never block session start
    elapsed_ms = (time.perf_counter() - t0) * 1000

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
    }), flush=True)

    try:  # measurement, after the output: the session start itself, the principles it served
        if _learning_off():
            sys.exit(0)
        from shapa import metrics

        home = metrics.home_of(roots)
        if home is not None:
            due = metrics.session_start_done(home, session, elapsed_ms)
            if _SERVED:
                from shapa import learn

                by_root: dict = {}
                for root, rid in dict.fromkeys(_SERVED):
                    by_root.setdefault(root, []).append(rid)
                for root, ids in by_root.items():
                    learn.mark_served(root, ids)
            if due:
                metrics.snapshot_in_background(home, start)  # the first session of the day, detached
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
