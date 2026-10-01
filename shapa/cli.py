"""The unified ``shapa`` command - the entry point for the installed tool.

Dispatches subcommands to the engine modules. Memory (the "wiki") lives OUTSIDE
the tool and can be anywhere: a repo-root ``shapa/`` wiki discovered from the
cwd, ``$SHAPA_MEMORY``, the path recorded by ``shapa init``, or the
``~/.shapa/memory`` default. Run ``shapa init [DIR]`` to scaffold a wiki: it
creates the directory, installs the bundled ``AGENTS.md`` rules and the
``arch/`` project templates into it, scaffolds an Obsidian vault, and records
the path so commands and hooks outside any repo resolve the same place.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

from shapa import __version__, config, registry

_SUBMODULES = (
    "bootstrap", "fetch", "capture", "save", "maintain", "heartbeat", "score",
    "validate", "serve", "mcp", "upgrade",
)

#: Docs shipped with the tool and installed into a wiki by ``shapa init``:
#: the ``AGENTS.md`` rules and the ``arch/`` project templates.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

USAGE = f"""shapa {__version__} - operational memory for an LLM agent

usage: shapa <command> [args]

commands:
  init [DIR]     scaffold a wiki: create it, install AGENTS.md + arch/
                 templates, set up an Obsidian vault, and remember the path.
                 Default DIR is ./shapa (a repo-root wiki a session in this
                 repo resolves automatically). Pass ./.shapa for a hidden
                 dot-folder wiki instead — discovery checks .shapa/ before
                 the legacy shapa/ name at every ancestor directory.
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
  fetch          surface relevant memory for a prompt (read path)
  capture        distil a finished session into a note (write path)
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
  serve [ROOT]   run the optional warm per-root daemon (latency only)
  mcp            run the MCP stdio server (search/get/save/placement tools;
                 vendor-neutral - Codex/OpenCode/etc.)

memory dir: ${'{'}SHAPA_MEMORY{'}'} or ~/.shapa/memory  (currently: {config.memory_dir()})
"""


def _install_docs(target: Path) -> list[str]:
    """Install the bundled design docs into *target*, without clobbering edits.

    Copies ``AGENTS.md`` and every file under ``arch/`` from the packaged
    assets into the wiki. Existing files are left untouched, so a user's own
    edits to the design docs survive re-running ``shapa init``.
    """
    installed: list[str] = []
    if not ASSETS_DIR.is_dir():
        return installed
    for src in sorted(ASSETS_DIR.rglob("*.md")):
        rel = src.relative_to(ASSETS_DIR)
        if rel.parts[0] == "skills":
            continue  # harness skills, installed by install.sh - not wiki docs
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


def _init(argv: list[str]) -> None:
    upgrade_docs = "--upgrade-docs" in argv
    positional = [a for a in argv if a != "--upgrade-docs"]

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

    # No argument -> scaffold a wiki at the repo root: ./shapa. A session in this
    # repo then resolves that wiki automatically (config.discover walks up for
    # the shapa/AGENTS.md marker). An explicit DIR overrides the location AND
    # becomes the recorded default; a bare init relies on discovery and leaves
    # the recorded default (e.g. the global LLM-rules wiki) untouched.
    explicit = bool(positional)
    target = Path(positional[0]).expanduser() if explicit else Path.cwd() / config.WIKI_DIRNAME

    # Refuse to scaffold into a pre-existing directory that holds unrelated
    # content (e.g. a Python package literally named ``shapa/``, or a namespace
    # collision): merging wiki files into it would pollute it and could make
    # ``config.discover`` treat it as a wiki thereafter. A directory that is
    # already a wiki (has the AGENTS.md marker) is fine - re-init is idempotent.
    if target.is_dir() and next(target.iterdir(), None) is not None and not (target / config.WIKI_MARKER).exists():
        print(
            f"shapa: refusing to init: {target} already exists and is not a shapa "
            f"wiki (no {config.WIKI_MARKER}). Move it aside or pass an empty/new "
            f"path: shapa init <DIR>.",
            file=sys.stderr,
        )
        sys.exit(2)

    target.mkdir(parents=True, exist_ok=True)
    fresh = not (target / config.WIKI_MARKER).exists()

    installed = _install_docs(target)
    if _ensure_index_gitignore(target):
        installed = [*installed, ".gitignore"]

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

    # Record an explicitly-chosen DIR as the default so commands outside any
    # repo resolve it. A bare init (repo-root ./shapa) is found by discovery, so
    # it must NOT overwrite the recorded default (e.g. the global rules wiki).
    if explicit:
        resolved = config.set_memory_dir(target)
    else:
        resolved = target.resolve()

    # A wiki scaffolded from this shapa's own assets starts at the current
    # format (derived scope fields + the marker). An existing wiki is never
    # migrated by init - that is `shapa upgrade`'s job.
    if fresh:
        from shapa import upgrade

        upgrade.upgrade_wiki(resolved)
    registry.register([resolved], via="init")

    verb = "connected" if explicit else "scaffolded"
    print(f"shapa wiki {verb} at: {resolved}")
    if installed:
        print(f"Installed {len(installed)} doc(s): {', '.join(installed)}")
    else:
        print("Wiki docs already present (left untouched).")
    print("Open this folder as an Obsidian vault to browse the graph.")
    if resolved.name == config.WIKI_DIRNAME:
        print("A session run inside this repo resolves this wiki automatically.")
    if explicit:
        print("This path is now the recorded default; override with $SHAPA_MEMORY.")


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
    if cmd in _SUBMODULES:
        importlib.import_module(f"shapa.{cmd}").main(rest)
        return

    print(f"shapa: unknown command '{cmd}'\n\n{USAGE}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
