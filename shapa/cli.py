"""The unified ``shapa`` command - the entry point for the installed tool.

Dispatches subcommands to the engine modules. Memory (the "wiki") lives OUTSIDE
the tool and can be anywhere: the ``.shapa/`` (or legacy ``shapa/``) wiki
discovered by walking up from the cwd, ``$SHAPA_MEMORY``, the path recorded by
``shapa init --global``, or the ``~/.shapa/memory`` default. Run ``shapa
init`` to set a repo up end to end: with no DIR it resolves the git top level
(the PRIMARY checkout's, from a linked worktree) and creates ``.shapa/``
there - or adopts an existing folder of notes when DIR is named explicitly -
installs the bundled ``AGENTS.md`` rules and the ``arch/`` project templates,
links this repo's git triggers (never into a shared or hook-manager-owned
dir it doesn't own - see ``shapa/githooks.py``), and registers the wiki.
Only ``shapa init --global`` records the path as the global wiki, so
commands and hooks outside any repo resolve the same place; ``--obsidian``
additionally scaffolds an Obsidian vault, and ``--no-git`` scaffolds
``./.shapa`` even outside a git work tree.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

from shapa import __version__, config, registry

_SUBMODULES = (
    "bootstrap", "fetch", "capture", "save", "maintain", "heartbeat", "score",
    "validate", "serve", "mcp", "upgrade", "get", "status", "commit",
)

#: Docs shipped with the tool and installed into a wiki by ``shapa init``:
#: the ``AGENTS.md`` rules and the ``arch/`` project templates.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

GIT_TIMEOUT = 10

USAGE = f"""shapa {__version__} - operational memory for an LLM agent

usage: shapa <command> [args]

commands:
  init [DIR] [--global] [--obsidian] [--no-git]
                 set a wiki up end to end: create it, install AGENTS.md +
                 arch/ templates, link this repo's git hooks (never into a
                 shared or hook-manager-owned dir - the lines to add by
                 hand are printed instead), and register it. With no DIR,
                 resolves the git top level (the PRIMARY checkout's, from a
                 linked worktree) and creates .shapa/ there - a session in
                 this repo then resolves it automatically; a legacy shapa/
                 wiki already there is respected instead. Not inside a git
                 work tree: exits 2 unless --no-git, which scaffolds
                 ./.shapa anyway. A named DIR that already holds notes is
                 adopted: missing scaffold files are added, the notes are
                 left untouched (`shapa upgrade DIR` migrates them). Only
                 --global records DIR (default ~/.shapa/memory) as the
                 global wiki pointer. --obsidian additionally scaffolds an
                 Obsidian vault (every mode; off by default).
  init --upgrade-docs [DIR]
                 refresh an EXISTING wiki's AGENTS.md + placement.md from
                 the bundled assets only — never arch/ templates, never a
                 user-written note. DIR defaults to the wiki already in
                 scope (discovery/pointer/default); errors if DIR isn't an
                 existing wiki. Does not scaffold a new wiki — run plain
                 `init` first if none exists yet.
  where          print the memory directory path
  bootstrap      session-start metadata-only overview of every wiki in
                 scope (SessionStart hook; id/type/summary only, no bodies)
  fetch          surface relevant memory for a prompt (read path): md notes
                 and v3 memory records in one fused ranking, one summary
                 line per item
  get ID         the full text behind an id fetch/bootstrap surfaced (a
                 note, or a memory record m-...)
  status         the live recall mode (fused vectors+BM25, or bm25-only
                 without the [semantic] extra), every wiki in scope, this
                 repo's git hooks, and which agent harness (Claude Code,
                 Codex, OpenCode) is actually wired to shapa
  doctor         status, exiting 1 when the install needs a hand: a wiki
                 behind format, a malformed memory-log line, a missing or
                 broken global wiki, this repo's git hooks not wired, or no
                 agent harness connected (a missing repo wiki is only ever
                 informational - `shapa init` creates one)
  capture        extract atomic memories from a finished session into the
                 wiki's memory log (Stop/SubagentStop hook, write path)
  save           write one note explicitly (--scope global|repo|external,
                 --applies-to REPO for external; see shapa/assets/placement.md)
  maintain       self-heal: auto-merge dupes, prune orphans/stale
                 (--prune, --resolve, --dry-run, --merge-threshold);
                 --lean reports lean-shape violations (F10/F11), --apply
                 archives status:superseded notes (never deletes)
  heartbeat      prune orphan notes (--dry-run)
  score          rank notes by value (--use FILE to record a use)
  validate       validate note frontmatter (FILE..., or a wiki DIR)
  upgrade [PATH|--all] [--check] [--json]
                 bring wikis to the current format: apply mechanical
                 migrations, report the judgment work list (exit 1 while
                 any wiki is behind; the shapa-upgrade skill finishes it)
  ledger ...     the work ledger (format 4): features (a new capability:
                 branch + PR), jobs (one commit, subject `J<n>:`; fixes
                 stand alone on a batch branch), tasks; claim, close,
                 tree, edit --parent, issues; git triggers on-commit /
                 on-merge / reconcile; after-feature sweep
  commit [--cwd DIR] [--hook] [--dry-run] [--json] [--scrub TERM ...]
                 commit a wiki's database on the default branch only (a
                 feature branch never commits its own copy); --hook reads
                 cwd/session_id from stdin and never blocks a session
                 (SessionEnd, plus SessionStart as a crash catch-up);
                 --scrub TERM (or config.json's scrub_terms) redacts text
                 before staging
  row ...        memory (M), rule (R) and issue (I) rows: add --scope,
                 edit, rm, link, tag, list
  correction     UserPromptSubmit hook: an operator correction ("no",
                 "wrong", "not like this") becomes an issue row
  serve [ROOT]   run the optional warm per-root daemon (latency only)
  mcp            run the MCP stdio server (search/get/save/placement tools;
                 vendor-neutral - Codex/OpenCode/etc.)

memory dir: ${'{'}SHAPA_MEMORY{'}'} or ~/.shapa/memory  (currently: {config.memory_dir()})
"""


#: What `shapa init` installs into a new wiki - named, never globbed, so a
#: stale build directory re-shipping a removed asset cannot leak it into wikis.
INIT_DOCS = ("AGENTS.md", "placement.md", "arch/index.md")


def _install_docs(target: Path) -> list[str]:
    """Install the bundled design docs into *target*, without clobbering edits.

    Copies ``AGENTS.md`` and every file under ``arch/`` from the packaged
    assets into the wiki. Existing files are left untouched, so a user's own
    edits to the design docs survive re-running ``shapa init``.
    """
    installed: list[str] = []
    if not ASSETS_DIR.is_dir():
        return installed
    for name in INIT_DOCS:
        src = ASSETS_DIR / name
        if not src.is_file():
            continue
        rel = Path(name)
        dst = target / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        installed.append(str(rel))
    return installed


#: The wiki's own read-path caches - `shapa.store`'s `.shapa-index.db` (plus
#: its WAL/SHM siblings) and `shapa.embed`'s `.shapa-vectors.json` - are
#: rebuildable side effects of a read path (fetch/bootstrap/score/mcp
#: search), never authoritative content - a fresh `shapa init` gitignores
#: them so neither can ever show up as an untracked/dirty file in a
#: git-tracked wiki (shapa-backend-spec.md §10 decision 7's acceptance:
#: "running fetch or bootstrap on a clean repo leaves git status clean").
#: Non-destructive: only ever written when the wiki has no `.gitignore` yet,
#: so an operator's own file is never touched. The text lives in
#: `shapa.upgrade.CACHE_GITIGNORE` (imported lazily throughout this module,
#: so the per-prompt hooks that dispatch through `main` never pay for it).
def _ensure_index_gitignore(target: Path) -> bool:
    """Write a ``.gitignore`` covering the index cache into *target* if it
    doesn't already have one. Returns whether it wrote one."""
    from shapa import upgrade

    gitignore = target / ".gitignore"
    if gitignore.exists():
        return False
    gitignore.write_text(upgrade.CACHE_GITIGNORE, encoding="utf-8")
    return True


#: Docs `--upgrade-docs` is allowed to overwrite: the schema/rules file and
#: the placement decision rule. Deliberately excludes arch/ (curated project
#: templates a user may have filled in) and, obviously, any note - this is
#: the schema-refresh path for an existing wiki (shapa-backend-spec.md §6/§10),
#: never a way to touch content the agent/operator authored. The list is
#: `shapa.upgrade.MANAGED_DOCS`.
def _upgrade_docs(target: Path) -> list[str]:
    """Force-overwrite ONLY ``shapa.upgrade.MANAGED_DOCS`` in *target* from
    the bundled assets - refreshing an existing wiki's schema docs without
    touching arch/ templates or any user/agent-written note."""
    from shapa import upgrade

    updated: list[str] = []
    for name in upgrade.MANAGED_DOCS:
        src = ASSETS_DIR / name
        if not src.is_file():
            continue
        shutil.copyfile(src, target / name)
        updated.append(name)
    return updated


#: Flags ``_init`` recognizes anywhere in argv; everything else is positional.
_INIT_FLAGS = ("--upgrade-docs", "--global", "--obsidian", "--no-git")


def _git_top_level(start: Path) -> Path | None:
    """The git top level for *start*, resolved by git itself - the PRIMARY
    checkout's top level when *start* sits in a linked worktree (every
    worktree of a repo shares one ``--git-common-dir`` with its primary
    checkout, so its parent is that checkout's top level either way).
    ``None`` when *start* is not inside a git work tree at all."""
    try:
        r = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--path-format=absolute",
             "--is-inside-work-tree", "--git-common-dir"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = r.stdout.splitlines()
    if r.returncode != 0 or len(lines) < 2 or lines[0].strip() != "true":
        return None
    common_dir = Path(lines[1].strip())
    return common_dir.parent if common_dir.name == ".git" else None


def _existing_wiki_at(top: Path) -> Path | None:
    """A wiki already sitting directly at *top* - ``.shapa/`` checked before
    the legacy ``shapa/`` name, same preference as ``config.discover``."""
    for dirname in config.WIKI_DIRNAMES:
        candidate = top / dirname
        if (candidate / config.WIKI_MARKER).is_file():
            return candidate
    return None


def _install_hooks_quietly(repo: Path) -> list[str]:
    """`shapa init` links this repo's git triggers too (the same installer
    ``shapa ledger git-hooks`` uses) - never a reason to fail init: DIR may
    be outside any git checkout (a global wiki, most often), and a hook
    problem is reported, not fatal."""
    # Only a wiki sitting directly at a checkout's top level links hooks: a
    # global wiki under a home directory that is itself a git repo must not
    # wire shapa into that repo.
    top = _git_top_level(repo)
    if top is None or top.resolve() != Path(repo).resolve():
        return []
    try:
        from shapa import ledger

        return ledger.install_git_hooks(repo)
    except Exception:
        return []


def _init(argv: list[str]) -> None:
    upgrade_docs = "--upgrade-docs" in argv
    make_global = "--global" in argv
    obsidian = "--obsidian" in argv
    no_git = "--no-git" in argv
    positional = [a for a in argv if a not in _INIT_FLAGS]

    if upgrade_docs:
        # Refresh, not scaffold: default target is whatever wiki is already
        # in scope (discovery/pointer/default), not a fresh ./shapa - an
        # explicit DIR still overrides that, same as plain `init`.
        target = Path(positional[0]).expanduser().resolve() if positional else config.memory_dir()
        if not (target / config.WIKI_MARKER).is_file():
            print(
                f"shapa: no wiki found at {target} ({config.WIKI_MARKER} missing) - "
                f"run `shapa init {target}` first, then `shapa init --upgrade-docs {target}` "
                "to refresh its docs. --upgrade-docs never scaffolds a new wiki.",
                file=sys.stderr,
            )
            sys.exit(2)
        updated = _upgrade_docs(target)
        if _ensure_index_gitignore(target):
            updated = [*updated, ".gitignore"]
        registry.register([target], via="init")
        if updated:
            print(f"shapa: refreshed {len(updated)} doc(s) at {target}: {', '.join(updated)}")
        else:
            print(f"shapa: nothing to refresh (bundled assets missing?) at {target}")
        return

    # No DIR, no --global -> set this repo up end to end: resolve the git top
    # level (the PRIMARY checkout's, from a linked worktree) and create
    # .shapa/ there, so a session anywhere in the repo resolves it the same
    # way (config.discover walks up for .shapa/shapa/'s AGENTS.md marker).
    # Not inside a git work tree at all: refuse unless --no-git says to
    # scaffold ./.shapa anyway. The global pointer (~/.shapa/config.json) is
    # written ONLY by --global: a repo wiki scaffolded or adopted by `shapa
    # init DIR` must never repoint every session outside that repo.
    if make_global:
        named = True
        target = Path(positional[0]).expanduser() if positional else config.DEFAULT_DIR
    elif positional:
        named = True
        target = Path(positional[0]).expanduser()
    else:
        named = False
        if no_git:
            top = Path.cwd()
        else:
            top = _git_top_level(Path.cwd())
            if top is None:
                print(
                    "shapa: not inside a git work tree - run `shapa init` from inside a repo, "
                    f"or pass --no-git to scaffold a wiki at {Path.cwd() / '.shapa'} anyway.",
                    file=sys.stderr,
                )
                sys.exit(2)
        # A legacy shapa/ wiki already sitting at the top level is respected
        # instead of scaffolding a second, hidden one beside it.
        existing = _existing_wiki_at(top)
        target = existing if existing is not None else top / ".shapa"

    # A non-empty folder without the AGENTS.md marker is adopted when it was
    # named: missing scaffold files are added, its notes are left untouched.
    # The implicit default still refuses one - that is the collision case
    # (e.g. some other tool's own hidden folder) where merging wiki files in
    # would pollute it and make ``config.discover`` treat it as a wiki
    # thereafter.
    fresh = not target.is_dir() or next(target.iterdir(), None) is None
    adopted = not fresh and not (target / config.WIKI_MARKER).exists()
    if adopted and not named:
        print(
            f"shapa: refusing to init: {target} already exists and is not a shapa "
            f"wiki (no {config.WIKI_MARKER}). Name it to adopt it as a wiki "
            f"(`shapa init {target}`), or pass an empty/new path.",
            file=sys.stderr,
        )
        sys.exit(2)

    target.mkdir(parents=True, exist_ok=True)

    installed = _install_docs(target)
    if _ensure_index_gitignore(target):
        installed = [*installed, ".gitignore"]

    if obsidian:
        obs = target / ".obsidian"
        obs.mkdir(exist_ok=True)
        for name, content in (
            ("app.json", "{}"),
            ("core-plugins.json", '{"graph":true,"backlink":true,"outgoing-link":true}'),
            ("graph.json", '{"showOrphans":true}'),
        ):
            f = obs / name
            if not f.exists():
                f.write_text(content, encoding="utf-8")

    resolved = config.set_memory_dir(target) if make_global else target.resolve()

    # A wiki scaffolded into an empty folder from this shapa's own assets
    # starts at the current format (derived scope fields + the marker). An
    # existing or adopted wiki is never migrated by init - its notes stay
    # byte-identical; that is `shapa upgrade`'s job.
    if fresh:
        from shapa import upgrade

        upgrade.upgrade_wiki(resolved)
    registry.register([resolved], via="init")

    verb = "scaffolded" if fresh else ("adopted" if adopted else "already present")
    print(f"shapa wiki {verb} at: {resolved}")
    if installed:
        print(f"Installed {len(installed)} doc(s): {', '.join(installed)}")
    else:
        print("Wiki docs already present (left untouched).")
    fmt = registry.read_format(resolved)
    if fmt < registry.CURRENT_FORMAT:
        print(f"Existing notes left untouched (format {fmt}<{registry.CURRENT_FORMAT}): "
              f"run `shapa upgrade {resolved}` to migrate them.")
    if obsidian:
        print("Open this folder as an Obsidian vault to browse the graph.")
    if make_global:
        print("This path is now the global wiki (recorded pointer); override with $SHAPA_MEMORY.")

    for line in _install_hooks_quietly(resolved.parent):
        print(line)

    print("Next: a session start surfaces this wiki's memory once it's in scope.")
    print('An operator correction ("no", "wrong", "not like this", ...) becomes an issue row.')
    print('A commit whose subject starts "J<n>:" closes that job.')


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return
    if argv[0] in ("-V", "--version", "version"):
        print(f"shapa {__version__}")
        return

    cmd, rest = argv[0], argv[1:]
    if cmd == "init":
        _init(rest)
        return
    if cmd == "where":
        print(config.memory_dir())
        return
    if cmd == "doctor":
        importlib.import_module("shapa.status").doctor_main(rest)
        return
    if cmd in _SUBMODULES:
        importlib.import_module(f"shapa.{cmd}").main(rest)
        return
    if cmd in ("ledger", "row", "correction"):
        from shapa import ledger

        entry = {"ledger": ledger.ledger_main, "row": ledger.row_main,
                 "correction": ledger.correction_main}[cmd]
        ledger.run(entry, rest)
        return

    print(f"shapa: unknown command '{cmd}'\n\n{USAGE}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
