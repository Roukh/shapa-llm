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
  used. Incremented by record_use() (NOT by an LLM). Dormant until shapa has a
  read path; until then it stays 0.

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

from shapa import config, frontmatter

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


def score_node(path: str | Path, now: datetime | None = None) -> ScoreResult:
    """Parse a node file and compute its score."""
    p = Path(path)
    parsed = frontmatter.parse(p)
    score, consequence, locus, fresh, uses = score_meta(parsed.meta, now=now)
    node_id = str(parsed.meta.get("id") or p.stem)
    return ScoreResult(node_id, score, consequence, locus, fresh, uses, p)


# ---------------------------------------------------------------------------
# Mechanical use counter (NOT an LLM judgement)
# ---------------------------------------------------------------------------

def record_use(path: str | Path, now: datetime | None = None) -> int:
    """Increment the node's ``uses`` counter and set ``last_used`` to now.

    Edits only the ``uses`` and ``last_used`` frontmatter lines, leaving the
    rest of the file byte-for-byte intact. Returns the new use count. This is
    the mechanical counter: it is meant to be called by the read/use path, not
    by a language model.
    """
    p = Path(path)
    now = now or datetime.now(timezone.utc)
    now_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = p.read_text(encoding="utf-8").split("\n")
    if not lines or lines[0].rstrip() != "---":
        raise ValueError(f"{p}: no frontmatter to update")
    close = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if close is None:
        raise ValueError(f"{p}: unterminated frontmatter")

    new_uses = uses_of(frontmatter.parse(p).meta) + 1

    def set_line(key: str, value: str) -> None:
        prefix = f"{key}:"
        for i in range(1, close):
            if lines[i].lstrip().startswith(prefix):
                lines[i] = f"{key}: {value}"
                return
        # Not present: insert just before the closing delimiter.
        lines.insert(close, f"{key}: {value}")

    set_line("uses", str(new_uses))
    set_line("last_used", now_iso)
    p.write_text("\n".join(lines), encoding="utf-8")
    return new_uses


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _iter_node_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.md")))  # include the arch/ subdir
        else:
            out.append(p)
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
        for p in _iter_node_files(args.paths):
            try:
                n = record_use(p)
                print(f"{p}: uses -> {n}")
            except ValueError as exc:
                print(f"{p}: {exc}", file=sys.stderr)
        return

    paths = args.paths or [str(config.memory_dir())]
    results = [score_node(p) for p in _iter_node_files(paths)]
    results.sort(key=lambda r: r.score, reverse=True)
    for r in results:
        print(
            f"{r.score:5.3f}  {r.node_id:32}  "
            f"consequence={r.consequence:<2} locus={r.locus:<11} "
            f"fresh={r.freshness:.2f} uses={r.uses}"
        )


if __name__ == "__main__":
    main()
