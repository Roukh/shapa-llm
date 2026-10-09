"""``shapa status`` / ``shapa doctor`` - whether the WHOLE install is sound:
the live retrieval mode, every wiki in scope, this repo's git hooks, and
whether any agent harness is actually wired to shapa.

The mode line is the point (memory v3): with the ``[semantic]`` extra,
recall fuses float32 model2vec vectors with FTS5 BM25; without it, recall
runs BM25-only. Both work - but which one is running is always reported,
never silent. ``status`` loads the embedding model to prove the extra
actually works (an installed-but-broken model cache reads as bm25-only
here, exactly as every read path would treat it).

``doctor`` prints the same report and exits 1 when something needs a hand:
a wiki behind the current format, a malformed memory-log line, a wiki path
in scope that is missing, a missing or broken global wiki, this repo's git
hooks not wired, or no agent harness connected (Claude Code hooks/MCP,
Codex MCP, OpenCode MCP - see ``harness_report``, which reads exactly what
``install.sh`` writes). ``status`` shows the same report but never exits 1;
a missing *repo* wiki specifically is informational either way - `shapa
init` is how you'd get one, not a fault.

CLI::

    shapa status [--cwd DIR] [--json]
    shapa doctor [--cwd DIR] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from shapa import __version__, config, embed, memlog, registry, store
from shapa.nodes import load_nodes


def mode_report(load_model: bool = True) -> dict:
    semantic = embed.available() if load_model else embed.installed()
    lexical = memlog.lexical_backend()
    if semantic:
        desc = f"fused: {embed.model_name()} float32 vectors + {lexical} BM25"
    else:
        desc = (f"bm25-only: {lexical} - the [semantic] extra is not installed (or its model "
                "does not load); install shapa[semantic] for vectors")
    try:
        import mcp  # noqa: F401
        mcp_sdk = True
    except ImportError:
        mcp_sdk = False
    return {"mode": "fused" if semantic else "bm25", "description": desc,
            "lexical": lexical, "semantic": semantic, "fts5": store.fts5_available(),
            "mcp_transport": "stdio (mcp SDK installed)" if mcp_sdk else "stdio (stdlib JSON-RPC)"}


def _index_state(root: Path) -> dict:
    path = store.db_path(root)
    if not path.is_file():
        return {"exists": False, "fresh": False, "vectors": 0}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return {"exists": True, "fresh": False, "vectors": 0}
    try:
        fresh = memlog._fresh(conn, root) if memlog.has_log(root) else True
        try:
            vectors = conn.execute("SELECT count(*) FROM memory_vectors").fetchone()[0]
        except sqlite3.Error:
            vectors = 0
    finally:
        conn.close()
    return {"exists": True, "fresh": bool(fresh), "vectors": int(vectors),
            "bytes": path.stat().st_size}


def wiki_report(wr) -> dict:
    root = Path(wr.path)
    out = {"kind": wr.kind, "repo": wr.repo, "path": str(root), "exists": root.is_dir(),
           "is_wiki": registry.is_wiki(root)}
    if not out["exists"]:
        return out
    fmt = registry.read_format(root)
    out.update({
        "format": fmt, "current_format": registry.CURRENT_FORMAT,
        "notes": len(load_nodes(root)),
        "memory": memlog.stats(root),
        "index": _index_state(root),
    })
    return out


# --- git hooks (this repo's wiki, if any) -----------------------------------

def git_hooks_report(start=None) -> dict | None:
    """``githooks.state()`` for the repo this wiki belongs to - ``None`` when
    there is no repo wiki in scope (nothing to link hooks into)."""
    repo_root = next((Path(wr.path) for wr in config.wiki_roots(start) if wr.kind == "repo"), None)
    if repo_root is None:
        return None
    try:
        from shapa import githooks

        st = githooks.state(repo_root.parent)
    except Exception:
        return None
    return {
        "shared": st["shared"], "manager": st["manager"],
        "hooks": {name: {"wired": h["wired"], "path": str(h["path"])}
                  for name, h in st["hooks"].items()},
    }


def repo_wiki_missing_here(start=None) -> bool:
    """True when *start* sits inside a git work tree that has no repo wiki
    of its own yet (informational, never a problem - `shapa init` fixes
    it)."""
    if any(wr.kind == "repo" for wr in config.wiki_roots(start)):
        return False
    try:
        here = Path(start).resolve() if start else Path.cwd().resolve()
    except OSError:
        return False
    for d in (here, *here.parents):
        try:
            if (d / ".git").exists():
                return True
        except OSError:
            continue
    return False


# --- agent harnesses: is anything actually wired to shapa? ------------------

def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _claude_settings_paths(start=None) -> list[Path]:
    """Every settings.json ``install.sh`` could have written a shapa hook
    into: the harness-wide one (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``),
    plus this repo's project-level ones."""
    home_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    repo = Path(start).resolve() if start else Path.cwd()
    return [home_dir / "settings.json", repo / ".claude" / "settings.json",
            repo / ".claude" / "settings.local.json"]


def _hook_shapa_commands(settings: dict) -> list[str]:
    """Every hook command anywhere in *settings* that invokes ``shapa``."""
    out = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks") or []:
                cmd = h.get("command") if isinstance(h, dict) else None
                if isinstance(cmd, str) and "shapa" in cmd:
                    out.append(cmd)
    return out


def _resolves(cmd: str) -> bool:
    """Whether the program a hook/MCP *cmd* string invokes would actually
    run: an absolute path must exist and be executable, a bare name must be
    on PATH, and the documented ``python3 -m shapa`` fallback resolves if
    ``python3`` does. Leading ``VAR=value`` env assignments are skipped, and
    in a guarded command (``command -v shapa >/dev/null && shapa bootstrap``)
    the segment that actually runs shapa is the one checked."""
    segments = [seg.split() for seg in re.split(r"&&|\|\||;", cmd)]
    for i, parts in enumerate(segments):
        while parts and "=" in parts[0] and not parts[0].startswith(("/", "-")):
            parts = parts[1:]
        segments[i] = parts
    runs_shapa = [p for p in segments if p and p[0] != "command"
                  and (Path(p[0]).name == "shapa" or p[1:3] == ["-m", "shapa"])]
    parts = runs_shapa[0] if runs_shapa else next((p for p in segments if p), [])
    if not parts:
        return False
    prog = parts[0]
    if prog in ("python3", "python") and parts[1:3] == ["-m", "shapa"]:
        return shutil.which(prog) is not None
    if "/" in prog:
        p = Path(prog)
        return p.is_file() and os.access(p, os.X_OK)
    return shutil.which(prog) is not None


def _claude_mcp_command() -> tuple[bool, str | None]:
    """``claude mcp add shapa --scope user`` registers in ``~/.claude.json``
    (not ``$CLAUDE_CONFIG_DIR`` - that's the Claude Code CLI's own user-level
    config file, independent of the settings.json hooks live in)."""
    data = _load_json(Path.home() / ".claude.json")
    entry = (data.get("mcpServers") or {}).get("shapa") if isinstance(data.get("mcpServers"), dict) else None
    if not isinstance(entry, dict):
        return False, None
    command = entry.get("command")
    return True, command if isinstance(command, str) else None


_CODEX_MCP_HEADER = "[mcp_servers.shapa]"


def _codex_mcp_command() -> tuple[bool, str | None]:
    path = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False, None
    if _CODEX_MCP_HEADER not in text:
        return False, None
    rest = text[text.index(_CODEX_MCP_HEADER):]
    end = rest.find("\n[", 1)
    block = rest if end == -1 else rest[:end]
    m = re.search(r'(?m)^\s*command\s*=\s*"((?:[^"\\]|\\.)*)"', block)
    cmd = m.group(1).replace('\\"', '"').replace("\\\\", "\\") if m else None
    return True, cmd


def _opencode_mcp_command() -> tuple[bool, str | None]:
    path = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "opencode" / "opencode.json"
    data = _load_json(path)
    mcp = data.get("mcp")
    entry = mcp.get("shapa") if isinstance(mcp, dict) else None
    if not isinstance(entry, dict):
        return False, None
    command = entry.get("command")
    if isinstance(command, list) and command:
        return True, command[0] if isinstance(command[0], str) else None
    return True, command if isinstance(command, str) else None


def harness_report(start=None) -> dict:
    """Which agent harness is actually wired to shapa, and whether the
    command each one would run actually resolves - the surface
    ``install.sh``/``bootstrap.sh`` write to (see their own docstrings)."""
    claude_hook_cmds: list[str] = []
    for p in _claude_settings_paths(start):
        claude_hook_cmds.extend(_hook_shapa_commands(_load_json(p)))
    claude_hooks_wired = bool(claude_hook_cmds)
    claude_mcp_wired, claude_mcp_cmd = _claude_mcp_command()
    codex_mcp_wired, codex_mcp_cmd = _codex_mcp_command()
    opencode_mcp_wired, opencode_mcp_cmd = _opencode_mcp_command()

    broken: list[tuple[str, str]] = []
    if claude_hooks_wired and not _resolves(claude_hook_cmds[0]):
        broken.append(("claude hook", claude_hook_cmds[0]))
    if claude_mcp_wired and claude_mcp_cmd and not _resolves(claude_mcp_cmd):
        broken.append(("claude mcp", claude_mcp_cmd))
    if codex_mcp_wired and codex_mcp_cmd and not _resolves(codex_mcp_cmd):
        broken.append(("codex mcp", codex_mcp_cmd))
    if opencode_mcp_wired and opencode_mcp_cmd and not _resolves(opencode_mcp_cmd):
        broken.append(("opencode mcp", opencode_mcp_cmd))

    return {
        "connected": claude_hooks_wired or claude_mcp_wired or codex_mcp_wired or opencode_mcp_wired,
        "claude_hooks": claude_hooks_wired, "claude_mcp": claude_mcp_wired,
        "codex_mcp": codex_mcp_wired, "opencode_mcp": opencode_mcp_wired,
        "broken": broken,
    }


def gather(start=None, *, load_model: bool = True) -> dict:
    roots = config.wiki_roots(start)
    return {"shapa": __version__, "recall": mode_report(load_model),
            "wikis": [wiki_report(wr) for wr in roots],
            "repo_wiki_missing_here": repo_wiki_missing_here(start),
            "git_hooks": git_hooks_report(start),
            "harness": harness_report(start)}


def problems(report: dict) -> list[str]:
    out = []
    for w in report["wikis"]:
        if not w["exists"] or not w.get("is_wiki"):
            if w["kind"] == "global":
                out.append(f"{w['path']}: no global wiki - run `shapa init --global`")
            elif w["exists"]:
                pass  # exists but isn't a wiki yet (e.g. an external/ bucket) - not a fault here
            else:
                out.append(f"{w['path']}: wiki path in scope does not exist")
            continue
        if w.get("format", 0) < registry.CURRENT_FORMAT:
            out.append(f"{w['path']}: format {w['format']}<{registry.CURRENT_FORMAT} - "
                       "run the shapa-upgrade skill (`shapa upgrade`)")
        bad = (w.get("memory") or {}).get("malformed", 0)
        if bad:
            out.append(f"{w['path']}: {bad} malformed memory-log line(s) - see `shapa upgrade --check`")

    git_hooks = report.get("git_hooks")
    if git_hooks:
        hand_off = git_hooks["shared"] or git_hooks["manager"]
        for name, h in git_hooks["hooks"].items():
            if h["wired"]:
                continue
            fix = (f"add the printed lines to {h['path']} (`shapa ledger git-hooks` reprints them)"
                   if hand_off else "run `shapa ledger git-hooks`")
            out.append(f"git {name} hook not wired - {fix}")

    harness = report.get("harness") or {}
    if not harness.get("connected"):
        out.append("no agent harness is connected - run: curl -fsSL "
                    "https://raw.githubusercontent.com/Roukh/shapa-llm/main/bootstrap.sh | sh")
    for kind, cmd in harness.get("broken", []):
        out.append(f"{kind} command does not resolve ({cmd!r}) - re-run the installer")
    return out


def render(report: dict, doctor: bool = False) -> str:
    r = report["recall"]
    lines = [f"shapa {report['shapa']}", f"recall mode: {r['description']}",
             f"mcp: {r['mcp_transport']}"]
    for w in report["wikis"]:
        tag = f" ({w['repo']})" if w.get("repo") else ""
        if not w["exists"]:
            lines.append(f"wiki[{w['kind']}{tag}]: {w['path']} - missing")
            continue
        m = w["memory"]
        idx = w["index"]
        idx_text = ("no index yet" if not idx["exists"] else
                    f"index {'fresh' if idx['fresh'] else 'stale (rebuilt on next read)'}, "
                    f"{idx['vectors']} vectors")
        lines.append(
            f"wiki[{w['kind']}{tag}]: {w['path']} - format {w['format']}/{w['current_format']}, "
            f"{w['notes']} notes, {m['active']} memories live ({m['superseded']} superseded, "
            f"{m['archived']} archived, {m['malformed']} malformed), log {m['log_bytes'] / 1024:.0f} KB, "
            f"{idx_text}")

    if report.get("repo_wiki_missing_here"):
        lines.append("no repo wiki here - `shapa init` creates one")

    git_hooks = report.get("git_hooks")
    if git_hooks:
        wired = [name for name, h in git_hooks["hooks"].items() if h["wired"]]
        lines.append(f"git hooks: {len(wired)}/{len(git_hooks['hooks'])} wired"
                      + (f" (manager: {git_hooks['manager']})" if git_hooks["manager"] else "")
                      + (" (shared hooksPath)" if git_hooks["shared"] else ""))

    h = report.get("harness") or {}
    wired_harnesses = [name for name, key in (
        ("claude hooks", "claude_hooks"), ("claude mcp", "claude_mcp"),
        ("codex mcp", "codex_mcp"), ("opencode mcp", "opencode_mcp")) if h.get(key)]
    lines.append("agent harness: " + (", ".join(wired_harnesses) if wired_harnesses else "none connected"))

    if doctor:
        probs = problems(report)
        lines.append("problems: " + ("none" if not probs else ""))
        lines.extend(f"  - {p}" for p in probs)
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, doctor: bool = False) -> None:
    parser = argparse.ArgumentParser(
        prog="shapa doctor" if doctor else "shapa status",
        description="Report the live retrieval mode (fused or bm25-only) and every wiki in scope.",
    )
    parser.add_argument("--cwd", default=None, help="Directory to resolve wikis from.")
    parser.add_argument("--json", action="store_true", help="Machine-readable report.")
    args = parser.parse_args(argv)
    report = gather(args.cwd)
    if args.json:
        if doctor:
            report["problems"] = problems(report)
        print(json.dumps(report, indent=2))
    else:
        print(render(report, doctor=doctor))
    sys.exit(1 if doctor and problems(report) else 0)


def doctor_main(argv: list[str] | None = None) -> None:
    main(argv, doctor=True)


if __name__ == "__main__":
    main()
