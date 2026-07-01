"""Maintenance - the self-healing pass that keeps the network itself.

It does not report for a human to act on; it maintains the network:

  - PRUNE orphans (no link in or out) and STALE notes (age > t AND uses < 1).
  - AUTO-MERGE near-duplicates: when two notes are ~the same, keep the
    higher-scored one, repoint inbound [[links]] to it, and delete the other.
  - RESOLVE contradictions (--resolve): for similar rule-vs-rule pairs, ask the
    local ``claude`` CLI to reconcile them into one note (or confirm DISTINCT).

Similarity uses local embeddings when available (see shapa.embed), else token
Jaccard. Auto-merge is mechanical and safe; contradiction resolution is an LLM
step, so it is on-demand (--resolve), not part of the per-turn hook.

CLI::
    shapa maintain --prune            # prune + auto-merge dupes (connected wiki)
    shapa maintain --prune --resolve  # also LLM-reconcile contradictions
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from shapa import config, embed, frontmatter
from shapa.heartbeat import find_orphans
from shapa.nodes import build_graph, is_protected, load_nodes
from shapa.score import _parse_ts, score_meta

MERGE_THRESHOLD = 0.9   # >= this similarity -> auto-merge (near-duplicate)
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


def merge_duplicates(directory, threshold: float = MERGE_THRESHOLD,
                     dry_run: bool = False) -> list[tuple]:
    """Auto-merge near-duplicate pairs. Returns [(dropped, kept, sim)].

    With dry_run=True, computes what would merge but changes nothing.
    """
    from pathlib import Path
    directory = Path(directory)
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
                           high: float = MERGE_THRESHOLD, timeout: int = 180) -> list[tuple]:
    """LLM-reconcile similar rule-vs-rule pairs via the local ``claude`` CLI.

    Returns [(dropped, kept)] for pairs that were reconciled into one note.
    No-op (returns []) if the claude CLI is not available. The CLI is invoked
    with stdin closed, hooks disabled (``--settings {hooks:{}}``), and a neutral
    cwd, so it runs as a clean LLM call uncontaminated by global/project hooks.
    """
    import tempfile
    from pathlib import Path
    directory = Path(directory)
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


def maintain(directory, prune: bool = False, resolve: bool = False,
             max_age_days: int = 90, now: datetime | None = None,
             dry_run: bool = False, merge_threshold: float = MERGE_THRESHOLD) -> dict:
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
    parser.add_argument("--merge-threshold", type=float, default=MERGE_THRESHOLD,
                        dest="merge_threshold", help="Similarity >= this auto-merges (default 0.9).")
    args = parser.parse_args(argv)

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
    sys.exit(0)


if __name__ == "__main__":
    main()
