"""``shapa harness`` - install shapa's own system prompt into each agent harness.

Operator decision (2026-11-20): shapa adds its own system prompt to each harness,
built from one shared core (``assets/prompts/core.md``) plus one short adapter per
harness (``assets/prompts/adapters/<harness>.md``). The default mode is ``append``;
``--mode replace`` is the explicit opt-in per harness that swaps the harness's own
prompt for shapa's. Session data (precedence, focus, principles, the skill gate
state) is never baked into the file: the SessionStart hook delivers it.

The composed text lives at ``~/.shapa/prompts/<harness>.md`` (``$SHAPA_HOME``
overrides ``~/.shapa``), outside every repository. Replace mode wires it with the
mechanism each harness documents:

=============  ==============================================================
claude-code    settings key ``agent`` = ``shapa`` plus ``<config>/agents/shapa.md``
codex          top-level ``model_instructions_file`` in ``$CODEX_HOME/config.toml``
opencode       ``agents.<id>.system`` (``agent.<id>.prompt`` on a V1 config) for
               build, plan, general and explore, as ``{file:ABS}``
gemini         ``GEMINI_SYSTEM_MD``: an env line printed for the operator's shell
               rc, which this module never edits
cursor         unsupported (rules are additive only)
=============  ==============================================================

Append mode (the default) adds the same text to the harness's own prompt through a
managed block in the user-level instruction file (``CLAUDE.md``, Codex's
``AGENTS.md``, ``GEMINI.md``) or OpenCode's ``instructions`` list. Modes:
``append``, ``replace`` and ``default`` (nothing wired; the harness keeps its own
prompt and a later install leaves it so). A later install keeps the mode stored
for a harness.

Every write goes through one staging layer (:class:`FS`): all transforms read and
stage in memory, a dry run stops there, and a real run flushes atomically per
harness (prompt file first, an existence check, then configs, then state), with a
rollback when a write fails. Each touched shared config is backed up once to
``<file>.shapa-bak``. Uninstall restores the backup byte for byte while the file is
still as install left it, and otherwise removes exactly shapa's edit. Malformed
configs, symlinks, a value the operator already set and a config directory inside
a git work tree are refused with the reason, never overwritten.

CLI::

    shapa harness install   [--harness NAME]... [--mode append|replace|default]
                            [--dry-run] [--json] [--force] [--follow-symlinks]
                            [--allow-repo-config]
    shapa harness uninstall [--harness NAME]... [--dry-run] [--json]
    shapa harness status    [--harness NAME]... [--json]
    shapa harness watch     [--capture-claude] [--accept] [--json]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from shapa.config import CHARS_PER_TOKEN

try:  # POSIX only; elsewhere installs are not serialised
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

ASSETS = Path(__file__).resolve().parent / "assets" / "prompts"

HARNESSES = ("claude-code", "codex", "opencode", "gemini", "cursor")
ALIASES = {"claude": "claude-code", "claudecode": "claude-code", "gemini-cli": "gemini"}
SUPPORTED = ("claude-code", "codex", "opencode", "gemini")
MODES = ("append", "replace", "default")
DEFAULT_MODE = "append"
BINARIES = {"claude-code": "claude", "codex": "codex", "opencode": "opencode",
            "gemini": "gemini", "cursor": "cursor-agent"}
OPENCODE_AGENTS = ("build", "plan", "general", "explore")

AGENT_NAME = "shapa"
OWNED_MARK = "shapa-managed"
TOML_BEGIN = "# BEGIN shapa-harness"
TOML_END = "# END shapa-harness"
MD_BEGIN = "<!-- BEGIN shapa-harness -->"
MD_END = "<!-- END shapa-harness -->"
DISPLACED = "# shapa-displaced: "
BACKUP_SUFFIX = ".shapa-bak"
LOCK_WAIT = 15.0

#: What the harness keeps no matter what its system prompt says (research, 2026-11-20).
KEEPS = {
    "claude-code": [
        "tool descriptions (the Bash git protocol, Edit and Write rules)",
        "the environment block (cwd, platform, shell, model, deferred tools)",
        "CLAUDE.md and AGENTS.md reminders and the attribution reminder",
        "the one-line identity header and billing header",
        "non-fork subagents (they run their own prompt)",
        "a project or local `agent` setting, or `--agent`, outranks the user setting",
    ],
    "codex": [
        "permission, sandbox and approval text",
        "collaboration-mode, apps, plugins and skills catalogs",
        "the <environment_context> block",
        "AGENTS.md fragments (loaded separately)",
        "per-slug model tuning is lost with the replacement",
    ],
    "opencode": [
        "the instructions part (date, environment, AGENTS.md, skills)",
        "tool descriptions",
        "title, summary and compaction agents (their own prompts)",
        "per-model prompt tuning is switched off for the replaced agents",
    ],
    "gemini": [
        "the GEMINI.md memory block appended after the prompt",
        "tool schemas and the environment context",
        "plan, YOLO and sandbox sections of the default prompt are dropped",
    ],
    "cursor": ["everything: rules are additive, the default prompt cannot be replaced"],
}

PREPEND_SCOPE_SENTENCE = "These instructions are the whole system prompt."
APPEND_SCOPE_SENTENCE = "These instructions are added to the harness's own."
_TEMPLATE_RE = re.compile(r"<!-- shapa:template -->\n(.*?)<!-- /shapa:template -->\n", re.S)


class HarnessError(Exception):
    """A refusal: the reason is the message, ``hint`` says how to proceed."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------- locations

def shapa_home() -> Path:
    env = os.environ.get("SHAPA_HOME")
    return Path(env).expanduser() if env else Path.home() / ".shapa"


def prompts_dir() -> Path:
    return shapa_home() / "prompts"


def prompt_path(harness: str) -> Path:
    return prompts_dir() / f"{harness}.md"


def state_path() -> Path:
    return shapa_home() / "harness.json"


def config_root(harness: str) -> Path:
    env = os.environ
    if harness == "claude-code":
        return Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()
    if harness == "codex":
        return Path(env.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    if harness == "opencode":
        return Path(env.get("XDG_CONFIG_HOME") or (Path.home() / ".config")).expanduser() / "opencode"
    if harness == "gemini":
        return Path.home() / ".gemini"
    return Path.home() / ".cursor"


def detected(harness: str) -> str | None:
    """Where the harness binary is, or the config dir when only that exists."""
    found = shutil.which(BINARIES[harness])
    if found:
        return found
    root = config_root(harness)
    return str(root) if root.is_dir() else None


def normalize(name: str) -> str:
    n = ALIASES.get(name.strip().lower(), name.strip().lower())
    if n not in HARNESSES:
        raise HarnessError(f"unknown harness {name!r}",
                           f"one of: {', '.join(HARNESSES)}, all")
    return n


def _home_boundary() -> Path:
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return Path("/")


def guard_dir(directory: Path, label: str, allow_repo: bool) -> None:
    """Refuse a directory inside a git work tree (below the home directory): a config
    written there would land in a repository and be committed or published."""
    if allow_repo:
        return
    try:
        here = directory.resolve()
    except (OSError, RuntimeError):
        return
    home = _home_boundary()
    for d in (here, *here.parents):
        if d == home or d == d.parent:
            return
        if (d / ".git").exists():
            raise HarnessError(
                f"{label} {directory} is inside the git work tree {d}; nothing is written there",
                "point the variable at a directory outside any repository, "
                "or pass --allow-repo-config if you mean it")


# ----------------------------------------------------------------------- compose

def _asset(rel: str) -> str:
    path = ASSETS / rel
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessError(f"bundled prompt asset {rel} is unreadable ({exc})",
                           "reinstall shapa") from exc
    if not text.strip():
        raise HarnessError(f"bundled prompt asset {rel} is empty", "reinstall shapa")
    return text


def render(harness: str, mode: str = DEFAULT_MODE) -> str:
    """The prompt for *harness*: the shared core, then its adapter."""
    core = _asset("core.md").rstrip("\n")
    adapter = _asset(f"adapters/{harness}.md").strip("\n")
    if mode == "append":
        core = core.replace(PREPEND_SCOPE_SENTENCE, APPEND_SCOPE_SENTENCE)
        adapter = _TEMPLATE_RE.sub("", adapter + "\n").strip("\n")
    else:
        adapter = _TEMPLATE_RE.sub(lambda m: m.group(1), adapter + "\n").strip("\n")
    return f"{core}\n\n{adapter}\n"


def tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def sha(data: bytes | str | None) -> str | None:
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------- staged IO

class FS:
    """Reads and stages writes in memory; ``flush`` applies them (never in a dry run)."""

    def __init__(self, dry: bool = False, follow_symlinks: bool = False):
        self.dry = dry
        self.follow = follow_symlinks
        self.orig: dict[Path, bytes | None] = {}
        self.staged: dict[Path, bytes | None] = {}
        self.flags: dict[Path, dict] = {}

    def real(self, path: Path) -> Path:
        path = Path(path)
        if path.is_symlink():
            if not self.follow:
                raise HarnessError(f"{path} is a symlink; shapa will not write through it",
                                   "pass --follow-symlinks to write to its target")
            target = path.resolve()
            if not target.exists() and not target.parent.is_dir():
                raise HarnessError(f"{path} is a dangling symlink")
            return target
        if path.exists() and not path.is_file():
            raise HarnessError(f"{path} exists and is not a regular file")
        return path

    def read(self, path: Path) -> bytes | None:
        p = self.real(path)
        if p in self.staged:
            return self.staged[p]
        if p not in self.orig:
            try:
                self.orig[p] = p.read_bytes()
            except FileNotFoundError:
                self.orig[p] = None
            except OSError as exc:
                raise HarnessError(f"{p} is unreadable ({exc})") from exc
        return self.orig[p]

    def read_text(self, path: Path) -> str | None:
        data = self.read(path)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessError(f"{path} is not valid UTF-8 ({exc})") from exc

    def stage(self, path: Path, data: bytes | str | None, *, backup: bool = False, tidy: bool = False) -> None:
        p = self.real(path)
        self.read(p)  # records the original
        if isinstance(data, str):
            data = data.encode("utf-8")
        if backup:
            # What the backup must hold: the file as it stands before this edit, minus anything shapa itself
            # wired (a reinstall stages the unwire first). It is refreshed on a reinstall, so an uninstall never
            # restores a backup older than the operator's later edits.
            base = self.staged.get(p, self.orig.get(p))
            if base is not None:
                self.flags.setdefault(p, {}).setdefault("backup_src", base)
            self.flags.setdefault(p, {})["backup"] = True
        self.staged[p] = data
        if tidy:  # drop an existing backup the file has returned to
            self.flags.setdefault(p, {})["tidy"] = True

    def changed(self) -> list[tuple[str, Path]]:
        out = []
        for p, data in self.staged.items():
            if data == self.orig.get(p):
                continue
            verb = "remove" if data is None else ("create" if self.orig.get(p) is None else "update")
            out.append((verb, p))
        return out

    def backups_due(self) -> list[Path]:
        out = []
        for p, flags in self.flags.items():
            src = flags.get("backup_src")
            if flags.get("backup") and src is not None and _backup_stale(p, src):
                out.append(_bak(p))
        return out

    def flush(self, only: set[Path] | None = None) -> None:
        if self.dry:
            return
        done: list[tuple[Path, bytes | None]] = []
        made: list[Path] = []
        try:
            for p, data in list(self.staged.items()):
                if only is not None and p not in only:
                    continue
                before = self.orig.get(p)
                src = self.flags.get(p, {}).get("backup_src")
                if self.flags.get(p, {}).get("backup") and src is not None and _backup_stale(p, src):
                    existed = _bak(p).exists()
                    _atomic_write(_bak(p), src, like=p)
                    if not existed:
                        made.append(_bak(p))
                if data == before:
                    continue
                if data is None:
                    with contextlib.suppress(FileNotFoundError):
                        p.unlink()
                else:
                    _atomic_write(p, data, like=p if before is not None else None)
                done.append((p, before))
                self.orig[p] = data
        except Exception:
            for p, before in reversed(done):  # undo what this flush wrote
                with contextlib.suppress(OSError):
                    if before is None:
                        p.unlink()
                    else:
                        _atomic_write(p, before, like=p)
                self.orig[p] = before
            for bak in made:  # a backup of a file that was never changed is noise
                with contextlib.suppress(OSError):
                    bak.unlink()
            raise
        for p, flags in self.flags.items():  # a backup equal to the file is redundant
            if only is not None and p not in only:
                continue
            bak = _bak(p)
            if (flags.get("backup") or flags.get("tidy")) and bak.exists():
                with contextlib.suppress(OSError):
                    if self.orig.get(p) is not None and bak.read_bytes() == self.orig[p]:
                        bak.unlink()


def _bak(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def _backup_stale(path: Path, src: bytes) -> bool:
    """Whether the backup of *path* is missing or differs from what it should hold."""
    bak = _bak(path)
    try:
        return not bak.exists() or bak.read_bytes() != src
    except OSError:
        return True


def _atomic_write(path: Path, data: bytes, like: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o644
    if like is not None:
        with contextlib.suppress(OSError):
            mode = like.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _lock(dry: bool):
    """One harness command at a time: concurrent installs queue instead of interleaving."""
    if dry or fcntl is None:
        yield
        return
    home = shapa_home()
    home.mkdir(parents=True, exist_ok=True)
    fh = open(home / "harness.lock", "a+")  # noqa: SIM115
    deadline = time.monotonic() + LOCK_WAIT
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise HarnessError("another `shapa harness` command holds the lock",
                                       f"retry, or remove {home / 'harness.lock'} if none is running") from None
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ------------------------------------------------------------------------- state

def load_state(fs: FS | None = None) -> dict:
    path = state_path()
    try:
        raw = (fs.read_text(path) if fs else path.read_text(encoding="utf-8"))
    except (OSError, HarnessError):
        raw = None
    if not raw:
        return {"version": 1, "harnesses": {}}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"version": 1, "harnesses": {}, "corrupt": True}
    if not isinstance(data, dict) or not isinstance(data.get("harnesses"), dict):
        return {"version": 1, "harnesses": {}}
    return data


# ----------------------------------------------------------------- text transforms

def _json_load(text: str | None, label: Path) -> tuple[dict, int | str, bool]:
    """(data, indent, ends-with-newline). An empty file is an empty object."""
    if text is None or not text.strip():
        return {}, 2, True
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise HarnessError(
            f"{label} is not valid JSON ({exc}); shapa leaves it alone",
            "fix or remove it (a comment-bearing .jsonc is not supported), then run `shapa harness install`") from exc
    if not isinstance(data, dict):
        raise HarnessError(f"{label} holds JSON that is not an object",
                           "fix it so it is one JSON object (or remove it), then run `shapa harness install`")
    indent: int | str = 2
    m = re.search(r"(?m)^([ \t]+)\S", text)
    if m:
        indent = m.group(1) if "\t" in m.group(1) else len(m.group(1))
    return data, indent, text.endswith("\n")


def _json_dump(data: dict, indent: int | str, newline: bool) -> str:
    return json.dumps(data, indent=indent, ensure_ascii=False) + ("\n" if newline else "")


def _toml_load(text: str | None, label: Path) -> dict:
    if text is None or not text.strip():
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HarnessError(
            f"{label} is not valid TOML ({exc}); shapa leaves it alone",
            "fix or remove the broken line, then run `shapa harness install`") from exc


_TOML_BLOCK = re.compile(rf"(?ms)^{re.escape(TOML_BEGIN)}[^\n]*\n.*?^{re.escape(TOML_END)}\n(?:\n)?")
_MD_BLOCK = re.compile(rf"(?ms)\n?^{re.escape(MD_BEGIN)}\n.*?^{re.escape(MD_END)}\n")


def toml_string(value: str) -> str:
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise HarnessError("a path with control characters cannot be written to TOML")
    return json.dumps(value, ensure_ascii=False)


def toml_wire(text: str | None, label: Path, prompt: Path, force: bool) -> tuple[str, dict]:
    """Insert (or refresh) the managed ``model_instructions_file`` block at the top of
    config.toml. A value the operator set stays untouched unless *force*, which
    comments it out in place (``# shapa-displaced: ...``) so uninstall restores it."""
    text = text or ""
    cleaned = toml_unwire(text)
    existing = _toml_load(cleaned, label).get("model_instructions_file")
    rec: dict = {}
    if existing is not None:
        if not force:
            raise HarnessError(
                f"{label} already sets model_instructions_file = {existing!r}; shapa keeps it",
                "re-run with --force to take it over (the line is kept as a comment and "
                "restored by uninstall), or use --mode append")
        lines = cleaned.split("\n")
        header = next((i for i, ln in enumerate(lines) if re.match(r"\s*\[", ln)), len(lines))
        hit = next((i for i in range(header)
                    if re.match(r"\s*model_instructions_file\s*=\s*(\"[^\"\n]*\"|'[^'\n]*')\s*(#.*)?$", lines[i])), None)
        if hit is None:
            raise HarnessError(f"{label}: model_instructions_file is set in a form shapa cannot "
                               "displace safely", "move it aside by hand, then re-run")
        lines[hit] = DISPLACED + lines[hit]
        cleaned = "\n".join(lines)
        rec["displaced"] = True
    block = (f"{TOML_BEGIN} (managed by `shapa harness`; undo with `shapa harness uninstall`)\n"
             f"model_instructions_file = {toml_string(str(prompt))}\n{TOML_END}\n")
    new = block + ("\n" + cleaned if cleaned.strip() else "")
    got = _toml_load(new, label).get("model_instructions_file")
    if got != str(prompt):
        raise HarnessError(f"{label}: could not place model_instructions_file safely")
    return new, rec


def toml_unwire(text: str) -> str:
    """Remove the managed block and bring back a line it displaced."""
    out = _TOML_BLOCK.sub("", text, count=1)
    return re.sub(rf"(?m)^{re.escape(DISPLACED)}", "", out)


def md_wire(text: str | None, body: str) -> str:
    text = text or ""
    block = f"{MD_BEGIN}\n{body.rstrip(chr(10))}\n{MD_END}\n"
    if MD_BEGIN in text:
        return _MD_BLOCK.sub(lambda m: ("\n" if m.group(0).startswith("\n") else "") + block, text, count=1)
    if not text:
        return block
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block


def md_unwire(text: str) -> str:
    return _MD_BLOCK.sub("", text, count=1)


def has_md_block(text: str | None) -> bool:
    return bool(text) and MD_BEGIN in text and MD_END in text


def has_toml_block(text: str | None) -> bool:
    return bool(text) and bool(_TOML_BLOCK.search(text))


def owned_agent_file(body: str) -> str:
    return (f"---\nname: {AGENT_NAME}\n"
            f"description: {OWNED_MARK} system prompt that replaces the default (undo: shapa harness uninstall)\n"
            f"---\n{body}")


def is_owned(text: str | None) -> bool:
    """True for an agent file shapa wrote: its frontmatter carries the marker."""
    if not text or not text.startswith("---"):
        return False
    parts = text.split("---", 2)
    return len(parts) == 3 and OWNED_MARK in parts[1]


def opencode_file(root: Path) -> Path:
    plain, comments = root / "opencode.json", root / "opencode.jsonc"
    return comments if comments.is_file() and not plain.exists() else plain


def opencode_schema(data: dict) -> str:
    env = os.environ.get("SHAPA_OPENCODE_SCHEMA", "").lower()
    if env in ("v1", "v2"):
        return env
    if isinstance(data.get("agents"), dict):
        return "v2"
    if isinstance(data.get("agent"), dict):
        return "v1"
    binary = shutil.which("opencode")
    if binary:
        try:
            out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                 timeout=5, check=False, stdin=subprocess.DEVNULL).stdout
            m = re.search(r"(\d+)\.\d+", out)
            if m and int(m.group(1)) < 2:
                return "v1"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "v2"


def _file_ref(path: Path) -> str:
    if "}" in str(path) or "\n" in str(path):
        raise HarnessError(f"{path} cannot be written as an OpenCode {{file:...}} reference")
    return "{file:" + str(path) + "}"


# --------------------------------------------------------------- per-harness wiring

@dataclass
class Opts:
    mode: str = DEFAULT_MODE
    force: bool = False
    follow_symlinks: bool = False
    allow_repo: bool = False


@dataclass
class Result:
    harness: str
    action: str = "skipped"      # installed | updated | unchanged | removed | refused | unsupported | skipped
    mode: str = ""
    changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    keeps: list[str] = field(default_factory=list)
    reason: str = ""
    hint: str = ""
    env_line: str = ""
    prompt_tokens: int = 0

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in ("", [], 0) or k in ("harness", "action")}


def _file_record(fs: FS, path: Path, kind: str, **extra) -> dict:
    p = fs.real(path)
    rec = {"path": str(path), "kind": kind, "existed": fs.orig.get(p) is not None,
           "post_sha256": sha(fs.staged.get(p, fs.orig.get(p)))}
    rec.update(extra)
    return rec


def _old_record(rec: dict | None, path: Path) -> dict | None:
    for f in (rec or {}).get("files", []):
        if f.get("path") == str(path):
            return f
    return None


def wire(harness: str, fs: FS, opts: Opts, old: dict | None) -> list[dict]:
    root = config_root(harness)
    prompt = prompt_path(harness)
    force = opts.force or bool((old or {}).get("forced"))
    body = render(harness, opts.mode)

    def carried(path: Path, rec: dict) -> dict:
        prev = _old_record(old, path)
        if prev:  # the file was ours before this run: keep what the first install saw
            rec["existed"] = prev.get("existed", rec["existed"])
        return rec

    if harness == "claude-code":
        if opts.mode == "replace":
            agent_file = root / "agents" / f"{AGENT_NAME}.md"
            existing = fs.read_text(agent_file)
            displaced_file = False
            if existing is not None and not is_owned(existing):
                if not force:
                    raise HarnessError(f"{agent_file} exists and is not shapa's",
                                       "move it aside, or re-run with --force (it is backed up and restored by uninstall)")
                displaced_file = True
            fs.stage(agent_file, owned_agent_file(body), backup=displaced_file)
            settings = root / "settings.json"
            data, indent, nl = _json_load(fs.read_text(settings), settings)
            rec_extra: dict = {}
            current = data.get("agent")
            if current not in (None, AGENT_NAME):
                if not force:
                    raise HarnessError(f"{settings} already sets agent = {current!r}; shapa keeps it",
                                       "re-run with --force to replace it (kept in the .shapa-bak backup and "
                                       "restored by uninstall), or use --mode append")
                rec_extra["displaced"] = {"agent": current}
            elif current is None and "agent" not in data:
                rec_extra["created_key"] = True
            data["agent"] = AGENT_NAME
            fs.stage(settings, _json_dump(data, indent, nl), backup=True)
            return [carried(agent_file, _file_record(fs, agent_file, "owned", displaced_file=displaced_file)),
                    carried(settings, _file_record(fs, settings, "json", **rec_extra))]
        target = root / "CLAUDE.md"
        fs.stage(target, md_wire(fs.read_text(target), body), backup=True)
        return [carried(target, _file_record(fs, target, "mdblock"))]

    if harness == "codex":
        if opts.mode == "replace":
            cfg = root / "config.toml"
            text, rec_extra = toml_wire(fs.read_text(cfg), cfg, prompt, force)
            fs.stage(cfg, text, backup=True)
            return [carried(cfg, _file_record(fs, cfg, "toml", **rec_extra))]
        target = root / "AGENTS.md"
        fs.stage(target, md_wire(fs.read_text(target), body), backup=True)
        return [carried(target, _file_record(fs, target, "mdblock"))]

    if harness == "opencode":
        cfg = opencode_file(root)
        data, indent, nl = _json_load(fs.read_text(cfg), cfg)
        rec_extra = {}
        if opts.mode == "replace":
            schema = opencode_schema(data)
            container, key = ("agents", "system") if schema == "v2" else ("agent", "prompt")
            ref = _file_ref(prompt)
            created_container = container not in data
            if not isinstance(data.get(container, {}), dict):
                raise HarnessError(f"{cfg}: {container!r} is not an object")
            agents = data.setdefault(container, {})
            created_agents, displaced = [], {}
            for aid in OPENCODE_AGENTS:
                if not isinstance(agents.get(aid, {}), dict):
                    raise HarnessError(f"{cfg}: {container}.{aid} is not an object")
                entry = agents.get(aid)
                if entry is None:
                    created_agents.append(aid)
                    entry = agents.setdefault(aid, {})
                cur = entry.get(key)
                if cur not in (None, "", ref):
                    if not force:
                        raise HarnessError(f"{cfg} already sets {container}.{aid}.{key}; shapa keeps it",
                                           "re-run with --force to replace it (kept in the .shapa-bak backup "
                                           "and restored by uninstall), or use --mode append")
                    displaced[aid] = cur
                entry[key] = ref
            rec_extra = {"container": container, "key": key, "ref": ref, "ids": list(OPENCODE_AGENTS),
                         "created_container": created_container, "created_agents": created_agents,
                         "displaced": displaced}
        else:
            ref = str(prompt)
            created = "instructions" not in data
            ins = data.setdefault("instructions", [])
            if not isinstance(ins, list):
                raise HarnessError(f"{cfg}: 'instructions' is not a list")
            if ref not in ins:
                ins.append(ref)
            rec_extra = {"instructions": ref, "created_instructions": created}
        fs.stage(cfg, _json_dump(data, indent, nl), backup=True)
        return [carried(cfg, _file_record(fs, cfg, "json", **rec_extra))]

    if harness == "gemini":
        if opts.mode == "replace":
            return []  # the prompt file plus the env line the operator adds
        target = root / "GEMINI.md"
        fs.stage(target, md_wire(fs.read_text(target), body), backup=True)
        return [carried(target, _file_record(fs, target, "mdblock"))]

    raise HarnessError(f"{harness} has no supported wiring")


def unwire(harness: str, fs: FS, rec: dict | None) -> list[str]:
    """Stage the removal of everything shapa wired for *harness* (detected from the
    files themselves, so a lost state file does not strand an install)."""
    notes: list[str] = []
    root = config_root(harness)
    prompt = prompt_path(harness)
    fs.stage(prompt, None)
    files = list((rec or {}).get("files", []))
    paths = {f["path"] for f in files}
    guesses = []
    if harness == "claude-code":
        guesses = [(root / "agents" / f"{AGENT_NAME}.md", "owned"), (root / "settings.json", "json"),
                   (root / "CLAUDE.md", "mdblock")]
    elif harness == "codex":
        guesses = [(root / "config.toml", "toml"), (root / "AGENTS.md", "mdblock")]
    elif harness == "opencode":
        guesses = [(opencode_file(root), "json")]
    elif harness == "gemini":
        guesses = [(root / "GEMINI.md", "mdblock")]
    for path, kind in guesses:
        if str(path) not in paths:
            files.append({"path": str(path), "kind": kind, "existed": True, "post_sha256": None, "guess": True})
    for f in files:
        path = Path(f["path"])
        kind = f["kind"]
        try:
            data = fs.read(path)
        except HarnessError as exc:
            notes.append(f"{path}: {exc}")
            continue
        if data is None:
            continue
        bak = _bak(fs.real(path))
        if (not f.get("guess") and f.get("post_sha256") and sha(data) == f["post_sha256"] and bak.is_file()
                and kind != "owned"):
            fs.stage(path, bak.read_bytes(), tidy=True)  # untouched since install: exact restore
            continue
        text = data.decode("utf-8", "replace")
        if kind == "owned":
            if is_owned(text):
                if f.get("displaced_file") and bak.is_file():
                    fs.stage(path, bak.read_bytes(), tidy=True)
                else:
                    fs.stage(path, None)
            continue
        if kind == "mdblock":
            if has_md_block(text):
                new = md_unwire(text)
                fs.stage(path, None if (not new.strip() and not f.get("existed", True)) else new, tidy=True)
            continue
        if kind == "toml":
            if has_toml_block(text) or DISPLACED in text:
                new = toml_unwire(text)
                fs.stage(path, None if (not new.strip() and not f.get("existed", True)) else new, tidy=True)
            continue
        if kind == "json":
            try:
                obj, indent, nl = _json_load(text, path)
            except HarnessError as exc:
                notes.append(str(exc))
                continue
            changed = _json_unwire(obj, f, harness)
            if changed:
                empty = not obj and not f.get("existed", True)
                fs.stage(path, None if empty else _json_dump(obj, indent, nl), tidy=True)
    return notes


def _json_unwire(obj: dict, rec: dict, harness: str) -> bool:
    changed = False
    if harness == "claude-code":
        if obj.get("agent") == AGENT_NAME:
            displaced = (rec.get("displaced") or {}).get("agent")
            if displaced is not None:
                obj["agent"] = displaced
            else:
                del obj["agent"]
            changed = True
        return changed
    if harness == "opencode":
        container = rec.get("container")
        containers = [container] if container else ["agents", "agent"]
        for cont in containers:
            agents = obj.get(cont)
            if not isinstance(agents, dict):
                continue
            key = rec.get("key") or ("system" if cont == "agents" else "prompt")
            for aid in OPENCODE_AGENTS:
                entry = agents.get(aid)
                if isinstance(entry, dict) and isinstance(entry.get(key), str) and entry[key].startswith("{file:") \
                        and (str(prompt_path("opencode")) in entry[key]):
                    prev = (rec.get("displaced") or {}).get(aid)
                    if prev is not None:
                        entry[key] = prev
                    else:
                        del entry[key]
                    changed = True
                    if not entry and aid in (rec.get("created_agents") or []):
                        del agents[aid]
            if changed and not agents and rec.get("created_container"):
                del obj[cont]
        ins = obj.get("instructions")
        if isinstance(ins, list) and str(prompt_path("opencode")) in ins:
            ins.remove(str(prompt_path("opencode")))
            changed = True
            if not ins and rec.get("created_instructions"):
                del obj["instructions"]
    return changed


# ---------------------------------------------------------------------- operations

def _env_line(harness: str) -> str:
    if harness == "gemini":
        return f"export GEMINI_SYSTEM_MD={shlex.quote(str(prompt_path('gemini')))}"
    return ""


def _targets(names: list[str] | None, installing: bool) -> list[str]:
    if names:
        out: list[str] = []
        for raw in names:
            for part in raw.split(","):
                if not part.strip():
                    continue
                if part.strip().lower() == "all":
                    out += [h for h in HARNESSES if h in SUPPORTED and detected(h)]
                else:
                    out.append(normalize(part))
        return list(dict.fromkeys(out))
    if installing:
        return [h for h in SUPPORTED if detected(h)]
    state = load_state()
    return [h for h in HARNESSES if h in state["harnesses"]] or [h for h in SUPPORTED if detected(h)]


def install(harnesses: list[str] | None = None, mode: str | None = None, *, dry_run: bool = False,
            force: bool = False, follow_symlinks: bool = False, allow_repo_config: bool = False) -> list[Result]:
    if mode is not None and mode not in MODES:
        raise HarnessError(f"unknown mode {mode!r}", f"one of: {', '.join(MODES)}")
    targets = _targets(harnesses, installing=True)
    results: list[Result] = []
    guard_dir(shapa_home(), "the shapa home", allow_repo_config)
    with _lock(dry_run):
        state_fs = FS(dry_run, follow_symlinks)
        state = load_state(state_fs)
        if state.get("corrupt"):
            raise HarnessError(f"{state_path()} is not valid JSON",
                               "remove it (the wired configs stay as they are), then re-run")
        for h in targets:
            results.append(_install_one(h, mode, state, dry_run, force, follow_symlinks, allow_repo_config))
        if not dry_run and any(r.action in ("installed", "updated", "removed", "unchanged") for r in results):
            sfs = FS(False, False)
            sfs.stage(state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")
            sfs.flush()
    return results


def _install_one(h: str, mode: str | None, state: dict, dry: bool, force: bool, follow: bool,
                 allow_repo: bool) -> Result:
    res = Result(harness=h, keeps=KEEPS.get(h, []))
    if h not in SUPPORTED:
        res.action = "unsupported"
        res.notes.append("Cursor rules are additive only: no way to replace its system prompt. "
                         "shapa still reaches Cursor through hooks and MCP; nothing is written here.")
        return res
    old = state["harnesses"].get(h)
    mode = mode or (old or {}).get("mode") or DEFAULT_MODE
    res.mode = mode
    opts = Opts(mode=mode, force=force, follow_symlinks=follow, allow_repo=allow_repo)
    fs = FS(dry, follow)
    try:
        guard_dir(config_root(h), f"the {h} config directory", allow_repo)
        guard_dir(prompts_dir(), "the prompts directory", allow_repo)
        notes = unwire(h, fs, old)
        res.notes += notes
        if mode == "default":
            record = {"mode": "default", "files": [], "prompt": None}
            res.notes.append("mode default: the harness keeps its own system prompt; `shapa harness "
                             "install` will leave it that way until you pick another mode")
        else:
            body = render(h, mode)
            if not body.strip():
                raise HarnessError("the composed prompt is empty")
            res.prompt_tokens = tokens(body)
            fs.stage(prompt_path(h), body)
            files = wire(h, fs, opts, old)
            record = {"mode": mode, "forced": bool(force or (old or {}).get("forced")),
                      "prompt": {"path": str(prompt_path(h)), "sha256": sha(body)}, "files": files,
                      "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if h == "gemini" and mode == "replace":
                res.env_line = _env_line(h)
                res.notes.append("add the env line below to your shell rc (shapa never edits rc files); "
                                 "Gemini CLI reads it on every prompt build")
        changes = fs.changed()
        for verb, path in changes:
            res.changes.append(f"{verb} {path}")
        for bak in fs.backups_due():
            res.changes.append(f"backup {bak}")
        if not dry:
            prompt_real = {fs.real(prompt_path(h))}
            if mode != "default":
                fs.flush(only=prompt_real)
                p = prompt_path(h)
                if not p.is_file() or p.stat().st_size == 0:
                    raise HarnessError(f"{p} is missing or empty after writing; the harness would fall back "
                                       "to its default prompt silently, so nothing is wired")
            fs.flush()
        prior = old is not None and old.get("mode") == mode
        if not changes and prior:
            res.action = "unchanged"
        else:
            res.action = "updated" if old else "installed"
        if mode == "default" and not changes:
            res.action = "unchanged" if old and old.get("mode") == "default" else "installed"
        state["harnesses"][h] = record
        if not dry:
            res.notes.append("takes effect in new sessions; a running session keeps its old prompt")
    except HarnessError as exc:
        res.action, res.reason, res.hint = "refused", str(exc), exc.hint
    except OSError as exc:
        res.action, res.reason = "refused", f"{exc.__class__.__name__}: {exc}"
    return res


def uninstall(harnesses: list[str] | None = None, *, dry_run: bool = False, follow_symlinks: bool = False,
              allow_repo_config: bool = False) -> list[Result]:
    targets = _targets(harnesses, installing=False)
    results: list[Result] = []
    guard_dir(shapa_home(), "the shapa home", allow_repo_config)
    with _lock(dry_run):
        state = load_state()
        for h in targets:
            res = Result(harness=h, mode=(state["harnesses"].get(h) or {}).get("mode", ""))
            if h not in SUPPORTED:
                res.action = "unsupported"
                results.append(res)
                continue
            fs = FS(dry_run, follow_symlinks)
            try:
                guard_dir(config_root(h), f"the {h} config directory", allow_repo_config)
                res.notes += unwire(h, fs, state["harnesses"].get(h))
                for verb, path in fs.changed():
                    res.changes.append(f"{verb} {path}")
                fs.flush()
                res.action = "removed" if res.changes or h in state["harnesses"] else "unchanged"
                if not dry_run:
                    state["harnesses"].pop(h, None)
            except HarnessError as exc:
                res.action, res.reason, res.hint = "refused", str(exc), exc.hint
            except OSError as exc:
                res.action, res.reason = "refused", f"{exc.__class__.__name__}: {exc}"
            results.append(res)
        if not dry_run and any(r.action in ("removed", "unchanged") for r in results):
            sfs = FS(False, False)
            if state["harnesses"]:
                sfs.stage(state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")
            else:
                sfs.stage(state_path(), None)
            sfs.flush()
    return results


# -------------------------------------------------------------------------- status

def _project_override(start: str | os.PathLike | None) -> str | None:
    base = Path(start).resolve() if start else Path.cwd()
    for name in (".claude/settings.local.json", ".claude/settings.json"):
        try:
            data = json.loads((base / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("agent") not in (None, AGENT_NAME):
            return f"{base / name} sets agent = {data['agent']!r}, which outranks shapa's user setting here"
    return None


def harness_status(h: str, state: dict, start=None) -> dict:
    out: dict = {"harness": h, "supported": h in SUPPORTED, "detected": detected(h),
                 "keeps": KEEPS.get(h, []), "problems": [], "notes": []}
    rec = state["harnesses"].get(h)
    out["mode"] = (rec or {}).get("mode", "none")
    if h not in SUPPORTED:
        out["wired"] = False
        out["notes"].append("unsupported: rules are additive only, the default prompt cannot be replaced")
        return out
    out["config"] = str(config_root(h))
    pp = prompt_path(h)
    pinfo = {"path": str(pp), "exists": pp.is_file()}
    wired = False
    mode = out["mode"]
    root = config_root(h)
    fs = FS(True, True)
    try:
        if pinfo["exists"]:
            text = pp.read_text(encoding="utf-8")
            pinfo.update({"bytes": len(text.encode()), "tokens": tokens(text),
                          "current": sha(text) == sha(render(h, mode if mode in ("replace", "append") else "replace"))})
        if h == "claude-code":
            data, _, _ = _json_load(fs.read_text(root / "settings.json"), root / "settings.json")
            agent_file = fs.read_text(root / "agents" / f"{AGENT_NAME}.md")
            if mode == "replace":
                wired = data.get("agent") == AGENT_NAME and is_owned(agent_file)
                if agent_file and pinfo["exists"] and is_owned(agent_file) and not agent_file.endswith(
                        pp.read_text(encoding="utf-8")):
                    out["problems"].append("agents/shapa.md differs from the composed prompt: run `shapa harness install`")
            elif mode == "append":
                wired = has_md_block(fs.read_text(root / "CLAUDE.md"))
            override = _project_override(start)
            if override:
                out["notes"].append(override)
        elif h == "codex":
            if mode == "replace":
                got = _toml_load(fs.read_text(root / "config.toml"), root / "config.toml").get("model_instructions_file")
                wired = got == str(pp)
                if got not in (None, str(pp)):
                    out["problems"].append(f"config.toml model_instructions_file now points at {got!r}, not shapa's file")
            elif mode == "append":
                wired = has_md_block(fs.read_text(root / "AGENTS.md"))
        elif h == "opencode":
            cfg = opencode_file(root)
            data, _, _ = _json_load(fs.read_text(cfg), cfg)
            if mode == "replace":
                cont = "agents" if isinstance(data.get("agents"), dict) else "agent"
                key = "system" if cont == "agents" else "prompt"
                refs = [(data.get(cont) or {}).get(a, {}).get(key) if isinstance((data.get(cont) or {}).get(a), dict)
                        else None for a in OPENCODE_AGENTS]
                wired = all(r == "{file:" + str(pp) + "}" for r in refs)
                if any(isinstance(r, str) and r.startswith("{file:") and r != "{file:" + str(pp) + "}" for r in refs):
                    out["problems"].append("an OpenCode agent points at a different file than shapa's")
            elif mode == "append":
                wired = str(pp) in (data.get("instructions") or [])
        elif h == "gemini":
            if mode == "replace":
                wired = pinfo["exists"]
                active = os.environ.get("GEMINI_SYSTEM_MD")
                out["env_line"] = _env_line(h)
                out["env_active"] = bool(active and Path(active).expanduser() == pp)
                if not out["env_active"]:
                    out["notes"].append("GEMINI_SYSTEM_MD is not set to shapa's file in this shell; add: "
                                        + out["env_line"])
            elif mode == "append":
                wired = has_md_block(fs.read_text(root / "GEMINI.md"))
    except HarnessError as exc:
        out["problems"].append(str(exc))
    out["prompt"] = pinfo
    out["wired"] = wired
    if mode in ("replace", "append"):
        if not pinfo["exists"]:
            out["problems"].append(f"{pp} is missing"
                                   + ("; OpenCode would silently fall back to its default prompt" if h == "opencode" else ""))
        elif pinfo.get("bytes", 1) == 0:
            out["problems"].append(f"{pp} is empty")
        elif not pinfo.get("current", True):
            out["problems"].append("the prompt file is out of date with this shapa: run `shapa harness install`")
        if not wired and pinfo["exists"] and not out["problems"]:
            out["problems"].append(f"state says {mode} but the {h} config no longer holds it: "
                                   "run `shapa harness install`")
    return out


def status(harnesses: list[str] | None = None, start=None) -> dict:
    state = load_state()
    names = _targets(harnesses, installing=False) if harnesses else list(HARNESSES)
    return {"state_file": str(state_path()), "prompts_dir": str(prompts_dir()),
            "harnesses": [harness_status(h, state, start) for h in names]}


def summary(start=None) -> list[dict]:
    """The compact per-harness view `shapa status` and `shapa doctor` print."""
    out = []
    state = load_state()
    for h in SUPPORTED:
        try:
            s = harness_status(h, state, start)
        except Exception:
            continue
        if s["mode"] == "none" and not s["detected"]:
            continue
        out.append({"harness": h, "mode": s["mode"], "wired": s["wired"], "detected": bool(s["detected"]),
                    "problems": s["problems"] if s["mode"] != "none" else []})
    return out


# ----------------------------------------------------------------------------- CLI

def _render_results(results: list[Result], verb: str) -> str:
    lines = []
    for r in results:
        head = f"{r.harness}: {r.action}" + (f" ({r.mode})" if r.mode and r.action != "refused" else "")
        if r.prompt_tokens:
            head += f", prompt about {r.prompt_tokens} tokens"
        lines.append(head)
        if r.action == "refused":
            lines.append(f"  refused: {r.reason}")
            if r.hint:
                lines.append(f"  next: {r.hint}")
        lines += [f"  {c}" for c in r.changes]
        lines += [f"  note: {n}" for n in r.notes]
        if r.env_line:
            lines.append(f"  {r.env_line}")
        if r.action in ("installed", "updated", "unchanged") and r.mode == "replace" and r.keeps:
            lines.append("  not replaced: " + "; ".join(r.keeps))
        elif r.action in ("installed", "updated", "unchanged") and r.mode == "append":
            lines.append("  added to the harness's own prompt, which stays in force; "
                         "`--mode replace` swaps it for shapa's")
    return "\n".join(lines) if lines else f"shapa harness {verb}: no supported harness detected"


def _render_status(rep: dict) -> str:
    lines = [f"prompts: {rep['prompts_dir']}"]
    for s in rep["harnesses"]:
        det = "found" if s["detected"] else "not found"
        wired = "wired" if s.get("wired") else "not wired"
        lines.append(f"{s['harness']}: mode {s['mode']}, {wired} ({det})")
        for p in s["problems"]:
            lines.append(f"  problem: {p}")
        for n in s["notes"]:
            lines.append(f"  note: {n}")
        if s["mode"] == "replace" and s["keeps"]:
            lines.append("  not replaced: " + "; ".join(s["keeps"]))
        elif s["mode"] == "append":
            lines.append("  appended to the harness's own prompt (replace: shapa harness install --mode replace)")
        if s.get("env_line") and not s.get("env_active"):
            lines.append(f"  env: {s['env_line']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="shapa harness", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("install", "uninstall", "status", "watch"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if name != "watch":
            p.add_argument("--harness", action="append", default=[],
                           help="claude-code|codex|opencode|gemini|cursor|all (repeatable); default: every detected")
        if name in ("install", "uninstall"):
            p.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
            p.add_argument("--follow-symlinks", action="store_true", help="write through a symlinked config")
            p.add_argument("--allow-repo-config", action="store_true",
                           help="allow a config directory inside a git work tree")
        if name == "install":
            p.add_argument("--mode", choices=MODES, default=None,
                           help="append (default: added to the harness prompt), replace (swap the harness "
                                "prompt for shapa's), or default (leave the harness alone)")
            p.add_argument("--force", action="store_true", help="take over a value the operator already set")
        if name == "status":
            p.add_argument("--cwd", default=None)
        if name == "watch":
            p.add_argument("--capture-claude", action="store_true",
                           help="run `claude -p` against a loopback endpoint to hash its default prompt")
            p.add_argument("--accept", action="store_true", help="adopt the current capture as the baseline")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "watch":
            from shapa import harnesswatch

            sys.exit(harnesswatch.run(capture_claude=args.capture_claude, accept=args.accept, as_json=args.json))
        if args.cmd == "status":
            rep = status(args.harness or None, args.cwd)
            print(json.dumps(rep, indent=2) if args.json else _render_status(rep))
            return
        if args.cmd == "install":
            results = install(args.harness or None, args.mode, dry_run=args.dry_run, force=args.force,
                              follow_symlinks=args.follow_symlinks, allow_repo_config=args.allow_repo_config)
        else:
            results = uninstall(args.harness or None, dry_run=args.dry_run,
                                follow_symlinks=args.follow_symlinks, allow_repo_config=args.allow_repo_config)
    except HarnessError as exc:
        print(f"shapa harness: {exc}" + (f"\n  next: {exc.hint}" if exc.hint else ""), file=sys.stderr)
        sys.exit(2)
    prefix = "dry run, nothing written\n" if args.dry_run and not args.json else ""
    if args.json:
        print(json.dumps({"dry_run": bool(args.dry_run), "results": [r.as_dict() for r in results]}, indent=2))
    else:
        print(prefix + _render_results(results, args.cmd))
    sys.exit(1 if any(r.action == "refused" for r in results) else 0)


if __name__ == "__main__":
    main()
