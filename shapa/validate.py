"""Frontmatter-schema validator for shapa wiki files.

Every file in the wiki - in ``arch/`` and ``memory/`` alike - carries the same
core frontmatter: ``id, type, created, consequence, locus, uses``. Connections
are ``[[wikilinks]]`` in the body and are not validated here (they are graph
data, checked by the heartbeat). Body format is the maintaining LLM's
discretion, subject to the length guidance checked by F07.

Schema v2 (additive, spec §6) layers progressive-disclosure fields onto the
same files: ``summary, scope, applies_to, tags, status, supersedes``. These
are all checked as **warnings** for this release, never errors - existing
(v1) notes without them stay ``valid=True``; the checks exist so ``maintain
--backfill`` and an operator have something concrete to act on, not to break
a corpus that predates the schema. They are promoted to errors only after a
one-time backfill (not yet scheduled).

Rules (core, F01-F03/S01-S03 unchanged from v1):
  F01  id present and equal to the filename stem
  F02  type is one of: memory, rule, issue, reference
  F03  created is present
  S01  consequence is an integer 1-10
  S02  locus is one of: output, output-meta, meta
  S03  uses is a non-negative integer
  F09  duplicate id across two roots in one wiki_roots() result (error) -
       checked only by --all-roots, since a single-root scan has nothing to
       compare against

Rules (schema v2, spec §6 - all warnings this release):
  F04  summary present, <=160 chars, single line
  F05  scope present, one of: global, repo (see note below)
  F06  scope matches the file's physical bucket (global_root() vs. not)
  F07  body length over the type's target (>300 words memory/rule/issue,
       >2000 words reference) - never flags being short, only being long
  F08  supersedes names an id that does not exist (checked only when the
       caller supplies the set of known ids - see ``known_ids=``)
  S04  status is one of: active, superseded, draft

Note on F05/scope: §6's original table lists ``global | repo | external``,
but the operator's decision 6 (spec §10, "Lean wiki shape") retires
``external`` as a note-level scope value ("scope values are global | repo
only. external is retired by decision 4") - there is no more external/<repo>/
staging inside the global wiki for a *note* to declare itself into. This
validator follows that later, more specific decision over the table text it
supersedes. ``wiki_roots()``'s root-*kind* enum (repo/external/global, for
resolving which directories to *read*) is a separate, unrelated concept and is
untouched.

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
#: External is retired as a note-level scope value - see the module
#: docstring's "Note on F05/scope".
VALID_SCOPES = {"global", "repo"}
VALID_STATUSES = {"active", "superseded", "draft"}
#: F07 body-length ceilings (words), keyed by ``type``. ``reference`` gets the
#: 2000-word hard cap; everything else (memory/rule/issue) gets 300. Being
#: *short* is never flagged - only exceeding the ceiling is (spec §6 revised
#: §4: "Past 300 words: warning F07" / "Past 2000 words: F07").
F07_WORD_CEILING = {"reference": 2000}
F07_DEFAULT_CEILING = 300


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


def _word_count(body: str) -> int:
    return len(body.split())


def bucket_of(path) -> str:
    """The physical bucket a file lives in: ``"global"`` if it resolves under
    :func:`shapa.config.global_root`, else ``"repo"`` (the only two buckets
    left after decision 4/6 retired ``external`` staging inside the global
    wiki - see the module docstring). Used by F06 to catch a note whose
    declared ``scope`` disagrees with where it actually lives."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    try:
        g_resolved = config.global_root().resolve()
    except OSError:
        g_resolved = config.global_root()
    try:
        resolved.relative_to(g_resolved)
        return "global"
    except ValueError:
        return "repo"


def validate_frontmatter(
    meta: dict,
    stem: str,
    *,
    body: str = "",
    path=None,
    known_ids: set[str] | None = None,
) -> list[Violation]:
    """Validate the uniform frontmatter schema.

    ``body``/``path``/``known_ids`` are optional context used only by the
    schema-v2 checks (F04-F08/S04): a bare call with just ``meta``/``stem``
    (the v1 signature) still runs F01-F03/S01-S03 exactly as before.
    ``known_ids`` is the set of every id known to the caller's scan (a single
    directory, or every ``wiki_roots()`` root); F08 (dangling ``supersedes``)
    is skipped when it is not supplied - a single arbitrary file has no way to
    know what else exists. F07's word-count ceiling reads ``type`` off *meta*
    itself (already validated as F02 above), not a separate parameter.
    """
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

    # ---- Schema v2 (spec §6) - warnings this release, never errors -------
    summary = meta.get("summary")
    if summary is None or not str(summary).strip():
        v.append(Violation("F04", 0, "missing 'summary' (<=160 chars, single line)", severity="warning"))
    else:
        summary_text = str(summary)
        if "\n" in summary_text:
            v.append(Violation("F04", 0, "'summary' must be a single line (no embedded newline)", severity="warning"))
        elif len(summary_text) > 160:
            v.append(Violation("F04", 0, f"'summary' is {len(summary_text)} chars, must be <=160", severity="warning"))

    scope = meta.get("scope")
    scope_text = str(scope).strip().lower() if scope is not None else ""
    if not scope_text:
        v.append(Violation("F05", 0, "missing 'scope' (must be one of: global, repo)", severity="warning"))
    elif scope_text not in VALID_SCOPES:
        v.append(Violation("F05", 0, f"scope '{scope}' must be one of: {', '.join(sorted(VALID_SCOPES))}", severity="warning"))
    elif path is not None:
        actual = bucket_of(path)
        if scope_text != actual:
            v.append(Violation("F06", 0, f"scope '{scope_text}' does not match physical bucket '{actual}'", severity="warning"))

    if str(body).strip():
        ceiling = F07_WORD_CEILING.get(node_type, F07_DEFAULT_CEILING)
        wc = _word_count(body)
        if wc > ceiling:
            v.append(Violation("F07", 0, f"body is {wc} words, target is <= {ceiling} for type '{node_type}'", severity="warning"))

    supersedes = meta.get("supersedes")
    if supersedes and str(supersedes).strip():
        target = str(supersedes).strip()
        if known_ids is not None and target not in known_ids:
            v.append(Violation("F08", 0, f"supersedes '{target}' does not exist", severity="warning"))

    status = meta.get("status")
    if status is not None and str(status).strip():
        status_text = str(status).strip().lower()
        if status_text not in VALID_STATUSES:
            v.append(Violation("S04", 0, f"status '{status}' must be one of: {', '.join(sorted(VALID_STATUSES))}", severity="warning"))

    return v


def validate_node(path, *, known_ids: set[str] | None = None) -> ValidationResult:
    """Parse and validate a wiki file's frontmatter schema.

    ``known_ids``, when supplied, is the set of every id the caller's scan
    knows about (see :func:`validate_frontmatter`) - used only by F08.
    """
    p = Path(path)
    parsed = frontmatter.parse(p)
    if parsed.error:
        return ValidationResult(valid=False, violations=[Violation("PARSE", 0, parsed.error)])
    violations = validate_frontmatter(
        parsed.meta, p.stem, body=parsed.body, path=p, known_ids=known_ids,
    )
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


def _known_ids_for_paths(paths: list[str]) -> set[str]:
    """Best-effort id set "known" to this scan, for F08 (dangling
    ``supersedes``). Parses each given path's own ``id`` (falling back to the
    filename stem on a parse error), so an ad-hoc list of files still yields
    a meaningful - if partial - known-id set, not just a whole-directory
    default."""
    ids: set[str] = set()
    for raw in paths:
        p = Path(raw)
        try:
            parsed = frontmatter.parse(p)
        except OSError:
            continue
        ids.add(str(parsed.meta.get("id", "")).strip() or p.stem)
    return ids


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
        all_paths = [
            str(p) for root in roots if root.path.is_dir() for p in root.path.rglob("*.md")
        ]
        known_ids = _known_ids_for_paths(all_paths)
        any_invalid = False
        for root in roots:
            for raw in sorted(str(p) for p in root.path.rglob("*.md")) if root.path.is_dir() else []:
                result = validate_node(raw, known_ids=known_ids)
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
    known_ids = _known_ids_for_paths(paths)
    any_invalid = False
    for raw in paths:
        result = validate_node(raw, known_ids=known_ids)
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
