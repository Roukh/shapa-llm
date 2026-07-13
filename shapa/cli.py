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

from shapa import __version__, config

_SUBMODULES = ("fetch", "capture", "maintain", "heartbeat", "score", "validate")

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
  where          print the memory directory path
  fetch          surface relevant memory for a prompt (read path)
  capture        distil a finished session into a note (write path)
  maintain       self-heal: auto-merge dupes, prune orphans/stale
                 (--prune, --resolve, --dry-run, --merge-threshold)
  heartbeat      prune orphan notes (--dry-run)
  score          rank notes by value (--use FILE to record a use)
  validate       validate note frontmatter

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
        dst = target / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        installed.append(str(rel))
    return installed


def _init(argv: list[str]) -> None:
    # No argument -> scaffold a wiki at the repo root: ./shapa. A session in this
    # repo then resolves that wiki automatically (config.discover walks up for
    # the shapa/AGENTS.md marker). An explicit DIR overrides the location AND
    # becomes the recorded default; a bare init relies on discovery and leaves
    # the recorded default (e.g. the global LLM-rules wiki) untouched.
    explicit = bool(argv)
    target = Path(argv[0]).expanduser() if explicit else Path.cwd() / config.WIKI_DIRNAME

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

    installed = _install_docs(target)

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
