"""Node scoring for shapa.

A node's score estimates its value to the LLM agent that consumes this memory.
It combines four fields:

- **consequence** (1-10): how much the agent's performance would degrade if this
  node were absent from context. Author-set at capture time (an LLM judgement);
  the engine does not compute it.
- **locus** (output | output-meta | meta): what the node affects. output = the
  agent's direct answer; output-meta = how the agent works (method/strategy);
  meta = the agent's self-governance (its rules). Drives a weight, and is meant
  to drive retrieval policy too (meta nodes pre-loaded, output nodes on demand).
- **freshness**: exponential decay since the node was last used. Stability (the
  decay time-constant) grows with consequence, so a high-consequence rule barely
  decays while a routine memory fades fast.
- **uses**: a mechanical counter of how many times the node file has been read /
  used. Incremented by record_use() (NOT by an LLM). Lives in the index store
  (``shapa.store``), keyed by ``(root, note id)`` - shapa-backend-spec.md §10
  decision 7, "reads never write notes": a note's own frontmatter changes only
  when its content does, never as a side effect of being read. ``uses``/
  ``last_used`` in a file are tolerated legacy values, read here only as a
  fallback for a note the store has never indexed.

Score formula::

    locus_weight = {output:1.0, output-meta:1.5, meta:2.0}[locus]
    c_norm       = consequence / 10
    stability    = S_MIN + (consequence-1)/9 * (S_MAX - S_MIN)   # days
    freshness    = exp(-age_days / stability)
    use_factor   = 1 + USE_WEIGHT * min(1, log1p(uses)/log1p(USE_SAT))
    score        = locus_weight * c_norm * freshness * use_factor

CLI::

    shapa score                       # rank notes by score (connected wiki)
    shapa score --use <path>/x.md     # record one use (bumps counter)
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shapa import config, frontmatter, store

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

LOCUS_WEIGHTS = {"output": 1.0, "output-meta": 1.5, "meta": 2.0}
DEFAULT_LOCUS = "output"
DEFAULT_CONSEQUENCE = 5

S_MIN_DAYS = 7.0      # stability of a consequence-1 node
S_MAX_DAYS = 365.0    # stability of a consequence-10 node
USE_WEIGHT = 0.5      # max fractional boost from the use counter
USE_SAT = 50.0        # uses at which the boost saturates


@dataclass
class ScoreResult:
    """Computed score and its components for one node."""

    node_id: str
    score: float
    consequence: int
    locus: str
    freshness: float
    uses: int
    path: Path


# ---------------------------------------------------------------------------
# Field extraction (tolerant of missing / malformed values)
# ---------------------------------------------------------------------------

def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def consequence_of(meta: dict) -> int:
    c = _as_int(meta.get("consequence"), DEFAULT_CONSEQUENCE)
    return max(1, min(10, c))


def locus_of(meta: dict) -> str:
    locus = str(meta.get("locus", DEFAULT_LOCUS)).strip().lower()
    return locus if locus in LOCUS_WEIGHTS else DEFAULT_LOCUS


def uses_of(meta: dict) -> int:
    return max(0, _as_int(meta.get("uses"), 0))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def stability_days(consequence: int) -> float:
    """Decay time-constant in days, rising with consequence."""
    return S_MIN_DAYS + (consequence - 1) / 9.0 * (S_MAX_DAYS - S_MIN_DAYS)


def freshness(consequence: int, last_used: datetime | None, now: datetime) -> float:
    """Exponential decay since last use; 1.0 when never aged."""
    if last_used is None:
        return 1.0
    age_days = max(0.0, (now - last_used).total_seconds() / 86400.0)
    return math.exp(-age_days / stability_days(consequence))


def use_factor(uses: int) -> float:
    if uses <= 0:
        return 1.0
    return 1.0 + USE_WEIGHT * min(1.0, math.log1p(uses) / math.log1p(USE_SAT))


def score_meta(meta: dict, now: datetime | None = None) -> tuple[float, int, str, float, int]:
    """Return (score, consequence, locus, freshness, uses) for a meta dict."""
    now = now or datetime.now(timezone.utc)
    consequence = consequence_of(meta)
    locus = locus_of(meta)
    uses = uses_of(meta)
    last_used = _parse_ts(meta.get("last_used")) or _parse_ts(meta.get("created"))
    fresh = freshness(consequence, last_used, now)
    score = LOCUS_WEIGHTS[locus] * (consequence / 10.0) * fresh * use_factor(uses)
    return score, consequence, locus, fresh, uses


def _resolve_root(path: Path, root: str | Path | None) -> Path | None:
    """*root* if given, else the nearest discoverable wiki above *path*
    (:func:`shapa.config.discover`) - never guesses when neither resolves."""
    if root is not None:
        return Path(root)
    return config.discover(path)


def live_use_fields(path: str | Path, meta: dict, root: str | Path | None = None
                     ) -> tuple[int, str | None]:
    """``(uses, last_used)`` for the note at *path*, preferring the index
    store's live counters over the note's own (legacy) frontmatter.

    shapa-backend-spec.md §10 decision 7: usage counters live in
    ``shapa.store``, keyed by ``(root, note id)`` - never re-read from a
    note's frontmatter except as a fallback for a note the store has never
    indexed (``uses`` and ``last_used`` both absent/zero there). *root* is
    the wiki root to query; when not given explicitly it is discovered from
    *path* (see :func:`_resolve_root`) - a note store.py has no indexed root
    for at all (e.g. no discoverable wiki) simply falls back to whatever the
    file itself says, exactly like before store.py existed.

    Read-only: uses ``read_only=True`` (:func:`shapa.store.get_all_uses`),
    so this never creates ``.shapa-index.db`` as a side effect of scoring.
    """
    p = Path(path)
    resolved_root = _resolve_root(p, root)
    if resolved_root is not None:
        note_id = str(meta.get("id") or p.stem)
        live = store.get_all_uses(resolved_root, read_only=True)
        uses, last_used = live.get(note_id, (0, None))
        if uses or last_used:
            return uses, last_used
    return uses_of(meta), meta.get("last_used")


def score_node(path: str | Path, now: datetime | None = None,
               root: str | Path | None = None) -> ScoreResult:
    """Parse a node file and compute its score.

    ``uses``/``last_used`` come from the index store when the note is
    indexed there (live, decision 7); the note's own frontmatter is only
    ever a legacy fallback - see :func:`live_use_fields`. *root* threads
    through to it; omit it to auto-discover the wiki root above *path*.
    """
    p = Path(path)
    parsed = frontmatter.parse(p)
    meta = dict(parsed.meta)
    uses, last_used = live_use_fields(p, meta, root=root)
    meta["uses"] = uses
    if last_used is not None:
        meta["last_used"] = last_used
    score, consequence, locus, fresh, uses_val = score_meta(meta, now=now)
    node_id = str(meta.get("id") or p.stem)
    return ScoreResult(node_id, score, consequence, locus, fresh, uses_val, p)


# ---------------------------------------------------------------------------
# Mechanical use counter (NOT an LLM judgement)
# ---------------------------------------------------------------------------

def record_use(path: str | Path, now: datetime | None = None,
               root: str | Path | None = None) -> int:
    """Record one use of the node at *path* in the index store.

    shapa-backend-spec.md §10 decision 7 ("reads never write notes"): this
    never touches the note's own frontmatter - the mechanical counter lives
    in :func:`shapa.store.record_use`, keyed by ``(root, note id)``. Returns
    the new use count, or 0 when *path* sits outside any discoverable wiki
    root and no explicit *root* was given (never raises).
    """
    p = Path(path)
    resolved_root = _resolve_root(p, root)
    if resolved_root is None:
        return 0
    meta = frontmatter.parse(p).meta
    note_id = str(meta.get("id") or p.stem)
    return store.record_use(resolved_root, note_id, now=now)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _iter_node_files(paths: list[str]) -> list[tuple[Path | None, Path]]:
    """``[(root_hint, file_path), ...]`` for each given path/directory.

    *root_hint* is the directory itself when the argument was a directory -
    matching every real invocation, which passes a wiki root (the default
    memory dir, or an explicit one) - so every file found under it (incl.
    the ``arch/`` subdir) shares that same root for the index-store lookup.
    A bare file argument carries no such hint (``None``); the caller
    resolves its root itself (:func:`_resolve_root` via ``config.discover``).
    """
    out: list[tuple[Path | None, Path]] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend((p, f) for f in sorted(p.rglob("*.md")))  # include arch/
        else:
            out.append((None, p))
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.score",
        description="Score shapa nodes, or record a use of one.",
    )
    parser.add_argument("paths", metavar="PATH", nargs="*",
                        help="Node files or a directory (default: the memory directory).")
    parser.add_argument("--use", action="store_true",
                        help="Record one use of each given node (increments the counter).")
    args = parser.parse_args(argv)

    if args.use:
        if not args.paths:
            print("score --use needs an explicit note path.", file=sys.stderr)
            sys.exit(2)
        for root_hint, p in _iter_node_files(args.paths):
            n = record_use(p, root=root_hint)
            print(f"{p}: uses -> {n}")
        return

    paths = args.paths or [str(config.memory_dir())]
    results = [score_node(p, root=root_hint) for root_hint, p in _iter_node_files(paths)]
    results.sort(key=lambda r: r.score, reverse=True)
    for r in results:
        print(
            f"{r.score:5.3f}  {r.node_id:32}  "
            f"consequence={r.consequence:<2} locus={r.locus:<11} "
            f"fresh={r.freshness:.2f} uses={r.uses}"
        )


if __name__ == "__main__":
    main()
