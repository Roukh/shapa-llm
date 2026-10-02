"""Maintenance - the self-healing pass that keeps the network itself.

It does not report for a human to act on; it maintains the network:

  - PRUNE orphans (no link in or out) and STALE notes (age > t AND uses < 1).
  - AUTO-MERGE near-duplicates: when two notes are ~the same, keep the
    higher-scored one, repoint inbound [[links]] to it, and delete the other.
  - RESOLVE contradictions (--resolve): for similar rule-vs-rule pairs, ask the
    local ``claude`` CLI to reconcile them into one note (or confirm DISTINCT).
  - LEAN (--lean): report lean-wiki-shape violations (F10/F11, see
    shapa.validate) and which live notes are ``status: superseded`` -
    ready to move into ``archive/`` (--apply; never deletes).
  - BACKFILL (--backfill): strip legacy ``uses``/``last_used`` frontmatter
    lines left over from before those counters moved into the index store
    (shapa-backend-spec.md §10 decision 7 - see ``shapa.store``/``shapa.score``).

Similarity uses local embeddings when available (see shapa.embed), else token
Jaccard. Auto-merge is mechanical and safe; contradiction resolution is an LLM
step, so it is on-demand (--resolve), not part of the per-turn hook.

CLI::
    shapa maintain --prune            # prune + auto-merge dupes (connected wiki)
    shapa maintain --prune --resolve  # also LLM-reconcile contradictions
    shapa maintain --lean             # report lean-shape violations (F10/F11)
    shapa maintain --lean --apply     # + archive status:superseded notes
    shapa maintain --backfill         # strip legacy uses/last_used lines
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from shapa import config, embed, frontmatter, memlog, save, validate
from shapa.heartbeat import find_orphans
from shapa.nodes import build_graph, is_protected, load_nodes
from shapa.score import _parse_ts, score_meta

#: Default minimum use count for ``--memories``/promotion candidates.
DEFAULT_MIN_MEMORY_USES = 5

MERGE_THRESHOLD_JACCARD = 0.9   # >= this token-Jaccard similarity -> auto-merge
# Embedding cosine similarity runs "hotter" than shingle-Jaccard on short notes
# that merely share topic/wording (e.g. two "old" notes about the same linked
# note scored ~0.92 cosine but ~0.3 Jaccard) - a single shared threshold picked
# for one backend false-merges under the other. Calibrated with headroom above
# that kind of same-topic-different-content pair and below true near-duplicates
# (verbatim-identical bodies score 1.0).
MERGE_THRESHOLD_EMBED = 0.95    # >= this cosine similarity -> auto-merge
# Back-compat alias: existing callers importing MERGE_THRESHOLD keep working,
# pointed at the lexical (always-available, dependency-free) default.
MERGE_THRESHOLD = MERGE_THRESHOLD_JACCARD
CONTRA_LOW = 0.6        # [LOW, MERGE) rule-vs-rule -> contradiction candidate
_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = {"the", "and", "for", "with", "that", "this", "are", "but", "not",
         "you", "its", "from", "into", "then", "they", "have", "has", "was",
         "will", "can", "use", "uses", "used", "when", "where", "which", "any"}


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP]


def _shingles(tokens: list[str], k: int = 3) -> set:
    if len(tokens) < k:
        return set(tokens)
    return {tuple(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def default_merge_threshold() -> float:
    """The backend-appropriate auto-merge threshold for the *current* similarity
    source (embeddings when available, else token-Jaccard) - see the two
    MERGE_THRESHOLD_* constants for why they differ."""
    return MERGE_THRESHOLD_EMBED if embed.available() else MERGE_THRESHOLD_JACCARD


def find_stale(nodes: dict, now: datetime, max_age_days: int) -> list[str]:
    """Notes older than max_age_days (by last_used or created) with uses < 1.

    Curated design docs (``type: reference``) are never stale - they are
    authored material, not ephemeral memory, so they are skipped.
    """
    out = []
    for nid, node in nodes.items():
        if is_protected(node):
            continue
        try:
            uses = int(str(node.meta.get("uses", 0)).strip())
        except ValueError:
            uses = 0
        ts = _parse_ts(node.meta.get("last_used")) or _parse_ts(node.meta.get("created"))
        if ts is None:
            continue
        if (now - ts).total_seconds() / 86400.0 > max_age_days and uses < 1:
            out.append(nid)
    return sorted(out)


def _similarity_pairs(nodes: dict, directory, min_sim: float) -> list[tuple]:
    """Return (a, b, sim, both_rule) pairs with similarity >= min_sim, desc.

    Uses local embeddings when available, else token-Jaccard.
    """
    bodies = {nid: frontmatter.parse(n.path).body for nid, n in nodes.items()}
    ids = sorted(bodies)

    if embed.available():
        vecs = embed.note_vectors(directory, bodies)
        sim = lambda a, b: embed.cosine(vecs[a], vecs[b])
    else:
        sh = {nid: _shingles(_tokens(b)) for nid, b in bodies.items()}
        sim = lambda a, b: _jaccard(sh[a], sh[b])

    pairs = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            s = sim(ids[i], ids[j])
            if s >= min_sim:
                both_rule = (nodes[ids[i]].meta.get("type") == "rule"
                             and nodes[ids[j]].meta.get("type") == "rule")
                pairs.append((ids[i], ids[j], round(float(s), 3), both_rule))
    pairs.sort(key=lambda x: -x[2])
    return pairs


def _repoint(directory, old_id: str, new_id: str) -> None:
    """Rewrite every [[old_id]] reference to [[new_id]] across the directory."""
    pat = re.compile(r"\[\[\s*" + re.escape(old_id) + r"(?=[\]|#])")
    for p in directory.rglob("*.md"):  # include the arch/ subdir when repointing links
        t = p.read_text(encoding="utf-8")
        if old_id in t:
            p.write_text(pat.sub(f"[[{new_id}", t), encoding="utf-8")


def merge_duplicates(directory, threshold: float | None = None,
                     dry_run: bool = False) -> list[tuple]:
    """Auto-merge near-duplicate pairs. Returns [(dropped, kept, sim)].

    With dry_run=True, computes what would merge but changes nothing.
    threshold defaults to the backend-appropriate value (see
    default_merge_threshold) - callers that pin a value opt out of that.
    """
    from pathlib import Path
    directory = Path(directory)
    if threshold is None:
        threshold = default_merge_threshold()
    nodes = load_nodes(directory)
    pairs = _similarity_pairs(nodes, directory, threshold)
    merged = []
    gone: set[str] = set()
    for a, b, sim, _r in pairs:
        if a in gone or b in gone:
            continue
        if is_protected(nodes[a]) or is_protected(nodes[b]):
            continue  # never auto-merge a curated design doc
        sa = score_meta(nodes[a].meta)[0]
        sb = score_meta(nodes[b].meta)[0]
        keep, drop = (a, b) if sa >= sb else (b, a)
        if not dry_run:
            _repoint(directory, drop, keep)
            if nodes[drop].path.exists():
                nodes[drop].path.unlink()
        gone.add(drop)
        merged.append((drop, keep, sim))
    return merged


def _looks_like_note(out: str, ba: str, bb: str) -> bool:
    """Reject LLM output that is not a clean reconciled note body (empty,
    DISTINCT, too long, or polluted by hook/agent meta-output)."""
    if not out or out.upper().startswith("DISTINCT"):
        return False
    if len(out) > 2500:  # absolute sanity ceiling; pollution is caught below
        return False
    banned = ("ROUKH", "SessionStart", "```", "## ", "NOTE A", "NOTE B",
              "macro", "assessment", "I have what", "FIRST NOTE", "SECOND NOTE")
    return not any(b in out for b in banned)


def resolve_contradictions(directory, low: float = CONTRA_LOW,
                           high: float | None = None, timeout: int = 180) -> list[tuple]:
    """LLM-reconcile similar rule-vs-rule pairs via the local ``claude`` CLI.

    Returns [(dropped, kept)] for pairs that were reconciled into one note.
    No-op (returns []) if the claude CLI is not available. The CLI is invoked
    with stdin closed, hooks disabled (``--settings {hooks:{}}``), and a neutral
    cwd, so it runs as a clean LLM call uncontaminated by global/project hooks.
    high defaults to the backend-appropriate merge threshold (see
    default_merge_threshold).
    """
    import tempfile
    from pathlib import Path
    directory = Path(directory)
    if high is None:
        high = default_merge_threshold()
    if not shutil.which("claude"):
        return []
    neutral_cwd = tempfile.gettempdir()
    nodes = load_nodes(directory)
    pairs = [p for p in _similarity_pairs(nodes, directory, low)
             if low <= p[2] < high and p[3]]  # rule-vs-rule, below merge threshold
    resolved = []
    gone: set[str] = set()
    for a, b, _s, _r in pairs:
        if a in gone or b in gone:
            continue
        ba = frontmatter.parse(nodes[a].path).body
        bb = frontmatter.parse(nodes[b].path).body
        prompt = (
            "You reconcile two operational notes for an LLM agent. Reconcile them "
            "into ONE note body and wrap it EXACTLY between <note> and </note> "
            "tags (plain prose, keep any [[wikilinks]], no headings or frontmatter "
            "inside). If the two notes give conflicting advice, the reconciled note "
            "states the better-justified position. If they are genuinely distinct "
            "and not in conflict, reply with exactly DISTINCT and no tags. You may "
            "think before the tags; only the text inside <note></note> is used.\n\n"
            f"FIRST NOTE:\n{ba}\n\nSECOND NOTE:\n{bb}"
        )
        try:
            # stdin closed (else claude -p blocks on it); hooks disabled and a
            # neutral cwd so it is a clean LLM call, not a full project session.
            r = subprocess.run(
                ["claude", "-p", "--settings", '{"hooks":{}}', prompt],
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, cwd=neutral_cwd)
        except (OSError, subprocess.TimeoutExpired):
            continue
        raw = (r.stdout or "").strip()
        m = re.search(r"<note>(.*?)</note>", raw, re.S)
        if not m:
            continue  # no tagged note (DISTINCT or unusable output) -> leave both
        out = m.group(1).strip()
        if not _looks_like_note(out, ba, bb):
            continue  # extracted body still looks wrong -> leave both notes
        sa = score_meta(nodes[a].meta)[0]
        sb = score_meta(nodes[b].meta)[0]
        keep, drop = (a, b) if sa >= sb else (b, a)
        # Replace the kept note's body with the reconciled text.
        kp = nodes[keep].path
        meta_block = kp.read_text(encoding="utf-8").split("---\n", 2)
        if len(meta_block) >= 3:
            kp.write_text(f"---\n{meta_block[1]}---\n{out}\n", encoding="utf-8")
        _repoint(directory, drop, keep)
        if nodes[drop].path.exists():
            nodes[drop].path.unlink()
        gone.add(drop)
        resolved.append((drop, keep))
    return resolved


# ---------------------------------------------------------------------------
# Lean wiki shape (--lean/--apply, spec §10 decision 6)
# ---------------------------------------------------------------------------

def find_superseded(nodes: dict) -> list[str]:
    """Ids of live notes (``nodes`` - i.e. already excluding archive/attic;
    see ``nodes.load_nodes``) whose frontmatter declares
    ``status: superseded``. These are exactly what ``--lean --apply`` moves
    into ``archive/`` - they are done being live memory, but decision 6
    says archive/attic notes are never deleted, only ever moved there."""
    return sorted(
        nid for nid, node in nodes.items()
        if str(node.meta.get("status", "")).strip().lower() == "superseded"
    )


def _git_mv(src: Path, dest: Path) -> bool:
    """Try ``git mv`` (history-preserving) for *src* -> *dest*. Returns
    True on success; False (never raises) when there is no git checkout
    here, git isn't on PATH, or the move is refused for any reason - the
    caller falls back to a plain filesystem move either way."""
    try:
        r = subprocess.run(
            ["git", "mv", "--", str(src), str(dest)],
            cwd=str(src.parent), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _unique_dest(archive_dir: Path, name: str) -> Path:
    """*name* under *archive_dir*, disambiguated (``-2``, ``-3``, ...) if a
    file by that name is already archived - never silently overwrites an
    earlier archived note."""
    dest = archive_dir / name
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while True:
        candidate = archive_dir / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def archive_note(node_path: Path, archive_dir: Path) -> Path:
    """Move *node_path* into *archive_dir* (created if needed) and return
    its new path. Prefers ``git mv`` (history-preserving) when the wiki
    sits inside a git checkout; falls back to a plain filesystem move
    otherwise. Never deletes anything - the file always still exists,
    just under ``archive/`` (spec §10 decision 6: "archive/ and attic/ ...
    hold history ... never deletes")."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(archive_dir, node_path.name)
    if not _git_mv(node_path, dest):
        shutil.move(str(node_path), str(dest))
    return dest


def apply_lean(directory) -> list[tuple[str, Path]]:
    """Move every ``status: superseded`` live note into ``archive/``
    (:func:`archive_note`). Returns ``[(id, new_path), ...]`` for what was
    moved, in id order."""
    directory = Path(directory)
    nodes = load_nodes(directory)
    archive_dir = directory / "archive"
    moved = []
    for nid in find_superseded(nodes):
        node = nodes[nid]
        if not node.path.exists():
            continue
        dest = archive_note(node.path, archive_dir)
        moved.append((nid, dest))
    return moved


# ---------------------------------------------------------------------------
# --backfill (GAP E, spec §10 decision 7): strip legacy uses/last_used lines
# ---------------------------------------------------------------------------

_LEGACY_USE_FIELD_RE = re.compile(r"^\s*(uses|last_used)\s*:")


def backfill_strip_legacy_uses(directory) -> list[tuple[str, Path]]:
    """Strip the legacy ``uses``/``last_used`` frontmatter lines from every
    live note.

    shapa-backend-spec.md §10 decision 7: those mechanical counters live
    entirely in the index store (``shapa.store``) now, keyed by
    ``(root, note id)`` - a note's own file should carry neither. Only the
    two lines are removed, byte-for-byte, so the rest of a note (including
    line numbers the validator/heartbeat report against) is untouched. A
    note with neither field present is left completely alone - no read, no
    write, no diff - so running this twice, or against a wiki that has
    already been backfilled, touches nothing. Never descends into
    ``archive/``/``attic/`` (decision 6: never loaded, so never touched
    either - see ``nodes.load_nodes``).

    Returns ``[(id, path), ...]`` for every note actually rewritten, in id
    order.
    """
    directory = Path(directory)
    nodes = load_nodes(directory)
    changed: list[tuple[str, Path]] = []
    for nid in sorted(nodes):
        p = nodes[nid].path
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.split("\n")
        if not lines or lines[0].rstrip() != "---":
            continue
        close = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
        if close is None:
            continue
        kept = [
            ln for i, ln in enumerate(lines)
            if not (0 < i < close and _LEGACY_USE_FIELD_RE.match(ln))
        ]
        if kept == lines:
            continue  # nothing legacy here; never rewrite a clean note
        p.write_text("\n".join(kept), encoding="utf-8")
        changed.append((nid, p))
    return changed


def lean_report(directory) -> dict:
    """The ``--lean`` report: lean-shape violations (F10/F11) plus which
    live notes are ``status: superseded`` and so ready to archive."""
    directory = Path(directory)
    violations = validate.check_lean_shape(directory) + validate.check_agenda(directory)
    superseded = find_superseded(load_nodes(directory))
    return {"violations": violations, "superseded": superseded}


# ---------------------------------------------------------------------------
# Memory-log promotion (format 3, spec item 5): never auto-deletes a memory
# record - a promotion writes a curated note then archives the record it
# came from; a plain archive just retires one. The default --prune path
# below never reads the memory log beyond the one-line candidate count.
# ---------------------------------------------------------------------------

def memories_report(directory, min_uses: int = DEFAULT_MIN_MEMORY_USES
                    ) -> list[tuple]:
    """Hot live memory-log records (:func:`shapa.memlog.hot`) with at least
    *min_uses* uses - promotion candidates for a curated note. Each item is
    ``(record, uses, last_used)``."""
    return memlog.hot(Path(directory), min_uses)


def promote_memory(directory, mem_id: str, *, note_id: str | None = None,
                   note_type: str = "memory", scope: str | None = None) -> dict:
    """Promote a live memory-log record into a curated note
    (:func:`shapa.save.save_note`), then archive the record it came from
    (:func:`shapa.memlog.archive` - never deletes). A failed save archives
    nothing. Returns the ``save_note`` result dict (``"id"``/``"path"``/
    ``"scope"``, or ``"error"``)."""
    directory = Path(directory)
    found = memlog.get(directory, mem_id)
    if found is None:
        return {"error": f"memory '{mem_id}' was not found in the log at {directory}"}
    record, status = found
    if status != "active":
        return {"error": f"memory '{mem_id}' is {status}, not active - nothing to promote"}

    resolved_scope = scope or record.scope
    result = save.save_note(
        resolved_scope, record.summary, record.body,
        note_type=note_type, tags=list(record.tags), id=note_id,
        start=directory.parent,
    )
    if "error" in result:
        return result
    memlog.archive(directory, mem_id, reason=f"promoted:{result['id']}")
    return result


def archive_memory(directory, mem_id: str, reason: str = "") -> bool:
    """Archive one memory-log record directly (:func:`shapa.memlog.archive`
    - never deletes). Returns False when *mem_id* is unknown or already
    archived."""
    return memlog.archive(Path(directory), mem_id, reason=reason)


def maintain(directory, prune: bool = False, resolve: bool = False,
             max_age_days: int = 90, now: datetime | None = None,
             dry_run: bool = False, merge_threshold: float | None = None) -> dict:
    from pathlib import Path
    directory = Path(directory)
    now = now or datetime.now(timezone.utc)

    merged = merge_duplicates(directory, threshold=merge_threshold, dry_run=dry_run)
    resolved = resolve_contradictions(directory) if (resolve and not dry_run) else []

    nodes = load_nodes(directory)
    graph = build_graph(nodes)
    orphans = find_orphans(graph)
    stale = find_stale(nodes, now, max_age_days)

    pruned = []
    if prune:
        for nid in sorted(set(orphans) | set(stale)):
            node = nodes.get(nid)
            if node is not None and node.path.exists():
                if not dry_run:
                    node.path.unlink()
                pruned.append(nid)

    return {"merged": merged, "resolved": resolved, "orphans": orphans,
            "stale": stale, "pruned": pruned, "dry_run": dry_run}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.maintain",
        description="Self-heal: auto-merge dupes, prune orphans/stale, optionally LLM-reconcile.",
    )
    parser.add_argument("directory", nargs="?", default=None,
                        help="Memory directory (default: $SHAPA_MEMORY or ~/.shapa/memory).")
    parser.add_argument("--prune", action="store_true", help="Delete orphans and stale notes.")
    parser.add_argument("--resolve", action="store_true",
                        help="LLM-reconcile contradiction candidates via the claude CLI.")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Show what would merge/prune; change nothing.")
    parser.add_argument("--max-age-days", type=int, default=90, dest="max_age_days")
    parser.add_argument("--merge-threshold", type=float, default=None,
                        dest="merge_threshold",
                        help="Similarity >= this auto-merges (default: backend-appropriate - "
                             f"{MERGE_THRESHOLD_EMBED} for embeddings, {MERGE_THRESHOLD_JACCARD} "
                             "for token-Jaccard).")
    parser.add_argument("--lean", action="store_true",
                        help="Report lean-wiki-shape violations (F10/F11) and which "
                             "status:superseded notes are ready to archive. Ignores "
                             "--prune/--resolve/--merge-threshold.")
    parser.add_argument("--apply", action="store_true",
                        help="With --lean: move status:superseded notes into archive/ "
                             "(git mv when this is a git checkout, else a plain move). "
                             "Never deletes.")
    parser.add_argument("--backfill", action="store_true",
                        help="Strip legacy `uses`/`last_used` frontmatter lines from every "
                             "live note (spec §10 decision 7: those counters live in the "
                             "index store now, keyed by root+id - see shapa.store). A note "
                             "with neither field is left untouched. Ignores "
                             "--prune/--resolve/--lean/--merge-threshold.")
    parser.add_argument("--memories", action="store_true",
                        help="List hot live memory-log records (shapa.memlog) with at least "
                             "--min-uses uses: promotion candidates for a curated note.")
    parser.add_argument("--min-uses", type=int, default=DEFAULT_MIN_MEMORY_USES, dest="min_uses",
                        help=f"--memories: minimum uses to list (default {DEFAULT_MIN_MEMORY_USES}).")
    parser.add_argument("--promote", default=None, metavar="MEM_ID", dest="promote",
                        help="Promote a memory-log record into a curated note (shapa.save), "
                             "then archive the record - never deletes; a failed save archives "
                             "nothing.")
    parser.add_argument("--id", default=None, dest="note_id",
                        help="--promote: explicit note id (default: slugified summary).")
    parser.add_argument("--type", default="memory", dest="note_type",
                        choices=sorted(save.VALID_NOTE_TYPES),
                        help="--promote: the curated note's type (default memory).")
    parser.add_argument("--scope", default=None, choices=("global", "repo"), dest="scope",
                        help="--promote: the curated note's scope (default: the record's own scope).")
    parser.add_argument("--archive-memory", default=None, metavar="MEM_ID", dest="archive_memory",
                        help="Archive one memory-log record directly - never deletes.")
    parser.add_argument("--reason", default="", dest="reason",
                        help="--archive-memory: free-text reason recorded in the archive op.")
    args = parser.parse_args(argv)

    if args.memories:
        directory = config.resolve(args.directory)
        rows = memories_report(directory, args.min_uses)
        print(f"=== shapa maintain --memories (min uses {args.min_uses}) ===")
        if not rows:
            print("  no candidates")
        for rec, uses, _last_used in rows:
            print(f"  {rec.id}  uses={uses}  [{rec.kind}]  {rec.summary}")
        sys.exit(0)

    if args.promote:
        directory = config.resolve(args.directory)
        result = promote_memory(directory, args.promote, note_id=args.note_id,
                                note_type=args.note_type, scope=args.scope)
        if "error" in result:
            print(f"shapa maintain: {result['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"promoted {args.promote} -> '{result['id']}' (scope: {result['scope']}) "
              f"at {result['path']}")
        sys.exit(0)

    if args.archive_memory:
        directory = config.resolve(args.directory)
        ok = archive_memory(directory, args.archive_memory, reason=args.reason)
        if not ok:
            print(f"shapa maintain: '{args.archive_memory}' is unknown or already archived "
                  f"in the log at {directory}", file=sys.stderr)
            sys.exit(1)
        print(f"archived memory {args.archive_memory}" + (f" ({args.reason})" if args.reason else ""))
        sys.exit(0)

    if args.backfill:
        directory = config.resolve(args.directory)
        changed = backfill_strip_legacy_uses(directory)
        print("=== shapa maintain --backfill ===")
        print(f"stripped legacy uses/last_used from {len(changed)} note(s): "
              f"{', '.join(nid for nid, _ in changed) or 'none'}")
        sys.exit(0)

    if args.lean:
        directory = config.resolve(args.directory)
        report = lean_report(directory)
        print("=== shapa maintain --lean ===")
        if report["violations"]:
            for v in report["violations"]:
                print(f"  [{v.rule}] {v.severity.upper()}: {v.message}")
        else:
            print("  no lean-shape violations")
        verb = "superseded, ready to archive" if not args.apply else "archiving"
        print(f"status:superseded notes ({len(report['superseded'])}) {verb}: "
              f"{', '.join(report['superseded']) or 'none'}")
        if args.apply:
            moved = apply_lean(directory)
            for nid, dest in moved:
                print(f"  archived {nid} -> {dest}")
            if not moved:
                print("  nothing to archive")
        sys.exit(0)

    r = maintain(config.resolve(args.directory), prune=args.prune, resolve=args.resolve,
                 max_age_days=args.max_age_days, dry_run=args.dry_run,
                 merge_threshold=args.merge_threshold)
    verb = "would merge" if args.dry_run else "merged"
    print("=== shapa maintain" + (" (dry-run)" if args.dry_run else "") + " ===")
    print(f"backend: {'embeddings' if embed.available() else 'lexical (Jaccard)'}")
    print(f"{verb} duplicates ({len(r['merged'])}):")
    for d, k, sim in r["merged"][:40]:
        print(f"  {sim:.3f}  {d}  ->  {k}")
    if not r["merged"]:
        print("  none")
    if args.resolve:
        print(f"reconciled contradictions ({len(r['resolved'])}): " +
              (", ".join(f"{d}->{k}" for d, k in r["resolved"]) or "none"))
    if args.prune:
        pverb = "would prune" if args.dry_run else "pruned"
        print(f"{pverb} orphans+stale ({len(r['pruned'])}): {', '.join(r['pruned']) or 'none'}")
    # The default/--prune path never otherwise reads the memory log - this
    # is the one optional, one-line exception (memlog.hot no-ops when the
    # wiki has no log at all): a cheap nudge toward --memories/--promote,
    # not a scan.
    directory = config.resolve(args.directory)
    if memlog.has_log(directory):
        n = len(memories_report(directory))
        print(f"memory candidates ready for promotion (uses>={DEFAULT_MIN_MEMORY_USES}): {n}")
    sys.exit(0)


if __name__ == "__main__":
    main()
