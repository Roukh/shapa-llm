"""Frontmatter-schema validator for shapa wiki files.

Every file in the wiki - in ``arch/`` and ``memory/`` alike - carries the same
frontmatter: ``id, type, created, consequence, locus, uses``. Connections are
``[[wikilinks]]`` in the body and are not validated here (they are graph data,
checked by the heartbeat). Body format is the maintaining LLM's discretion.

Rules:
  F01  id present and equal to the filename stem
  F02  type is one of: memory, rule, issue, reference
  F03  created is present
  S01  consequence is an integer 1-10
  S02  locus is one of: output, output-meta, meta
  S03  uses is a non-negative integer
  F09  duplicate id across two roots in one wiki_roots() result (error) -
       checked only by --all-roots, since a single-root scan has nothing to
       compare against

CLI::

    python3 -m shapa.validate FILE [FILE ...]
    python3 -m shapa.validate --all-roots [START]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from shapa import config, frontmatter
from shapa.nodes import load_nodes
from shapa.score import LOCUS_WEIGHTS

VALID_TYPES = {"memory", "rule", "issue", "reference"}


@dataclass
class Violation:
    rule: str
    line: int
    message: str
    severity: str = "error"


@dataclass
class ValidationResult:
    valid: bool
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]


def _is_int_in(value, lo: int, hi: int) -> bool:
    try:
        return lo <= int(str(value).strip()) <= hi
    except (TypeError, ValueError):
        return False


def validate_frontmatter(meta: dict, stem: str) -> list[Violation]:
    """Validate the uniform frontmatter schema."""
    v: list[Violation] = []

    node_id = str(meta.get("id", "")).strip()
    if not node_id:
        v.append(Violation("F01", 0, "missing 'id'"))
    elif node_id != stem:
        v.append(Violation("F01", 0, f"id '{node_id}' must equal filename stem '{stem}'"))

    node_type = str(meta.get("type", "")).strip().lower()
    if node_type not in VALID_TYPES:
        v.append(Violation("F02", 0, f"type '{meta.get('type')}' must be one of: {', '.join(sorted(VALID_TYPES))}"))

    if not str(meta.get("created", "")).strip():
        v.append(Violation("F03", 0, "missing 'created'"))

    if not _is_int_in(meta.get("consequence"), 1, 10):
        v.append(Violation("S01", 0, f"consequence '{meta.get('consequence')}' must be an integer 1-10"))

    locus = str(meta.get("locus", "")).strip().lower()
    if locus not in LOCUS_WEIGHTS:
        v.append(Violation("S02", 0, f"locus '{meta.get('locus')}' must be one of: {', '.join(sorted(LOCUS_WEIGHTS))}"))

    uses = str(meta.get("uses", "")).strip()
    try:
        ok = int(uses) >= 0
    except ValueError:
        ok = False
    if not ok:
        v.append(Violation("S03", 0, f"uses '{meta.get('uses')}' must be a non-negative integer"))

    return v


def validate_node(path) -> ValidationResult:
    """Parse and validate a wiki file's frontmatter schema."""
    p = Path(path)
    parsed = frontmatter.parse(p)
    if parsed.error:
        return ValidationResult(valid=False, violations=[Violation("PARSE", 0, parsed.error)])
    violations = validate_frontmatter(parsed.meta, p.stem)
    has_error = any(v.severity == "error" for v in violations)
    return ValidationResult(valid=not has_error, violations=violations)


def check_cross_root_duplicates(roots) -> list[Violation]:
    """F09: a note ``id`` that exists in more than one of *roots* (a
    :func:`shapa.config.wiki_roots` result, or any iterable of
    ``WikiRoot``/path-likes) is a hard error - it is the one correctness
    property the whole multi-root read merge depends on (§4.1 of the spec:
    a silent keep-higher-scored pick is exactly the ambiguity this guards
    against). Reports every colliding id once, naming every root kind it
    was found in."""
    by_id: dict[str, list[str]] = {}
    for root in roots:
        path = getattr(root, "path", root)
        kind = getattr(root, "kind", str(path))
        try:
            nodes = load_nodes(path)
        except OSError:
            continue
        for nid in nodes:
            by_id.setdefault(nid, []).append(kind)

    violations = []
    for nid, kinds in sorted(by_id.items()):
        if len(kinds) > 1:
            violations.append(Violation(
                "F09", 0,
                f"id '{nid}' exists in {len(kinds)} roots ({', '.join(kinds)}) - ambiguous",
            ))
    return violations


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.validate",
        description="Validate shapa wiki-file frontmatter.",
    )
    parser.add_argument("paths", metavar="PATH", nargs="*",
                        help="One or more .md files (default: all notes in the memory dir).")
    parser.add_argument("--all-roots", action="store_true",
                        help="Also check for F09 duplicate ids across every "
                             "wiki_roots() root (global/repo/external), instead "
                             "of validating a single directory's files.")
    args = parser.parse_args(argv)

    if args.all_roots:
        start = args.paths[0] if args.paths else None
        roots = config.wiki_roots(start)
        any_invalid = False
        for root in roots:
            for raw in sorted(str(p) for p in root.path.rglob("*.md")) if root.path.is_dir() else []:
                result = validate_node(raw)
                if not result.valid:
                    any_invalid = True
                print(f"[{root.kind}] {raw}: {'OK' if result.valid and not result.violations else ('OK (with warnings)' if result.valid else 'INVALID')}")
                for v in result.violations:
                    print(f"  [{v.rule}] {v.severity.upper()}: {v.message}")
        dupes = check_cross_root_duplicates(roots)
        for v in dupes:
            any_invalid = True
            print(f"[{v.rule}] {v.severity.upper()}: {v.message}")
        sys.exit(1 if any_invalid else 0)

    paths = args.paths or [str(p) for p in sorted(config.memory_dir().rglob("*.md"))]
    any_invalid = False
    for raw in paths:
        result = validate_node(raw)
        if result.valid and not result.violations:
            print(f"{raw}: OK")
            continue
        if not result.valid:
            any_invalid = True
        print(f"{raw}: {'OK (with warnings)' if result.valid else 'INVALID'}")
        for v in result.violations:
            print(f"  [{v.rule}] {v.severity.upper()}: {v.message}")

    sys.exit(1 if any_invalid else 0)


if __name__ == "__main__":
    main()
