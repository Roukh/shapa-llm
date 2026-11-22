"""``shapa hierarchy`` and ``shapa repo`` - how the operator's repos nest, and what each repo may read.

Nothing here is named in the code: the level names, the repo types and the tree are data in two TOML files.

* The **global config** ``<global wiki>/shapa.toml``: ``levels`` (names, outermost first), ``types`` (the repo
  types), optional ``max_depth`` and ``max_children``, ``global_store`` (``local``, or ``hosted`` to make the
  local global wiki a cache of the hosted store), and the tree: groups nested and branched without limit, each
  a table ``[tree."a"."b"]`` with ``repos = [keys]``.
* The **repo config** ``<repo wiki>/shapa.toml``, written by ``shapa init``: ``type``, ``global_access`` (the
  highest global stage this repo reads; unset reads everything, as before) and ``read_stage`` (the lowest
  device stage that may read this repo on the hosted store; above 0 hides it from a ``baseline`` device).

Reading uses the standard library's ``tomllib``; the writer here emits only the shapes above, always quoted, so a
file it wrote reads back equal. A repo is known by its node key (:func:`repo_key`).

CLI (the MCP tools ``hierarchy`` and ``repo`` run the same functions)::

    shapa hierarchy level|type [add NAME | rm NAME]     list or edit the level names / repo types
    shapa hierarchy group PATH [--rm]                   create (or remove an empty) group, PATH = a/b/c
    shapa hierarchy link PATH [REPO]                    put a repo (default: this one) under a group
    shapa hierarchy unlink [REPO]
    shapa hierarchy store [local|hosted]
    shapa hierarchy show [--json] | check [--json]
    shapa repo type [NAME|unset] | stage [N] | access [N|unset]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

from shapa import config, db

CONFIG_NAME = "shapa.toml"
STORES = ("local", "hosted")
#: A group, level or type name, written as a quoted TOML key: no space, quote, dot or slash can break the file.
#: ``\Z``, not ``$``: ``$`` also matches before a trailing newline, which would reach the session text.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,79}\Z")
#: The most a config file may hold. A repo's file arrives with a clone, and every session start reads it.
MAX_FILE_BYTES = 256 * 1024
RESERVED = ("repos",)
#: The session-start line's label and the most siblings it names (hosted/src/instructions.ts holds the same).
PREFIX = "HIERARCHY: "
MAX_SIBLINGS = 8


class HierarchyError(ValueError):
    pass


def _group() -> dict:
    return {"repos": [], "groups": {}}


def empty_global() -> dict:
    return {"global_store": "local", "levels": [], "types": [], "max_depth": None, "max_children": None,
            "tree": _group()}


# --- reading and writing the files -------------------------------------------------------------------

def _read(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HierarchyError(f"{path} is larger than {MAX_FILE_BYTES // 1024} KB")
        text = data.decode("utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise HierarchyError(f"{path} is unreadable ({type(exc).__name__})") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HierarchyError(f"{path} is not valid TOML: {exc}") from None
    except RecursionError:
        raise HierarchyError(f"{path} is nested too deeply to read") from None


def _names(raw, what: str) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(n, str) and NAME_RE.match(n) for n in raw) \
            or len(set(raw)) != len(raw):
        raise HierarchyError(f"{what} must be a list of distinct names ({NAME_RE.pattern})")
    return list(raw)


def _limit(raw, what: str) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise HierarchyError(f"{what} must be a whole number of at least 1")
    return raw


def _read_group(raw, where: str) -> dict:
    if not isinstance(raw, dict):
        raise HierarchyError(f"{where} must be a table")
    repos = raw.get("repos", [])
    if not isinstance(repos, list) or not all(isinstance(r, str) and KEY_RE.match(r) for r in repos):
        raise HierarchyError(f"{where}: repos must be a list of repo keys")
    out = _group()
    out["repos"] = list(repos)
    for name, sub in raw.items():
        if name == "repos":
            continue
        if not NAME_RE.match(name):
            raise HierarchyError(f"{where}: {name!r} is not a group name")
        out["groups"][name] = _read_group(sub, f"{where}.{name}")
    return out


def load_global(root=None) -> dict:
    """The global config (the defaults when the file is absent)."""
    raw = _read(Path(root if root is not None else config.global_root()) / CONFIG_NAME)
    cfg = empty_global()
    store = raw.get("global_store", "local")
    if store not in STORES:
        raise HierarchyError(f"global_store must be one of {', '.join(STORES)}")
    cfg["global_store"] = store
    cfg["levels"] = _names(raw.get("levels", []), "levels")
    cfg["types"] = _names(raw.get("types", []), "types")
    cfg["max_depth"] = _limit(raw.get("max_depth"), "max_depth")
    cfg["max_children"] = _limit(raw.get("max_children"), "max_children")
    try:
        cfg["tree"] = _read_group(raw.get("tree", {}), "tree")
    except RecursionError:
        raise HierarchyError("the tree is nested too deeply to read") from None
    return cfg


def _q(text: str) -> str:
    return json.dumps(text)


def _array(items) -> str:
    return "[" + ", ".join(_q(i) for i in items) + "]"


def _tables(group: dict, path: list[str], out: list[str]) -> None:
    for name, sub in group["groups"].items():
        here = [*path, name]
        out += ["", "[" + ".".join(_q(p) for p in ["tree", *here]) + "]", f"repos = {_array(sub['repos'])}"]
        _tables(sub, here, out)


def dumps_global(cfg: dict) -> str:
    out = ["# shapa hierarchy: edited by `shapa hierarchy`, rewritten whole on every change.",
           f"global_store = {_q(cfg['global_store'])}", f"levels = {_array(cfg['levels'])}",
           f"types = {_array(cfg['types'])}"]
    for key in ("max_depth", "max_children"):
        if cfg[key] is not None:
            out.append(f"{key} = {cfg[key]}")
    _tables(cfg["tree"], [], out)
    return "\n".join(out) + "\n"


def _write(path: Path, text: str) -> None:
    # A repo's wiki arrives with a clone: a symlink left at the temp name must never be written through.
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink(missing_ok=True)
        with open(tmp, "x", encoding="utf-8") as fh:
            fh.write(text)
        tmp.replace(path)
    except OSError as exc:
        raise HierarchyError(f"cannot write {path} ({type(exc).__name__})") from None


def save_global(root, cfg: dict) -> None:
    problems = check(cfg)
    if problems:
        raise HierarchyError(problems[0])
    _write(Path(root if root is not None else config.global_root()) / CONFIG_NAME, dumps_global(cfg))


def load_repo(root) -> dict:
    """A repo wiki's own config: ``{type, global_access, read_stage}``."""
    raw = _read(Path(root) / CONFIG_NAME)
    typ = raw.get("type")
    if typ is not None and not (isinstance(typ, str) and NAME_RE.match(typ)):
        raise HierarchyError(f"type must be a name ({NAME_RE.pattern})")
    access = raw.get("global_access")
    if access is not None and not _stage_ok(access):
        raise HierarchyError(f"global_access must be a whole number from 0 to {db.STAGE_MAX}")
    read = raw.get("read_stage", 0)
    if not _stage_ok(read):
        raise HierarchyError(f"read_stage must be a whole number from 0 to {db.STAGE_MAX}")
    return {"type": typ, "global_access": access, "read_stage": read}


def _stage_ok(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= db.STAGE_MAX


def dumps_repo(cfg: dict) -> str:
    out = ["# this repo in the hierarchy: edited by `shapa repo type|stage|access`."]
    if cfg.get("type"):
        out.append(f"type = {_q(cfg['type'])}")
    if cfg.get("global_access") is not None:
        out.append(f"global_access = {cfg['global_access']}")
    if cfg.get("read_stage"):
        out.append(f"read_stage = {cfg['read_stage']}")
    return "\n".join(out) + "\n"


def save_repo(root, cfg: dict) -> None:
    _write(Path(root) / CONFIG_NAME, dumps_repo(cfg))


def init_repo(root, global_root=None) -> bool:
    """What ``shapa init`` writes: the repo config with the first configured type. An existing file is left
    alone. Returns whether it wrote one."""
    path = Path(root) / CONFIG_NAME
    if path.exists():
        return False
    try:
        types = load_global(global_root)["types"]
    except HierarchyError:
        types = []
    save_repo(root, {"type": types[0] if types else None, "global_access": None, "read_stage": 0})
    return True


# --- the tree ------------------------------------------------------------------------------------------

def _check_name(name: str, what: str) -> str:
    if not NAME_RE.match(name or "") or name in RESERVED:
        raise HierarchyError(f"{what} {name!r} must match {NAME_RE.pattern} and not be one of "
                             f"{', '.join(RESERVED)}")
    return name


def split_path(text: str) -> list[str]:
    return [p for p in (text or "").strip("/").split("/") if p]


def _walk(group: dict, path: list[str] | None = None):
    """Every group below *group* as ``(path, group)``, parents first."""
    path = path or []
    for name, sub in group["groups"].items():
        yield [*path, name], sub
        yield from _walk(sub, [*path, name])


def _find(cfg: dict, path: list[str]) -> dict:
    group = cfg["tree"]
    for name in path:
        if name not in group["groups"]:
            raise HierarchyError(f"no group {'/'.join(path)}")
        group = group["groups"][name]
    return group


def path_of(cfg: dict, key: str) -> list[str] | None:
    """The groups above a linked repo, outermost first; ``None`` when it is not linked."""
    for path, group in _walk(cfg["tree"]):
        if key in group["repos"]:
            return path
    return None


def siblings(cfg: dict, key: str) -> list[str]:
    path = path_of(cfg, key)
    return [] if path is None else [r for r in _find(cfg, path)["repos"] if r != key]


def add_group(cfg: dict, path: list[str]) -> None:
    for name in path:
        _check_name(name, "group")
    group = cfg["tree"]
    for i, name in enumerate(path):
        if name not in group["groups"]:
            group["groups"][name] = _group()
            problems = check(cfg)
            if problems:
                del group["groups"][name]
                raise HierarchyError(problems[0])
        group = group["groups"][name]


def remove_group(cfg: dict, path: list[str]) -> None:
    group = _find(cfg, path)
    if group["repos"] or group["groups"]:
        raise HierarchyError(f"group {'/'.join(path)} is not empty: unlink its repos and remove its groups first")
    del _find(cfg, path[:-1])["groups"][path[-1]]


def link(cfg: dict, path: list[str], key: str) -> None:
    if not KEY_RE.match(key or ""):
        raise HierarchyError(f"{key!r} is not a repo key ({KEY_RE.pattern})")
    held = path_of(cfg, key)
    if held is not None:
        raise HierarchyError(f"{key} is already linked under {'/'.join(held)}: unlink it first")
    group = _find(cfg, path)
    group["repos"].append(key)
    problems = check(cfg)
    if problems:
        group["repos"].remove(key)
        raise HierarchyError(problems[0])


def unlink(cfg: dict, key: str) -> list[str]:
    path = path_of(cfg, key)
    if path is None:
        raise HierarchyError(f"{key} is not linked")
    _find(cfg, path)["repos"].remove(key)
    return path


def add_level(cfg: dict, name: str) -> None:
    _names([*cfg["levels"], name], "levels")
    cfg["levels"].append(name)


def add_type(cfg: dict, name: str) -> None:
    _names([*cfg["types"], name], "types")
    cfg["types"].append(name)


def _drop(cfg: dict, what: str, name: str) -> None:
    if name not in cfg[what]:
        raise HierarchyError(f"{name} is not one of the {what}")
    cfg[what].remove(name)


def check(cfg: dict, repo_type: str | None = None) -> list[str]:
    """Everything wrong with a config, one sentence each (empty when sound). The same test guards every write."""
    problems = []
    depth, children = cfg["max_depth"], cfg["max_children"]
    seen: dict[str, list[str]] = {}
    nodes = [([], cfg["tree"])] + list(_walk(cfg["tree"]))
    for path, group in nodes:
        where = "/".join(path) or "the top"
        if depth is not None and path and len(path) + 1 > depth:
            problems.append(f"group {where} is deeper than max_depth {depth} (a repo below it would be level {len(path) + 1})")
        if children is not None and len(group["groups"]) + len(group["repos"]) > children:
            problems.append(f"{where} holds more than max_children {children}")
        for key in group["repos"]:
            if key in seen:
                problems.append(f"{key} is linked twice (under {'/'.join(seen[key])} and {where})")
            seen[key] = path
    if repo_type is not None and cfg["types"] and repo_type not in cfg["types"]:
        problems.append(f"repo type {repo_type!r} is not one of {', '.join(cfg['types'])}")
    return problems


def repo_key(root) -> str:
    """A repo wiki's node key: its checkout's directory name as a project key (the key ``shapa node`` uses)."""
    name = db.db_path(root).parent.parent.name or "repo"
    return re.sub(r"[^a-z0-9._/-]+", "-", name.lower()).strip("-./")[:70] or "repo"


# --- what a session reads --------------------------------------------------------------------------------

def global_access(repo_root) -> int | None:
    """The highest global stage this repo reads, or ``None`` (unset, or no readable config)."""
    try:
        return load_repo(repo_root)["global_access"]
    except HierarchyError:
        return None


def line(path: list[str], key: str, typ: str | None, sibs: list[str]) -> str:
    """The session-start line: the repo's path in the tree, its type and its siblings. Empty when nothing is
    known. (hosted/src/instructions.ts builds the same text from the repo's node.)"""
    if not path and not typ:
        return ""
    text = PREFIX + " > ".join([*path, key]) + (f" (type {typ})" if typ else "")
    if sibs:
        shown = ", ".join(sibs[:MAX_SIBLINGS])
        more = f" (+{len(sibs) - MAX_SIBLINGS} more)" if len(sibs) > MAX_SIBLINGS else ""
        text += f"; siblings: {shown}{more}"
    return text


def session_line(repo_root) -> str:
    """The line for the repo wiki at *repo_root*; never raises, ``""`` when unconfigured."""
    try:
        key = repo_key(repo_root)
        repo = load_repo(repo_root)
        cfg = load_global()
        path = path_of(cfg, key) or []
        return line(path, key, repo["type"], siblings(cfg, key) if path else [])
    except Exception:
        return ""


# --- the commands ------------------------------------------------------------------------------------------

def repo_wiki(cwd=None) -> Path:
    found = config.discover(cwd)
    if found is None or found.resolve() == config.global_root().resolve():
        raise HierarchyError("this is not a repo wiki: run it from inside a repo that has run `shapa init`")
    return found


def tree_lines(cfg: dict) -> list[str]:
    out = []

    def level(depth: int) -> str:
        return f"{cfg['levels'][min(depth, len(cfg['levels']) - 1)]} " if cfg["levels"] else ""

    def walk(group: dict, depth: int) -> None:
        for key in group["repos"]:
            out.append("  " * depth + f"{level(depth)}{key}")
        for name, sub in group["groups"].items():
            out.append("  " * depth + f"{level(depth)}{name}/")
            walk(sub, depth + 1)

    walk(cfg["tree"], 0)
    return out


def show(cfg: dict, as_json: bool) -> str:
    if as_json:
        return json.dumps(cfg, sort_keys=True)
    head = [f"global_store: {cfg['global_store']}", "levels: " + (", ".join(cfg["levels"]) or "-"),
            "types: " + (", ".join(cfg["types"]) or "-"),
            f"limits: max_depth {cfg['max_depth'] or '-'}, max_children {cfg['max_children'] or '-'}"]
    return "\n".join([*head, *tree_lines(cfg)])


def edit(cfg: dict, cmd: str, a: dict, cwd=None) -> None:
    """One edit of the global config (the CLI and the MCP tool both call it). *a* holds what the command
    names: ``action``, ``name``, ``path``, ``repo``, ``remove``, ``value``. Raises :class:`HierarchyError`."""
    if cmd in ("level", "type"):
        what = "levels" if cmd == "level" else "types"
        if not a.get("name"):
            raise HierarchyError(f"{cmd} {a.get('action')} needs a NAME")
        if a.get("action") == "rm":
            _drop(cfg, what, a["name"])
        else:
            (add_level if what == "levels" else add_type)(cfg, a["name"])
    elif cmd == "group":
        (remove_group if a.get("remove") else add_group)(cfg, split_path(a.get("path", "")))
    elif cmd == "link":
        link(cfg, split_path(a.get("path", "")), a.get("repo") or repo_key(repo_wiki(cwd)))
    elif cmd == "unlink":
        unlink(cfg, a.get("repo") or repo_key(repo_wiki(cwd)))
    elif cmd == "store":
        if a.get("value") not in STORES:
            raise HierarchyError(f"global_store must be one of {', '.join(STORES)}")
        cfg["global_store"] = a["value"]
    else:
        raise HierarchyError(f"no hierarchy command {cmd!r}")


def set_repo(wiki, cmd: str, value) -> dict:
    """Set this repo's ``type``, ``stage`` (read_stage) or ``access`` (global_access) from a typed value
    (a name, a number, or ``unset``) and return the new repo config. Raises :class:`HierarchyError`."""
    key = {"type": "type", "stage": "read_stage", "access": "global_access"}.get(cmd)
    if key is None:
        raise HierarchyError(f"no repo command {cmd!r}")
    cfg = load_repo(wiki)
    unset = str(value) == "unset"
    if cmd == "type":
        known = load_global()["types"]
        if not unset and known and value not in known:
            raise HierarchyError(f"repo type {value!r} is not one of {', '.join(known)}")
        if not unset and not NAME_RE.match(str(value)):
            raise HierarchyError(f"a type must match {NAME_RE.pattern}")
        cfg["type"] = None if unset else value
    elif unset:
        cfg[key] = 0 if cmd == "stage" else None
    else:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = -1
        if not _stage_ok(number):
            raise HierarchyError(f"{cmd} takes a whole number from 0 to {db.STAGE_MAX}"
                                 + (" or unset" if cmd == "access" else ""))
        cfg[key] = number
    save_repo(wiki, cfg)
    return cfg


def run(argv: list[str], cwd=None) -> int:
    p = argparse.ArgumentParser(prog="shapa hierarchy", description="The tree of repos, in the global config.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for what in ("level", "type"):
        s = sub.add_parser(what, help=f"list or edit the {what} names")
        s.add_argument("action", nargs="?", choices=("add", "rm"))
        s.add_argument("name", nargs="?")
    g = sub.add_parser("group", help="create (or with --rm remove an empty) group")
    g.add_argument("path")
    g.add_argument("--rm", dest="remove", action="store_true")
    for what in ("link", "unlink"):
        s = sub.add_parser(what, help=f"{what} a repo (default: this one)")
        if what == "link":
            s.add_argument("path")
        s.add_argument("repo", nargs="?")
    st = sub.add_parser("store", help="where the global wiki lives: local (default) or hosted")
    st.add_argument("value", nargs="?")
    for what in ("show", "check"):
        sub.add_parser(what).add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        root = config.global_root()
        cfg = load_global(root)
        if args.cmd == "show":
            print(show(cfg, args.json))
        elif args.cmd == "check":
            problems = check(cfg, own_type(cwd))
            print(json.dumps({"ok": not problems, "problems": problems}) if args.json
                  else ("ok" if not problems else "\n".join(problems)))
            return 1 if problems else 0
        elif args.cmd in ("level", "type") and args.action is None:
            print("\n".join(cfg["levels" if args.cmd == "level" else "types"]))
        elif args.cmd == "store" and args.value is None:
            print(cfg["global_store"])
        else:
            edit(cfg, args.cmd, vars(args), cwd)
            save_global(root, cfg)
    except HierarchyError as exc:
        print(f"shapa: {exc}", file=sys.stderr)
        return 2
    return 0


def own_type(cwd=None) -> str | None:
    """This repo's own type, for ``check``; ``None`` outside a repo wiki."""
    wiki = config.discover(cwd)
    return load_repo(wiki)["type"] if wiki is not None and wiki.resolve() != config.global_root().resolve() else None


def run_repo(argv: list[str], cwd=None) -> int:
    p = argparse.ArgumentParser(prog="shapa repo", description="This repo's type and access settings.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for what in ("type", "stage", "access"):
        sub.add_parser(what).add_argument("value", nargs="?")
    args = p.parse_args(argv)
    try:
        wiki = repo_wiki(cwd)
        if args.value is None:
            shown = load_repo(wiki)[{"type": "type", "stage": "read_stage", "access": "global_access"}[args.cmd]]
            print(shown if shown is not None else "unset")
        else:
            set_repo(wiki, args.cmd, args.value)
    except HierarchyError as exc:
        print(f"shapa: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(list(sys.argv[1:] if argv is None else argv)))


def main_repo(argv: list[str] | None = None) -> None:
    sys.exit(run_repo(list(sys.argv[1:] if argv is None else argv)))
