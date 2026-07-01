"""The unified ``shapa`` command - the entry point for the installed tool.

Dispatches subcommands to the engine modules. Memory (the "wiki") lives OUTSIDE
the tool and can be anywhere: ``$SHAPA_MEMORY``, the path recorded by
``shapa init``, or ``~/.shapa/memory``. Run ``shapa init [DIR]`` to connect a
wiki: it creates the directory, installs the bundled design docs (``arch/`` +
``AGENTS.md``) into it, scaffolds an Obsidian vault, and records the path so
every later command and hook resolve the same place.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

from shapa import __version__, config

_SUBMODULES = ("fetch", "capture", "maintain", "heartbeat", "score", "validate")

#: Design docs shipped with the tool and installed into a wiki by ``shapa init``.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

USAGE = f"""shapa {__version__} - operational memory for an LLM agent

usage: shapa <command> [args]

commands:
  init [DIR]     connect a wiki: create it, install the design docs
                 (arch/ + AGENTS.md), scaffold an Obsidian vault, and
                 remember the path (default: $SHAPA_MEMORY or ~/.shapa/memory)
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
    target = Path(argv[0]).expanduser() if argv else config.memory_dir()
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

    # Remember where the wiki is so later commands + hooks resolve the same place.
    resolved = config.set_memory_dir(target)

    print(f"shapa wiki connected at: {resolved}")
    if installed:
        print(f"Installed {len(installed)} design doc(s): {', '.join(installed)}")
    else:
        print("Design docs already present (left untouched).")
    print("Open this folder as an Obsidian vault to browse the graph.")
    print("This path is now the default; override per-invocation with $SHAPA_MEMORY.")


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
