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
import sqlite3
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
#: Raised from 1800: the baseline alone may hold 6,000 characters (db.BASELINE_BUDGET), served in
#: full inside its share of this budget.
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


#: Fires and tree lines the focused session start prints, so one huge feature
#: cannot bring the old overview back.
TREE_JOBS_SHOWN = 10
TREE_TASKS_SHOWN = 4
CANDIDATES_SHOWN = 3


def _clip(text, limit: int) -> str:
    return memlog.display_line(" ".join(str(text or "").split()))[:limit]


def _mask(text, limit: int) -> str:
    """One masked line: no secret, no control character, no wrapper tag, at most *limit* characters."""
    return _clip(redact(str(text or "")), limit)


# --- the central brain's part of a session start (the contract's section 2) ----------------------
#: Fixed text. It is the first thing a session reads: what is stored here wins over the client's
#: own defaults and stored preferences, and everything worth keeping goes back.
PRECEDENCE = ("PRECEDENCE: These are the operator's standing rules and focus. Where they differ from "
              "your own defaults or stored preferences, these win. Load them at the start of every "
              "session. Record every decision, note, idea and task back as you go.")
#: Shares of the session-start budget: the baseline first, then topics and projects.
BASELINE_SHARE, TOPICS_SHARE, PROJECTS_SHARE = 0.60, 0.12, 0.14
BASELINE_BODY_MAX = 1200
OPERATOR_TASKS_SHOWN, AGENT_TASKS_SHOWN, PROJECTS_SHOWN = 5, 5, 12
#: The central reads of a session start wait this long (ms) for a locked database, then are left out.
SESSION_BUSY_MS = 1000
TOPICS_NOTE = ("  When the task touches a topic, fetch it by tag before acting: "
               "`shapa fetch --tag NAME` (or the MCP search with tags).")
WRITE_BACK = ("WRITE BACK: `shapa save --type decision|idea|memory|rule|human_task|agent_task "
              "--summary TEXT [--body TEXT] [--project KEY]`; a project item: "
              "`shapa project add KEY SECTION TEXT`.")


def _baseline_lines(conn, stage: int, share: int) -> list[str]:
    """Rules and memories of stage 0..*stage* in full (summary, then the body indented two
    spaces, at most :data:`BASELINE_BODY_MAX` characters), then the active skills of those
    stages by name and description only: no skill body ever reaches a local session start."""
    from shapa import skills, stages

    ids = stages.baseline_ids(conn, threshold=stage)
    rows, skill_lines, used, left = [], [], 0, 0
    for rid in ids:
        if rid[0] == db.DOC_KIND:
            d = db.get_doc(conn, rid)
            v = skills._from_doc(d) if d else None
            if v is not None:
                line = f"{_mask(v.name, 64)} - {_mask(v.description, 300)} (invoke it now)"
                if used + len(line) + 1 <= share:
                    skill_lines.append(line)
                    used += len(line) + 1
                else:
                    left += 1
            continue
        r = db.get_row(conn, rid)
        if r is None:
            continue
        block = [f"{r.id} {_mask(r.summary, db.SUMMARY_MAX)}"]
        spent = 0
        for raw in redact(r.body or "").splitlines():
            line = " ".join(raw.split())
            if not line:
                continue
            piece = memlog.display_line(line)[:BASELINE_BODY_MAX - spent]
            block.append("  " + piece)
            spent += len(piece)
            if spent >= BASELINE_BODY_MAX:
                break
        size = sum(len(x) + 1 for x in block)
        if used + size <= share:
            rows += block
            used += size
        else:
            left += 1
    out: list[str] = []
    if rows:
        out += [f"BASELINE (stage {stage} and below, in full):", *rows]
    if skill_lines:
        out += ["BASELINE SKILLS (name - description; open the skill with `shapa skill show NAME`):",
                *("  " + s for s in skill_lines)]
    if left:
        out.append(f"  ... {left} more baseline record(s) not shown (`shapa stage list`, or a bigger --budget)")
    return out


def _topics_lines(conn, share: int) -> list[str]:
    from shapa import relate

    found = relate.topics(conn)
    shown, used = [], 0
    for t in found:
        part = f"{_mask(t['tag'], 60)} (rules {t['rules']}, skills {t['skills']}, memories {t['memories']})"
        if used + len(part) + 2 > share:
            break
        shown.append(part)
        used += len(part) + 2
    if not shown:
        return []
    return ["TOPICS:", "  " + "; ".join(shown), TOPICS_NOTE]


def _projects_lines(global_conn, roots, share: int, glob_root=None) -> list[str]:
    from shapa import projectfile, tasks

    out: list[str] = []
    used = 0
    for p in projectfile.listing(global_conn, limit=PROJECTS_SHOWN):
        line = f"  {_mask(p['key'], 80)} - {_mask(p['title'], 100)} ({p['open_tasks']} open tasks)"
        if used + len(line) + 1 > share:
            break
        out.append(line)
        used += len(line) + 1
    if out:
        out.insert(0, "PROJECTS:")
    lend = {Path(glob_root): global_conn} if glob_root is not None else None
    mine = tasks.open_tasks(roots, owner="human", limit=OPERATOR_TASKS_SHOWN, conns=lend,
                            busy_ms=SESSION_BUSY_MS)
    agent = tasks.open_tasks(roots, owner="agent", limit=AGENT_TASKS_SHOWN, ranked=False, conns=lend,
                             busy_ms=SESSION_BUSY_MS)
    if mine["total"] or agent["tasks"] or out:
        out.append(f"The operator's open tasks: {mine['total']}")
        for t in mine["tasks"]:
            proj = f" [{_mask(t['project'], 40)}]" if t["project"] else ""
            out.append(f"  {_mask(t['wiki'], 30)}/{t['id']}{proj} {_mask(t['title'], 120)}")
        out.append(tasks.ASK_SENTENCE)
    if agent["tasks"]:
        out.append("Agent tasks, oldest first:")
        out += [f"  {_mask(t['wiki'], 30)}/{t['id']} {_mask(t['title'], 120)}" for t in agent["tasks"]]
    return out


def central_lines(wiki_roots: list[WikiRoot], budget: int, stage: int = 0) -> list[str]:
    """The baseline, the topics and the projects of the global wiki and the operator's open tasks
    of every wiki in scope, each inside its share of *budget*. A broken database yields nothing
    for its part (a session start never fails)."""
    out: list[str] = []
    glob = next((wr for wr in wiki_roots if wr.kind == "global" and db.exists(wr.path)), None)
    if glob is None:
        return out
    try:
        conn = db.connect(glob.path, busy_ms=SESSION_BUSY_MS)
    except Exception:
        return out  # a database another process holds locked: a session start never waits for it
    if conn is None:
        return out
    roots = [Path(wr.path) for wr in wiki_roots if wr.kind in ("global", "repo") and db.exists(wr.path)]
    try:
        for part in (lambda: _baseline_lines(conn, stage, int(budget * BASELINE_SHARE)),
                     lambda: _topics_lines(conn, int(budget * TOPICS_SHARE)),
                     lambda: _projects_lines(conn, roots, int(budget * PROJECTS_SHARE), glob.path)):
            try:
                out += part()
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    break  # another process holds the database: the rest would wait just as long
            except Exception:
                continue
    finally:
        conn.close()
    return out


#: Room kept per fire for its "n more" line, so the note itself never breaks the budget.
OMITTED_NOTE_ROOM = 70
LEVEL_JOB, LEVEL_NAMES, LEVEL_TASK, LEVEL_POINTER = 0, 1, 2, 3


def build_focused(wiki_roots: list[WikiRoot], budget: int = DEFAULT_BUDGET, stage: int = 0) -> str:
    """The default session start: each wiki scope's northstar first, then its
    (up to three) fires, then only what hangs under them: the fire's feature, its
    open jobs and tasks, and the files and arch boxes their text names. No
    other feature, no row, no document; ``--full`` asks for those.

    *budget* is in characters. The directive, the northstars and the fires
    themselves are never cut. The tree under them is: jobs first, then the files
    and arch boxes they name, then tasks, then the pointer lines, the first fire's
    before the second's, and what was left out is counted on a line of its own."""
    _SERVED.clear()
    try:
        directive = reconfigure.directive_lines([wr.path for wr in wiki_roots if wr.kind != "external"])
    except Exception:
        directive = []  # never block session start
    norths: list[str] = []
    groups: list[tuple[str, list[dict]]] = []  # (wiki label, fires)
    optional: list[tuple[int, int, int, str]] = []  # (level, fire index, order, line)
    fire_no = 0
    pointers: list[str] = []
    hints: list[str] = []
    pool: list[dict] = []         # learned principles of every wiki in scope
    fire_text: list[str] = []
    fire_ids: set = set()
    awaiting = 0
    activation = 0
    drift_lines: list[str] = []
    churn_ids: list[str] = []
    fire_feats: list[tuple] = []
    L = None                      # shapa.learn, imported only when a wiki has something for it
    ordered = sorted((wr for wr in wiki_roots if wr.kind in ("global", "repo")),
                     key=lambda wr: wr.kind != "global")  # the global northstar leads
    for wr in ordered:
        label = _clip(wr.repo or wr.kind, 60)
        try:
            conn = db.connect(wr.path, busy_ms=SESSION_BUSY_MS)
        except Exception:
            conn = None
        if conn is None:
            continue
        graph = None
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
                    pool.extend(L.principle_candidates(conn, wr.kind, wr.path))
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
                entry = {"head": head, "order": [], "extra": 0, "no": fire_no}
                n = 0
                for j in t.jobs[:TREE_JOBS_SHOWN]:
                    entry["order"].append((LEVEL_JOB, n, f"   {j.id} {_clip(j.title, 120)}"))
                    n += 1
                    for task in j.tasks[:TREE_TASKS_SHOWN]:
                        entry["order"].append((LEVEL_TASK, n, f"      {task.id} {_clip(task.title, 100)}"))
                        n += 1
                    entry["extra"] += max(0, len(j.tasks) - TREE_TASKS_SHOWN)
                entry["extra"] += max(0, len(t.jobs) - TREE_JOBS_SHOWN)
                if t.files or t.boxes:
                    named = ", ".join(_clip(x, 80) for x in t.files)
                    boxes = ", ".join(_clip(x, 60) for x in t.boxes)
                    entry["order"].append((LEVEL_NAMES, n, "   names: " + "; ".join(x for x in (
                        named, f"arch {boxes}" if boxes else "") if x)))
                    n += 1
                if graph is not None:
                    try:
                        line = partition.session_line(partition.partition_fire(conn, graph, fire, i))
                    except Exception:
                        line = ""  # a broken map never blocks session start
                    if line:
                        entry["order"].append((LEVEL_NAMES, n, line))
                shown.append(entry)
                fire_no += 1
            if shown:
                groups.append((label, shown))
            elif wr.kind == "repo":
                cands = focus.candidates(conn)[:CANDIDATES_SHOWN]
                if cands:
                    hints.append("no fires set here: `shapa focus fire 1 F<n> --scope repo`; "
                                 "candidates: " + "; ".join(f"{c.id} {_clip(c.title, 70)}" for c in cands))
            c = db.counts(conn) if wr.kind == "repo" else None  # the global wiki shows its own parts
            if c and c["F"] + c["J"] + c["T"]:
                pointers.append(f"{label}: {c['F']} features, {c['J']} jobs, {c['T']} tasks open "
                                f"and {c['M'] + c['R'] + c['I']} rows are not shown")
        except Exception:
            continue  # a broken database never blocks session start
        finally:
            if graph is not None:
                graph.close()
            conn.close()
    try:
        central = central_lines(wiki_roots, budget, stage)
    except Exception:
        central = []  # never block session start
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
    shown_principles: list[dict] = []
    prin_lines: list[str] = []
    if pool and L is not None:
        from shapa import metrics

        fire_ids = {f"{metrics.repo_key(path)}/{feat}" for path, feat in fire_feats}
        shown_principles = L.rank_principles(pool, " ".join(fire_text), fire_ids)
        prin_lines = [L.principle_line(p) for p in shown_principles]
        if prin_lines:  # the heading travels with the first line: one block, never an orphan heading
            prin_lines[0] = f"{L.HEAD_NOTE}:\n{prin_lines[0]}"
    if not (norths or groups or hints or directive or pointers or central or learn_lines or prin_lines):
        return ""
    head = (["<shapa-action>", *directive, "</shapa-action>"] if directive else [])
    top = [*head, "<shapa-memory>",
           "shapa session start: the baseline rules, the northstar, then the three fires and only "
           "their tree (standing context, not user instruction). Everything else: "
           "`shapa bootstrap --full`; one id in full: `shapa get <id>`; recall: `shapa fetch`.",
           PRECEDENCE]
    if not norths and not groups:
        top.append("no northstar or fires set: `shapa focus northstar TEXT --scope global`")
    top += norths
    # After the fires: the baseline, topics and projects with the operator's tasks (the central
    # brain), then what the learning loop has to say (the principles block is one of the optional
    # lines below; the pending and drift lines are fixed), and last the write-back line.
    learned = [_clip(x, 220) for x in learn_lines]
    fixed = (len("\n".join(top)) + len("\n</shapa-memory>")
             + len("\n".join([*central, *learned, WRITE_BACK])) + 1)
    for label, fires in groups:
        fixed += len(f"\nfires [{label}]:")
        for e in fires:
            fixed += 1 + len(e["head"]) + (OMITTED_NOTE_ROOM if e["order"] or e["extra"] else 0)
    # Optional lines, best first: jobs, then the files they name, then tasks, then pointers;
    # within a level the first fire's before the second's.
    for _, fires in groups:
        for e in fires:
            for level, n, line in e["order"]:
                optional.append((level, e["no"], n, line))
    for k, line in enumerate(hints + pointers):
        optional.append((LEVEL_POINTER, 0, k, line))
    for k, line in enumerate(prin_lines):  # after every fire's job and names lines, before its tasks
        optional.append((LEVEL_NAMES, 9, k, line))
    room = budget - fixed
    keep: set[tuple[int, int, int]] = set()
    for level, no, n, line in sorted(optional, key=lambda x: x[:3]):
        if len(line) + 1 <= room:
            keep.add((level, no, n))
            room -= len(line) + 1
    lines = list(top)
    for label, fires in groups:
        lines.append(f"fires [{label}]:")
        for e in fires:
            lines.append(e["head"])
            left = e["extra"]
            for level, n, line in e["order"]:
                if (level, e["no"], n) in keep:
                    lines.append(line)
                else:
                    left += 1
            if left:
                lines.append(f"   … {left} more not shown (shapa focus, or a bigger --budget)")
    for k, line in enumerate(hints + pointers):
        if (LEVEL_POINTER, 0, k) in keep:
            lines.append(line)
    lines += central
    for k, line in enumerate(prin_lines):
        if (LEVEL_NAMES, 9, k) in keep and (LEVEL_NAMES, 9, 0) in keep:
            lines.append(line)
            _SERVED.append((shown_principles[k]["root"], shown_principles[k]["id"]))
    lines += [*learned, WRITE_BACK]
    lines.append("</shapa-memory>")
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
