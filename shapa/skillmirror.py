"""``shapa skill mirror`` - skill FILES stay the source of truth; the wiki database mirrors them.

A skill is a directory holding a ``SKILL.md`` (frontmatter ``name`` and ``description``, then the
text). Two libraries are mirrored, each into the wiki that owns it:

* global: a library directory (``$SHAPA_SKILLS_DIR``, else ``<global wiki>/../skills``, else
  ``--source``), mirrored into the global wiki;
* repo: ``<repo wiki>/skills`` (``.shapa/skills/<name>/SKILL.md``, tracked in git), mirrored into
  that repo's wiki.

The mirror writes the versioned skill documents :mod:`shapa.skills` already defines (kind ``D``,
tags ``skill``, ``skill:NAME``, ``v:N``, ``status:...``); nothing new in the schema. Each mirrored
version carries ``source = mirror:<hash of the text>``, which is how an unchanged file is told from a
changed one without reading a body:

* a new skill is written as an active v1;
* a changed file is a new version, activated at once (the mirror is the operator's own CLI, so it may
  activate; an existing ``stage:N`` tag is kept, a stage is never invented);
* an unchanged file writes nothing, so a second run is a no-op;
* a skill whose directory is gone has every version deleted (with tombstones, so a hosted store
  learns of it) - but only a skill the mirror wrote (some version has a ``mirror:`` source): a skill
  added by hand with ``shapa skill add`` is never touched. A directory that is still there but was
  skipped (a symlink, a bad file) keeps its stored copy.

The file wins: edit the file to change a mirrored skill; a version activated by hand over a mirrored
one is replaced at the next run that sees a different file. Only ``SKILL.md`` is read (never
``references/``); everything about a file is untrusted input: names come from the directory (never the
frontmatter), a symlink is never followed, a file is read at most :data:`BODY_CAP` bytes (cut with a
marker and a warning), and the frontmatter is parsed as plain text for two keys (no YAML is evaluated).

The automatic runs (:func:`auto`: ``shapa upgrade`` and the SessionStart catch-up of ``shapa commit
--hook``) are quiet, bounded in time, never raise, and run a library only when its fingerprint (paths,
sizes and mtimes of its ``SKILL.md`` files) changed since the last mirror; the watermark lives in the
gitignored index, never in ``shapa.db``.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from shapa import config, db, skills

MIRROR_PREFIX = "mirror:"
FILE_NAME = "SKILL.md"
ENV_SOURCE = "SKILLS_DIR"  # read as $SHAPA_SKILLS_DIR
MAX_SKILLS = 10_000        # skill directories read in one run
MAX_DEPTH = 4              # directories below the library root a skill may sit in
MAX_DIRS = 60_000          # directories listed in one run
FRONT_MAX = 65_536         # characters searched for the end of the frontmatter
VALUE_MAX = 20_000         # characters kept of one frontmatter value before it is cut to a line
BODY_CAP = db.DOC_BODY_MAX - 8192  # bytes of a SKILL.md kept (the frontmatter composed anew fits in the rest)
BATCH = 100                # skills written per commit
AUTO_BUDGET_S = 8.0        # an automatic run stops writing after this long
WARNINGS_MAX = 50
SKIP_DIRS = frozenset({"node_modules", "__pycache__"})
_MARKER = "\n\n[truncated by `shapa skill mirror`: this SKILL.md is larger than {cap} bytes]\n"
_META_PREFIX = "skill_mirror:"
_BLOCK = ("|", ">")


@dataclass
class Entry:
    name: str
    path: Path
    description: str
    body: str
    digest: str


@dataclass
class Scan:
    entries: list[Entry] = field(default_factory=list)
    present: set[str] = field(default_factory=set)   # every skill directory seen, skipped ones too
    warnings: list[str] = field(default_factory=list)
    incomplete: bool = False                           # a directory could not be listed: delete nothing
    skipped: int = 0

    def warn(self, text: str, skip: bool = True) -> None:
        self.skipped += int(skip)
        if len(self.warnings) < WARNINGS_MAX:
            self.warnings.append(text)


# --- reading one file ------------------------------------------------------------------------------

def _clean_line(text: str, limit: int) -> str:
    """One line with control, format (zero-width, bidi) and line-separator characters removed."""
    text = "".join(" " if c in "\r\n\t" else ("" if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c)
                   for c in str(text)[:VALUE_MAX])
    return db.one_line(text, limit)


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1].replace('\\"', '"').replace("\\n", " ").replace("\\\\", "\\")
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """``({name, description}, body)`` of a ``SKILL.md``. Plain text, quoted and block (``|``, ``>``)
    scalars, continuation lines; nothing is evaluated (no anchors, tags or merge keys, no other key kept).
    No frontmatter, or one that never closes within :data:`FRONT_MAX` characters: ``({}, whole text)``."""
    text = text.lstrip(chr(0xFEFF))
    window = text[:FRONT_MAX].split("\n")
    if not text.startswith("---") or window[0].rstrip() != "---":
        return {}, text.strip()
    lines: list[str] = []
    close = -1
    for idx in range(1, len(window)):
        if window[idx].rstrip() == "---":
            close = idx
            break
        lines.append(window[idx].rstrip("\r"))
    if close < 0:
        return {}, text.strip()
    end = sum(len(w) for w in window[:close + 1]) + close + 1  # characters up to and with the closing line
    fields: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        key, sep, val = line.partition(":")
        i += 1
        if not sep or line[:1] in (" ", "\t") or not key.strip() or key.strip() not in ("name", "description"):
            continue
        val = val.strip()
        cont: list[str] = []
        while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
            cont.append(lines[i].strip())
            i += 1
        while cont and not cont[-1]:
            cont.pop()
        if val[:1] in _BLOCK:
            fields[key.strip()] = " ".join(c for c in cont if c)
        else:
            fields[key.strip()] = _unquote(" ".join([val, *[c for c in cont if c]]).strip())
    return fields, text[end:].strip()


def _read_skill_file(path: Path, root_real: str) -> tuple[str, int] | str:
    """``(text, size)`` of a regular, non-symlink file inside *root_real* (at most BODY_CAP bytes read), or
    a one-line reason it was not read."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        return f"unreadable ({exc.strerror or type(exc).__name__})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "not a regular file"
        real = os.path.realpath(path)
        if not (real == root_real or real.startswith(root_real + os.sep)):
            return "leads outside the library"
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(BODY_CAP + 1)
        size = st.st_size
    finally:
        os.close(fd)
    cut = len(raw) > BODY_CAP
    text = raw[:BODY_CAP].decode("utf-8", errors="ignore" if cut else "replace").replace("\x00", "")
    if cut:
        text += _MARKER.format(cap=BODY_CAP)
    return text, size


# --- walking a library -----------------------------------------------------------------------------

def scan(source, *, deadline: float | None = None) -> Scan:
    """Read every skill under *source*: a directory (not a symlink, below the root) holding a regular
    ``SKILL.md``. Never raises for a bad entry: it is skipped with a warning."""
    out = Scan()
    root = Path(source)
    try:
        root_real = os.path.realpath(root)
        top = os.scandir(root_real)
        top.close()
    except OSError as exc:
        raise db.LedgerError(f"cannot read the skills directory {root}: {exc.strerror or exc}") from exc
    seen_names: dict[str, Path] = {}
    dirs = 0
    stack: list[tuple[str, int]] = [(root_real, 0)]
    while stack:
        here, depth = stack.pop()
        dirs += 1
        if dirs > MAX_DIRS or len(out.present) >= MAX_SKILLS:
            out.incomplete = True
            out.warn(f"stopped at {MAX_DIRS} directories or {MAX_SKILLS} skills: the rest is not mirrored")
            break
        if deadline is not None and time.monotonic() > deadline:
            out.incomplete = True
            out.warn("out of time while reading the library")
            break
        try:
            with os.scandir(here) as it:
                found = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            out.incomplete = True
            out.warn(f"cannot list {here}: {exc.strerror or exc}")
            continue
        skill_file = next((e for e in found if e.name == FILE_NAME), None)
        if skill_file is not None and depth > 0:
            _take(out, Path(here), skill_file, root_real, seen_names)
            continue  # a skill directory is not searched for more skills (references/ and the like)
        subdirs = []
        for e in found:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                is_link = e.is_symlink()
            except OSError:
                continue
            if is_link:
                if e.name != FILE_NAME and os.path.isdir(e.path):
                    out.warn(f"{e.name}: a symlinked directory is not followed")
                continue
            if not is_dir or e.name.startswith(".") or e.name in SKIP_DIRS or depth + 1 > MAX_DEPTH:
                continue
            subdirs.append((e.path, depth + 1))
        stack.extend(reversed(subdirs))  # sorted order out
    return out


def _take(out: Scan, directory: Path, skill_file, root_real: str, seen: dict[str, Path]) -> None:
    name = directory.name
    where = f"{name}"
    out.present.add(name)
    if not skills.NAME_RE.match(name):
        return out.warn(f"{where}: the directory name is not a skill slug (a-z, 0-9, -; up to 64): skipped")
    if name in seen:
        return out.warn(f"{where}: also at {seen[name]}; the first one is mirrored")
    try:
        if skill_file.is_symlink() or not skill_file.is_file(follow_symlinks=False):
            return out.warn(f"{where}: SKILL.md is a symlink or not a regular file: skipped")
    except OSError:
        return out.warn(f"{where}: SKILL.md cannot be examined: skipped")
    got = _read_skill_file(Path(skill_file.path), root_real)
    if isinstance(got, str):
        return out.warn(f"{where}: SKILL.md {got}: skipped")
    text, size = got
    if size > BODY_CAP:
        out.warn(f"{where}: SKILL.md is {size} bytes; cut to {BODY_CAP} with a marker", skip=False)
    fields, body = parse_frontmatter(text)
    declared = _clean_line(fields.get("name", ""), 80)
    if declared and declared != name:
        out.warn(f"{where}: frontmatter name {declared!r} differs from the directory; the directory names the skill",
                 skip=False)
    description = _clean_line(fields.get("description", ""), skills.DESCRIPTION_MAX)
    if not description:
        return out.warn(f"{where}: no description in the frontmatter: skipped")
    if not body.strip():
        return out.warn(f"{where}: no text after the frontmatter: skipped")
    seen[name] = directory
    digest = hashlib.sha256(f"{name}\0{description}\0{body}".encode("utf-8", errors="replace")).hexdigest()[:16]
    out.entries.append(Entry(name, directory, description, body, digest))


def fingerprint(source) -> str:
    """A hash of the library's skill files' relative paths, sizes and mtimes (and of any directory that
    could not be listed): cheap to take, changes when a SKILL.md is added, removed or edited."""
    h = hashlib.sha256()
    try:
        root_real = os.path.realpath(source)
    except OSError:
        return ""
    stack: list[tuple[str, int]] = [(root_real, 0)]
    dirs = 0
    while stack:
        here, depth = stack.pop()
        dirs += 1
        if dirs > MAX_DIRS:
            h.update(b"!limit")
            break
        try:
            with os.scandir(here) as it:
                found = sorted(it, key=lambda e: e.name)
        except OSError:
            h.update(f"!{here}".encode())
            continue
        for e in found:
            if e.name == FILE_NAME and depth > 0:
                try:
                    st = e.stat(follow_symlinks=False)
                    h.update(f"{os.path.relpath(here, root_real)}\0{st.st_size}\0{st.st_mtime_ns}\0{int(e.is_symlink())}\n".encode())
                except OSError:
                    h.update(f"!{here}/{e.name}".encode())
        if any(e.name == FILE_NAME for e in found) and depth > 0:
            continue
        for e in found:
            try:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP_DIRS and depth + 1 <= MAX_DEPTH:
                    stack.append((e.path, depth + 1))
            except OSError:
                continue
    return h.hexdigest()[:24]


# --- the stored side -------------------------------------------------------------------------------

@dataclass
class Held:
    version: int
    status: str
    doc_id: str
    source: str


def _held(conn: sqlite3.Connection) -> dict[str, list[Held]]:
    """Every stored skill version by name, read without any body (tags and the ``source`` column)."""
    out: dict[str, list[Held]] = {}
    if not db._has_table(conn, "docs"):
        return out
    tags: dict[str, list[str]] = {}
    for rid, tag in conn.execute("SELECT id, tag FROM tags WHERE id >= 'D' AND id < 'E' AND id IN "
                                 "(SELECT id FROM tags WHERE tag = ? AND id >= 'D' AND id < 'E')", (db.SKILL_TAG,)):
        tags.setdefault(rid, []).append(tag)
    sources = {r[0]: r[1] for r in conn.execute(
        "SELECT id, source FROM docs WHERE id IN (SELECT id FROM tags WHERE tag = ? AND id >= 'D' AND id < 'E')",
        (db.SKILL_TAG,))} if tags else {}
    for rid, ts in tags.items():
        name = next((t[len(skills.NAME_TAG):] for t in sorted(ts) if t.startswith(skills.NAME_TAG)), "")
        ver = next((t[len(skills.VERSION_TAG):] for t in sorted(ts) if t.startswith(skills.VERSION_TAG)), "")
        status = next((t[len(skills.STATUS_TAG):] for t in sorted(ts) if t.startswith(skills.STATUS_TAG)), "pending")
        if skills.NAME_RE.match(name) and ver.isdigit() and status in skills.STATUSES:
            out.setdefault(name, []).append(Held(int(ver), status, rid, sources.get(rid, "") or ""))
    for hs in out.values():
        hs.sort(key=lambda h: h.version)
    return out


def _open_readonly(root) -> sqlite3.Connection | None:
    path = db.db_path(root)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


# --- mirroring -------------------------------------------------------------------------------------

@dataclass
class Report:
    scope: str = ""
    source: str = ""
    wiki: str = ""
    dry_run: bool = False
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    partial: bool = False
    skipped_delete: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.deleted)

    def as_dict(self, names: int = 50) -> dict:
        return {"scope": self.scope, "source": self.source, "wiki": self.wiki, "dry_run": self.dry_run,
                "added": len(self.added), "updated": len(self.updated), "deleted": len(self.deleted),
                "unchanged": self.unchanged, "skipped": self.skipped, "partial": self.partial,
                "added_names": self.added[:names], "updated_names": self.updated[:names],
                "deleted_names": self.deleted[:names], "warnings": self.warnings,
                "errors": self.errors[:WARNINGS_MAX], **({"delete_skipped": self.skipped_delete} if self.skipped_delete else {})}


def _plan(entry: Entry, held: list[Held]) -> str:
    """``unchanged`` | ``activate`` (a pending version with this text exists) | ``add`` | ``update``."""
    mark = MIRROR_PREFIX + entry.digest
    active = [h for h in held if h.status == "active"]
    if active and active[-1].source == mark:
        return "unchanged"
    if any(h.status == "pending" and h.source == mark for h in held):
        return "activate"
    return "update" if held else "add"


def mirror(conn: sqlite3.Connection | None, sc: Scan, *, scope: str = "", source: str = "", wiki: str = "",
           dry_run: bool = False, deadline: float | None = None, allow_empty_delete: bool = True) -> Report:
    """Apply a :class:`Scan` to the wiki behind *conn* (``None``: an empty wiki, for a dry run)."""
    rep = Report(scope=scope, source=str(source), wiki=wiki, dry_run=dry_run, skipped=sc.skipped,
                 warnings=list(sc.warnings))
    held = _held(conn) if conn is not None else {}
    todo = []
    for e in sc.entries:
        plan = _plan(e, held.get(e.name, []))
        if plan == "unchanged":
            rep.unchanged += 1
        else:
            todo.append((e, plan))
    gone = [n for n, hs in held.items() if n not in sc.present and any(h.source.startswith(MIRROR_PREFIX) for h in hs)]
    if gone and sc.incomplete:
        rep.skipped_delete = "the library could not be read completely: nothing deleted"
        gone = []
    elif gone and not sc.entries and not allow_empty_delete:
        rep.skipped_delete = "the library is empty: an automatic run deletes nothing (run `shapa skill mirror`)"
        gone = []
    if dry_run:
        for e, plan in todo:
            (rep.added if plan == "add" else rep.updated).append(e.name)
        rep.deleted = sorted(gone)
        return rep
    assert conn is not None
    work: list[tuple[str, object, str]] = [("w", e, plan) for e, plan in todo] + [("d", n, "") for n in sorted(gone)]
    for start in range(0, len(work), BATCH):
        if deadline is not None and time.monotonic() > deadline:
            rep.partial = True
            rep.warnings.append("out of time: the rest is mirrored by the next run")
            break
        conn.execute("BEGIN IMMEDIATE")
        try:
            for kind, item, plan in work[start:start + BATCH]:
                if kind == "d":
                    name = str(item)
                    with db.write(conn):
                        db.remove(conn, [h.doc_id for h in held[name]])
                    rep.deleted.append(name)
                else:
                    _apply(conn, item, plan, held.get(item.name, []), rep)  # type: ignore[arg-type]
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return rep


def _apply(conn, e: Entry, plan: str, held: list[Held], rep: Report) -> None:
    mark = MIRROR_PREFIX + e.digest
    try:
        with db.write(conn):
            if plan == "add":
                skills.add_version(conn, e.name, e.description, e.body, new=True, active=True, source=mark, privileged=True)
                rep.added.append(e.name)
                return
            if plan == "activate":
                target = next(h for h in held if h.status == "pending" and h.source == mark)
                skills.activate(conn, e.name, target.version)
            else:
                v = skills.add_version(conn, e.name, e.description, e.body, source=mark, privileged=True)
                skills.activate(conn, e.name, v.version)
            rep.updated.append(e.name)
    except db.LedgerError as exc:
        if len(rep.errors) < 1000:
            rep.errors.append(f"{e.name}: {exc}")


# --- where a library is ----------------------------------------------------------------------------

def default_source(scope: str, wiki: Path | None = None) -> Path:
    """The library directory of *scope*: global is ``$SHAPA_SKILLS_DIR``, else ``<global wiki>/../skills``;
    repo is ``<repo wiki>/skills``."""
    if scope == "global":
        env = os.environ.get("SHAPA_" + ENV_SOURCE)
        if env:
            return Path(env).expanduser()
        return config.global_root().parent / "skills"
    if wiki is None:
        raise db.LedgerError("no repo wiki here")
    return Path(wiki) / "skills"


def _mark_key(scope: str, source) -> str:
    return _META_PREFIX + scope + ":" + hashlib.sha1(str(os.path.realpath(source)).encode()).hexdigest()[:10]


def _watermark(root, key: str) -> str | None:
    from shapa import metrics

    conn = metrics.open_local(root, create=False, timeout=0.5)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM m_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _set_watermark(root, key: str, value: str) -> None:
    from shapa import metrics

    conn = metrics.open_local(root, create=True, timeout=0.5)
    if conn is None:
        return
    try:
        conn.execute("INSERT OR REPLACE INTO m_meta(key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def run_scope(scope: str, root, source, *, dry_run: bool = False, deadline: float | None = None,
              auto: bool = False) -> Report:
    """Mirror one library into the wiki at *root*. Raises :class:`shapa.db.LedgerError` when the library
    cannot be read; per-skill trouble is in the report."""
    sc = scan(source, deadline=deadline)
    if dry_run:
        conn = _open_readonly(root)
        try:
            return mirror(conn, sc, scope=scope, source=source, wiki=str(root), dry_run=True)
        finally:
            if conn is not None:
                conn.close()
    conn = db.connect(root, create=not auto, busy_ms=1000 if auto else None)
    if conn is None:
        raise db.LedgerError(f"{root} has no database yet")
    try:
        rep = mirror(conn, sc, scope=scope, source=source, wiki=str(root), deadline=deadline,
                     allow_empty_delete=not auto)
    finally:
        conn.close()
    if not rep.partial and not rep.errors and not sc.incomplete:
        try:
            _set_watermark(root, _mark_key(scope, source), fingerprint(source))
        except Exception:
            pass
    return rep


def auto(cwd=None, *, budget: float = AUTO_BUDGET_S) -> list[Report]:
    """The quiet, bounded runs: the global library and the repo's own, each only when its fingerprint
    changed since the last mirror and its wiki has a database. Never raises, never blocks for long."""
    out: list[Report] = []
    try:
        deadline = time.monotonic() + budget
        targets: list[tuple[str, Path, Path]] = []
        roots = config.wiki_roots(cwd)
        for wr in roots:
            if wr.kind == "repo":
                targets.append(("repo", Path(wr.path), default_source("repo", Path(wr.path))))
        groot = config.global_root()
        targets.append(("global", groot, default_source("global")))
        seen: set[tuple[str, str]] = set()
        for scope, root, source in targets:
            try:
                key = (scope, str(Path(source).resolve()))
                if key in seen or not Path(source).is_dir() or not db.exists(root):
                    continue
                seen.add(key)
                fp = fingerprint(source)
                if fp and _watermark(root, _mark_key(scope, source)) == fp:
                    continue
                if time.monotonic() > deadline:
                    break
                out.append(run_scope(scope, root, source, deadline=deadline, auto=True))
            except Exception:
                continue
    except Exception:
        pass
    return out
