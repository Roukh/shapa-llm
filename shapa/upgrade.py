"""``shapa upgrade`` - bring every wiki to the current format (spec §11).

Two kinds of work, both computed from the wiki's state on disk (never from
its recorded version), so every run is idempotent:

- **Mechanical** (applied here): refresh ``AGENTS.md``/``placement.md`` from
  the shipped assets, add the read-cache entries to ``.gitignore``, move
  legacy ``uses``/``last_used`` frontmatter counters into the index store
  and strip them, derive missing ``id``/``scope`` frontmatter, and write the
  format marker (:mod:`shapa.registry`) once nothing else is left.
- **Judgment** (reported as a per-wiki work list for an agent - the
  ``shapa-upgrade`` skill): lean-shape caps (F10/F11), over-length notes,
  missing summaries, duplicate ids and near-duplicate bodies, invalid
  frontmatter, scope/bucket mismatches, live notes outside root/``arch/``.

A wiki is *current* when its marker equals :data:`registry.CURRENT_FORMAT`,
no mechanical step is pending, and the work list is empty. The exit code is
1 whenever any wiki is behind, so ``--check`` doubles as a CI/installer gate.

CLI::

    shapa upgrade                    # the wikis in scope of the cwd
    shapa upgrade PATH               # one wiki
    shapa upgrade --all              # every registered wiki + those in scope
    shapa upgrade --all --check --json
    shapa upgrade --print-skill      # the bundled shapa-upgrade SKILL.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shapa import __version__, config, embed, frontmatter, maintain, registry, store, validate
from shapa.nodes import is_excluded_path
from shapa.score import _parse_ts

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
SKILL_ASSET = ASSETS_DIR / "skills" / "shapa-upgrade" / "SKILL.md"

#: Schema docs shapa owns inside every wiki - refreshed verbatim from the
#: assets, never edited by the frontmatter migration below, never
#: note-validated (``shapa.validate.MANAGED_DOCS``).
MANAGED_DOCS = validate.MANAGED_DOCS

#: Read-path caches a wiki must never track (store.py / embed.py).
CACHE_IGNORES = (
    store.INDEX_FILENAME,
    f"{store.INDEX_FILENAME}-wal",
    f"{store.INDEX_FILENAME}-shm",
    embed.CACHE_FILENAME,
)
CACHE_GITIGNORE = (
    "# shapa: read-path caches (shapa.store/shapa.embed) - rebuilt\n"
    "# automatically on the next read, never authoritative, never tracked.\n"
    + "".join(f"{name}\n" for name in CACHE_IGNORES)
)

#: Notes the duplicate scan and summary check apply to - operational
#: memory, not curated reference docs.
OPERATIONAL_TYPES = {"memory", "rule", "issue"}

#: Validator codes that become judgment items, with the fix an agent owes.
#: Absent codes are either fixed mechanically here (missing id/scope, S03
#: legacy counters) or deliberately not part of the format (F08: a
#: ``supersedes`` pointing into archive/ is expected).
_WORK_TEXT = {
    "PARSE": "frontmatter does not parse - repair the --- block",
    "NOFM": "no frontmatter - add the note schema or move the file out of the wiki",
    "F01": "id does not match the filename stem - rename one to match",
    "F02": "type must be memory|rule|issue|reference",
    "F03": "missing created timestamp",
    "S01": "consequence must be an integer 1-10",
    "S02": "locus must be output|output-meta|meta",
    "F04": "summary missing or malformed (one line, <=160 chars) - write one",
    "F05": "scope must be global|repo - external is retired; move the note into its project's wiki",
    "F06": "scope disagrees with the wiki it lives in - move the note or correct scope",
    "F07": "body over the word cap (notes <=300, references <=2000) - split into linked notes",
    "S04": "status must be active|superseded|draft",
}


@dataclass
class WorkItem:
    code: str
    message: str
    files: list[str] = field(default_factory=list)


@dataclass
class WikiReport:
    path: str
    status: str                 # current | behind | ahead | missing | not-a-wiki
    format: int | None
    current_format: int = registry.CURRENT_FORMAT
    mechanical: dict[str, list[str]] = field(default_factory=dict)
    work: list[WorkItem] = field(default_factory=list)
    #: relpath -> the legacy counter lines the counters step deletes
    #: (``--check``) or deleted (apply) - the diff, spelled out.
    counters: dict[str, list[str]] = field(default_factory=dict)

    @property
    def behind(self) -> bool:
        return self.status == "behind"


# --- helpers -----------------------------------------------------------------

def _live_md(root: Path) -> list[Path]:
    """Every live ``*.md`` under *root* (archive/attic/.obsidian excluded)."""
    return [p for p in sorted(root.rglob("*.md")) if not is_excluded_path(p.relative_to(root).parts)]


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _is_managed(root: Path, p: Path) -> bool:
    return p.parent == root and p.name in MANAGED_DOCS


def _frontmatter_close(lines: list[str]) -> int | None:
    """Index of the closing ``---`` line, or None when there is no block."""
    if not lines or lines[0].rstrip() != "---":
        return None
    return next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)


def _write_if_changed(path: Path, text: str, dry_run: bool) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    if not dry_run:
        path.write_text(text, encoding="utf-8")
    return True


# --- mechanical steps (each returns the relpaths it changed / would change) ---

def step_docs(root: Path, dry_run: bool) -> list[str]:
    changed = []
    for name in MANAGED_DOCS:
        src = ASSETS_DIR / name
        if src.is_file() and _write_if_changed(root / name, src.read_text(encoding="utf-8"), dry_run):
            changed.append(name)
    return changed


def step_gitignore(root: Path, dry_run: bool) -> list[str]:
    path = root / ".gitignore"
    if not path.exists():
        if not dry_run:
            path.write_text(CACHE_GITIGNORE, encoding="utf-8")
        return [".gitignore"]
    text = path.read_text(encoding="utf-8")
    have = {ln.strip() for ln in text.splitlines()}
    missing = [name for name in CACHE_IGNORES if name not in have]
    if not missing:
        return []
    if not dry_run:
        sep = "" if text.endswith("\n") or not text else "\n"
        path.write_text(text + sep + CACHE_GITIGNORE.split("\n", 2)[0] + "\n"
                        + "".join(f"{n}\n" for n in missing), encoding="utf-8")
    return [".gitignore"]


def _legacy_counter_notes(root: Path) -> list[tuple[Path, list[str], list[int]]]:
    """``(path, lines, legacy line indexes)`` for every live note whose
    frontmatter still carries ``uses``/``last_used`` lines."""
    out = []
    for p in _live_md(root):
        if _is_managed(root, p):
            continue  # refreshed whole by step_docs
        lines = p.read_text(encoding="utf-8").split("\n")
        close = _frontmatter_close(lines)
        if close is None:
            continue
        legacy = [i for i in range(1, close) if maintain._LEGACY_USE_FIELD_RE.match(lines[i])]
        if legacy:
            out.append((p, lines, legacy))
    return out


def counter_lines(root: Path) -> dict[str, list[str]]:
    """``{relpath: [frontmatter lines]}`` - exactly the lines
    :func:`step_counters` deletes, so ``--check`` can show the diff."""
    return {_rel(root, p): [lines[i].strip() for i in legacy]
            for p, lines, legacy in _legacy_counter_notes(root)}


def step_counters(root: Path, dry_run: bool) -> list[str]:
    """Move legacy ``uses``/``last_used`` frontmatter into the index store
    (max/latest wins - no scoring signal lost), then strip the lines."""
    rewrites: dict[Path, str] = {}
    counters: dict[str, tuple[int, str | None]] = {}
    for p, lines, legacy in _legacy_counter_notes(root):
        meta = frontmatter.parse(p).meta
        try:
            uses = max(0, int(str(meta.get("uses", 0)).strip() or 0))
        except ValueError:
            uses = 0
        ts = _parse_ts(meta.get("last_used"))
        last_used = store._now_iso(ts) if ts is not None else None
        if uses or last_used:
            counters[str(meta.get("id") or p.stem)] = (uses, last_used)
        rewrites[p] = "\n".join(ln for i, ln in enumerate(lines) if i not in legacy)
    if not dry_run:
        if counters:
            store.seed_uses(root, counters)  # before the strip, so nothing is lost
        for p, text in rewrites.items():
            p.write_text(text, encoding="utf-8")
    return [_rel(root, p) for p in rewrites]


def step_frontmatter(root: Path, dry_run: bool) -> list[str]:
    """Add the schema-v2 fields that are derivable without judgment: ``id``
    (the filename stem) and ``scope`` (the wiki's bucket: global iff it is
    the global wiki). Lines are inserted before the closing ``---``; the
    rest of the note is untouched."""
    bucket = validate.bucket_of(root)
    changed = []
    for p in _live_md(root):
        if _is_managed(root, p):
            continue
        parsed = frontmatter.parse(p)
        if parsed.error:
            continue
        lines = p.read_text(encoding="utf-8").split("\n")
        close = _frontmatter_close(lines)
        if close is None:
            continue
        new = list(lines)
        for key, value in (("id", p.stem), ("scope", bucket)):
            if str(parsed.meta.get(key, "")).strip():
                continue
            # An empty `key:` line is filled in place, never duplicated.
            blank = next((i for i in range(1, close) if re.match(rf"^{key}:\s*$", new[i])), None)
            if blank is not None:
                new[blank] = f"{key}: {value}"
            else:
                new.insert(close, f"{key}: {value}")
                close += 1
        if new == lines:
            continue
        if not dry_run:
            p.write_text("\n".join(new), encoding="utf-8")
        changed.append(_rel(root, p))
    return changed


MECHANICAL_STEPS = (
    ("docs", step_docs),
    ("gitignore", step_gitignore),
    ("counters", step_counters),
    ("frontmatter", step_frontmatter),
)

#: What each mechanical step changes - printed with every report (and under
#: ``steps`` in ``--json``) so the resulting diff is never mistaken for noise.
#: The counters wording matters: format 1 sessions were told to restore
#: counter-only diffs, which here would undo the migration.
STEP_TEXT = {
    "docs": "AGENTS.md/placement.md rewritten from the shipped assets",
    "gitignore": "read-cache entries added to .gitignore",
    "counters": "legacy uses/last_used frontmatter lines copied into the index store, then "
                "deleted from the notes. The deletion is the migration: commit it with the "
                "upgrade, never restore it",
    "frontmatter": "missing id/scope derived (filename stem / wiki bucket)",
    "format": f"{registry.FORMAT_FILENAME} written once nothing else is left",
}


# --- judgment ----------------------------------------------------------------

def _near_duplicates(root: Path, files: dict[Path, frontmatter.Parsed]) -> list[WorkItem]:
    """Lexical (token-shingle Jaccard) near-duplicates at ``maintain``'s
    merge threshold - always lexical, so the result never depends on
    whether the semantic extra is installed."""
    shingles = {}
    for p, parsed in files.items():
        if p.parent != root or str(parsed.meta.get("type", "")) not in OPERATIONAL_TYPES:
            continue
        if p.stem == "agenda":
            continue
        sh = maintain._shingles(maintain._tokens(parsed.body))
        if sh:
            shingles[p] = sh
    paths = sorted(shingles)
    items = []
    for i, a in enumerate(paths):
        for b in paths[i + 1:]:
            sim = maintain._jaccard(shingles[a], shingles[b])
            if sim >= maintain.MERGE_THRESHOLD_JACCARD:
                items.append(WorkItem(
                    "DUP", f"near-duplicate bodies (similarity {sim:.2f}) - merge into one note",
                    [_rel(root, a), _rel(root, b)],
                ))
    return items


def work_items(root: Path) -> list[WorkItem]:
    items = [WorkItem(v.rule, v.message) for v in validate.check_lean_shape(root) + validate.check_agenda(root)]

    files: dict[Path, frontmatter.Parsed] = {}
    by_code: dict[str, list[str]] = {}
    by_id: dict[str, list[str]] = {}
    outside: list[str] = []
    for p in _live_md(root):
        rel = _rel(root, p)
        parts = p.relative_to(root).parts
        if len(parts) > 1 and parts[0] != "arch":
            outside.append(rel)
        if _is_managed(root, p):
            continue
        parsed = frontmatter.parse(p)
        if parsed.error:
            by_code.setdefault("PARSE", []).append(rel)
            continue
        if _frontmatter_close(p.read_text(encoding="utf-8").split("\n")) is None:
            by_code.setdefault("NOFM", []).append(rel)
            continue
        files[p] = parsed
        by_id.setdefault(str(parsed.meta.get("id") or p.stem), []).append(rel)
        ntype = str(parsed.meta.get("type", "")).strip().lower()
        for v in validate.validate_frontmatter(parsed.meta, p.stem, body=parsed.body, path=p):
            if v.rule not in _WORK_TEXT:
                continue
            if v.rule == "F01" and v.message.startswith("missing"):
                continue  # derived mechanically
            if v.rule == "F05" and v.message.startswith("missing"):
                continue  # derived mechanically
            if v.rule == "F04" and ntype not in OPERATIONAL_TYPES:
                continue  # recommended, not required, for references
            by_code.setdefault(v.rule, []).append(rel)

    for code in _WORK_TEXT:
        if code in by_code:
            items.append(WorkItem(code, _WORK_TEXT[code], by_code[code]))
    for nid, rels in sorted(by_id.items()):
        if len(rels) > 1:
            items.append(WorkItem("DUP-ID", f"id '{nid}' is used by {len(rels)} files - one id per note", rels))
    items.extend(_near_duplicates(root, files))
    if outside:
        items.append(WorkItem(
            "LAYOUT", "live notes outside the wiki root and arch/ escape the lean caps - "
            "move into root/arch within caps, or into archive/ or attic/", outside,
        ))
    return items


# --- one wiki ----------------------------------------------------------------

def upgrade_wiki(path, *, check: bool = False) -> WikiReport:
    """Bring the wiki at *path* to the current format (or, with *check*,
    report what that would take without changing anything)."""
    root = Path(path).expanduser()
    try:
        root = root.resolve()
    except OSError:
        pass
    if not root.is_dir():
        return WikiReport(str(root), "missing", None)
    if not registry.is_wiki(root):
        return WikiReport(str(root), "not-a-wiki", None)
    fmt = registry.read_format(root)
    if fmt > registry.CURRENT_FORMAT:
        # Written by a newer shapa: an older one must never touch it (it
        # would regress AGENTS.md to its own, older asset).
        return WikiReport(str(root), "ahead", fmt)

    counters = counter_lines(root)  # before the step deletes them
    mechanical = {}
    for name, step in MECHANICAL_STEPS:
        changed = step(root, check)
        if changed:
            mechanical[name] = changed
    work = work_items(root)
    if not work and fmt != registry.CURRENT_FORMAT:
        if not check:
            registry.write_format(root)
            fmt = registry.CURRENT_FORMAT
        mechanical["format"] = [registry.FORMAT_FILENAME]

    current = fmt == registry.CURRENT_FORMAT and not work and (not check or not mechanical)
    return WikiReport(str(root), "current" if current else "behind", fmt,
                      mechanical=mechanical, work=work, counters=counters)


# --- CLI ---------------------------------------------------------------------

def _targets(args) -> list[Path]:
    if args.path:
        return [Path(args.path).expanduser()]
    in_scope = [Path(r.path) for r in config.wiki_roots() if registry.is_wiki(r.path)]
    if not args.all:
        return in_scope
    seen, out = set(), []
    for p in [*map(Path, registry.load()), *in_scope]:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _print_human(reports: list[WikiReport], check: bool) -> None:
    verb = "pending" if check else "applied"
    print(f"shapa upgrade: wiki format {registry.CURRENT_FORMAT} (shapa {__version__})"
          + (" [check]" if check else ""))
    for r in reports:
        fmt = "-" if r.format is None else r.format
        print(f"{r.status:<10} {r.path}  (format {fmt})")
        if r.mechanical:
            steps = ", ".join(f"{k} ({len(v)})" for k, v in r.mechanical.items())
            print(f"  mechanical {verb}: {steps}")
        if r.counters:
            print(f"  [counters] {len(r.counters)} note(s): {STEP_TEXT['counters']}")
            for rel, lines in list(r.counters.items())[:5]:
                print(f"    {rel}: {'  '.join(f'-{ln}' for ln in lines)}")
            if len(r.counters) > 5:
                print(f"    (+{len(r.counters) - 5} more)")
        for item in r.work:
            more = f" (+{len(item.files) - 5} more)" if len(item.files) > 5 else ""
            files = f": {', '.join(item.files[:5])}{more}" if item.files else ""
            print(f"  [{item.code}] {item.message}{files}")
    behind = [r for r in reports if r.behind]
    if behind:
        print(f"{len(behind)} wiki(s) behind format {registry.CURRENT_FORMAT} - "
              "run the shapa-upgrade skill")
    else:
        print(f"{len(reports)} wiki(s) checked, none behind")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="shapa upgrade",
        description="Bring wikis to the current shapa format: apply mechanical "
                    "migrations, report the judgment work list.",
    )
    parser.add_argument("path", nargs="?", default=None, help="One wiki (default: the wikis in scope of the cwd).")
    parser.add_argument("--all", action="store_true", help="Every registered wiki plus those in scope.")
    parser.add_argument("--check", action="store_true", help="Change nothing; exit 1 if any wiki is behind.")
    parser.add_argument("--json", action="store_true", help="Machine-readable report.")
    parser.add_argument("--print-skill", action="store_true", dest="print_skill",
                        help="Print the bundled shapa-upgrade SKILL.md (used by install.sh).")
    args = parser.parse_args(argv)

    if args.print_skill:
        sys.stdout.write(SKILL_ASSET.read_text(encoding="utf-8"))
        sys.exit(0)
    if args.path and args.all:
        parser.error("pass PATH or --all, not both")
    if args.path and not registry.is_wiki(Path(args.path).expanduser()):
        print(f"shapa: {args.path} is not a wiki ({config.WIKI_MARKER} missing) - "
              f"run `shapa init {args.path}` first", file=sys.stderr)
        sys.exit(2)

    targets = _targets(args)
    registry.register([t for t in targets if t.exists()], via="upgrade")
    reports = [upgrade_wiki(t, check=args.check) for t in targets]
    if args.all and not args.check:
        gone = [r.path for r in reports if r.status == "missing"]
        if gone:
            registry.unregister(gone)

    if args.json:
        print(json.dumps({
            "shapa": __version__,
            "format": registry.CURRENT_FORMAT,
            "mode": "check" if args.check else "apply",
            "steps": STEP_TEXT,
            "wikis": [asdict(r) for r in reports],
            "behind": [r.path for r in reports if r.behind],
        }, indent=2))
    else:
        _print_human(reports, args.check)
    sys.exit(1 if any(r.behind for r in reports) else 0)


if __name__ == "__main__":
    main()
