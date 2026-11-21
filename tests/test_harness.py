"""`shapa harness`: shapa's own system prompt, composed and wired into each harness.

Every test runs against a temp HOME with CLAUDE_CONFIG_DIR, CODEX_HOME, XDG_CONFIG_HOME
and SHAPA_HOME inside it, and a PATH without the real harness binaries.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from shapa import cli, harness

REPO = Path(__file__).resolve().parent.parent
SUPPORTED = harness.SUPPORTED
TOOL_NAMES = re.compile(r"\b(Bash|Grep|Glob|apply_patch|update_plan|todowrite|TodoWrite|str_replace\w*)\b"
                        r"|\b(Read|Edit|Write) tool\b|`(read|edit|write|bash|grep|glob)`")


class Box:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.claude = self.home / ".claude"
        self.codex = self.home / ".codex"
        self.xdg = self.home / ".config"
        self.opencode = self.xdg / "opencode"
        self.gemini = self.home / ".gemini"
        self.shapa = self.home / ".shapa"

    def env(self) -> dict:
        return {"HOME": str(self.home), "SHAPA_HOME": str(self.shapa), "CLAUDE_CONFIG_DIR": str(self.claude),
                "CODEX_HOME": str(self.codex), "XDG_CONFIG_HOME": str(self.xdg),
                "PATH": f"{self.root / 'bin'}:/usr/bin:/bin", "PYTHONPATH": str(REPO)}

    def snapshot(self) -> dict[str, bytes]:
        out = {}
        for p in sorted(self.home.rglob("*")):
            if p.is_file() and not p.is_symlink() and p.name != "harness.lock":  # the empty lock file stays
                out[str(p.relative_to(self.home))] = p.read_bytes()
        return out

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run([sys.executable, "-m", "shapa", "harness", *args], env=self.env(), cwd=str(self.root),
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if check:
            assert r.returncode == 0, r.stdout + r.stderr
        return r

    # the files a user may already have, per harness
    def seed(self) -> dict[Path, bytes]:
        files = {
            self.claude / "settings.json": b'{\n    "model": "x",\n    "hooks": {"Stop": []}\n}',
            self.claude / "CLAUDE.md": b"# my notes\nbe kind\n",
            self.codex / "config.toml": b'model = "gpt"\n\n[mcp_servers.other]\ncommand = "x"\n',
            self.codex / "AGENTS.md": b"codex notes",
            self.opencode / "opencode.json": b'{"theme":"dark","agents":{"build":{"model":"m"}}}\n',
            self.gemini / "GEMINI.md": b"gemini notes\n",
        }
        for p, data in files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        (self.home / ".bashrc").write_text("# rc\n")
        return files


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = Box(tmp_path)
    (tmp_path / "bin").mkdir()
    for k, v in b.env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GEMINI_SYSTEM_MD", raising=False)
    monkeypatch.delenv("SHAPA_OPENCODE_SCHEMA", raising=False)
    return b


def install(*names, **kw):
    return {r.harness: r for r in harness.install(list(names) or None, **kw)}


# ------------------------------------------------------------------ compose and budgets

def test_core_and_adapters_fit_their_token_budgets():
    core = harness.ASSETS.joinpath("core.md").read_text()
    assert 2400 <= harness.tokens(core) <= 2800, harness.tokens(core)
    for h in SUPPORTED:
        adapter = harness.ASSETS.joinpath(f"adapters/{h}.md").read_text()
        assert 300 <= harness.tokens(adapter) <= 600, (h, harness.tokens(adapter))


def test_core_is_model_neutral_and_names_no_tools():
    core = harness.ASSETS.joinpath("core.md").read_text()
    assert not TOOL_NAMES.search(core)
    assert not re.search(r"claude|gpt|gemini|opus|sonnet|codex|opencode", core, re.I)
    assert len(re.findall(r"(?m)^## \d+\. ", core)) == 10
    for needle in ("skill_match", "invoke every skill", "stop and ask the operator to install one",
                   "Authority, highest first"):
        assert needle in core


def test_each_adapter_restates_what_the_research_says_it_must():
    must = {"claude-code": ("system-reminder", "path:line", "Skill tool"),
            "codex": ("apply_patch", "update_plan", "commentary", "--body-file", "$CODEX_HOME"),
            "opencode": ("<system-update>", "<background-result>"),
            "gemini": ("<untrusted_context>", "${AvailableTools}", "never return an empty response".lower())}
    for h, words in must.items():
        text = harness.ASSETS.joinpath(f"adapters/{h}.md").read_text()
        for w in words:
            assert w in text or w in text.lower(), (h, w)


def test_composed_prompt_budget_and_mode_variants():
    for h in SUPPORTED:
        rep, app = harness.render(h, "replace"), harness.render(h, "append")
        assert 2700 <= harness.tokens(rep) <= 3400
        assert "the whole system prompt" in rep and "added to the harness's own" in app
    assert "${AvailableTools}" in harness.render("gemini", "replace")
    assert "${" not in harness.render("gemini", "append")
    assert "shapa:template" not in harness.render("gemini", "replace")


def test_default_mode_is_append(box):
    assert harness.DEFAULT_MODE == "append"
    box.seed()
    r = install("codex")["codex"]
    assert r.mode == "append" and r.action == "installed"
    assert not (box.codex / "config.toml").read_text().count("model_instructions_file")
    assert harness.MD_BEGIN in (box.codex / "AGENTS.md").read_text()


# ------------------------------------------------------------- round trip per harness

@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize("name", SUPPORTED)
def test_round_trip_restores_every_byte(box, name, mode):
    seeded = box.seed()
    before = box.snapshot()
    res = install(name, mode=mode)[name]
    assert res.action == "installed", res
    assert harness.prompt_path(name).is_file()
    assert box.snapshot() != before
    assert (harness.state_path()).is_file()
    harness.uninstall([name])
    assert box.snapshot() == before
    for p, data in seeded.items():
        assert p.read_bytes() == data
    assert not list(box.home.rglob("*.shapa-bak"))
    assert not harness.state_path().exists()


@pytest.mark.parametrize("mode", ["append", "replace"])
def test_round_trip_from_an_empty_home_leaves_nothing(box, mode):
    before = box.snapshot()
    install("claude-code", "codex", "opencode", "gemini", mode=mode)
    harness.uninstall(["claude-code", "codex", "opencode", "gemini"])
    assert box.snapshot() == before


@pytest.mark.parametrize("mode", ["append", "replace"])
def test_install_is_idempotent(box, mode):
    box.seed()
    install(*SUPPORTED, mode=mode)
    once = box.snapshot()
    second = install(*SUPPORTED, mode=mode)
    assert all(r.action == "unchanged" for r in second.values()), {k: v.action for k, v in second.items()}
    assert box.snapshot() == once
    text = (box.codex / ("AGENTS.md" if mode == "append" else "config.toml")).read_text()
    assert text.count("BEGIN shapa-harness") == 1


def test_a_second_install_after_a_prompt_edit_in_the_package_updates_only_the_prompt(box):
    box.seed()
    install("codex", mode="replace")
    harness.prompt_path("codex").write_text("stale")
    r = install("codex")["codex"]
    assert r.action == "updated"
    assert harness.prompt_path("codex").read_text() == harness.render("codex", "replace")


def test_claude_replace_wires_the_agent_setting_and_agent_file(box):
    box.seed()
    install("claude-code", mode="replace")
    settings = json.loads((box.claude / "settings.json").read_text())
    assert settings["agent"] == "shapa" and settings["model"] == "x"
    agent = (box.claude / "agents" / "shapa.md").read_text()
    head, body = agent.split("---\n", 2)[1], agent.split("---\n", 2)[2]
    assert "name: shapa" in head and "tools" not in head and "model" not in head
    assert body == harness.prompt_path("claude-code").read_text()
    st = harness.status(["claude-code"])["harnesses"][0]
    assert st["wired"] and any("tool descriptions" in k for k in st["keeps"])


def test_claude_append_leaves_the_settings_alone(box):
    box.seed()
    before = (box.claude / "settings.json").read_bytes()
    install("claude-code")
    assert (box.claude / "settings.json").read_bytes() == before
    assert harness.MD_BEGIN in (box.claude / "CLAUDE.md").read_text()
    assert not (box.claude / "agents" / "shapa.md").exists()


def test_claude_config_dir_is_respected(box, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    install("claude-code", mode="replace")
    assert (other / "agents" / "shapa.md").is_file() and not box.claude.exists()


def test_codex_key_sits_before_the_first_table_and_survives_odd_paths(box, monkeypatch, tmp_path):
    box.seed()
    odd = tmp_path / 'h o"me' / ".shapa"
    monkeypatch.setenv("SHAPA_HOME", str(odd))
    install("codex", mode="replace")
    data = tomllib.loads((box.codex / "config.toml").read_text())
    assert data["model_instructions_file"] == str(odd / "prompts" / "codex.md")
    assert data["model"] == "gpt" and data["mcp_servers"]["other"]["command"] == "x"
    assert Path(data["model_instructions_file"]).is_absolute()


def test_opencode_replace_covers_the_four_agents_with_an_absolute_file_ref(box):
    box.seed()
    install("opencode", mode="replace")
    cfg = json.loads((box.opencode / "opencode.json").read_text())
    ref = "{file:" + str(harness.prompt_path("opencode")) + "}"
    assert [cfg["agents"][a]["system"] for a in harness.OPENCODE_AGENTS] == [ref] * 4
    assert cfg["agents"]["build"]["model"] == "m" and cfg["theme"] == "dark"


def test_opencode_v1_config_gets_agent_prompt_keys(box):
    (box.opencode).mkdir(parents=True)
    (box.opencode / "opencode.json").write_text('{"agent":{"build":{"model":"m"}}}\n')
    install("opencode", mode="replace")
    cfg = json.loads((box.opencode / "opencode.json").read_text())
    assert "agents" not in cfg and "{file:" in cfg["agent"]["plan"]["prompt"]
    harness.uninstall(["opencode"])
    assert json.loads((box.opencode / "opencode.json").read_text()) == {"agent": {"build": {"model": "m"}}}


def test_opencode_append_uses_the_instructions_list(box):
    box.seed()
    install("opencode")
    cfg = json.loads((box.opencode / "opencode.json").read_text())
    assert cfg["instructions"] == [str(harness.prompt_path("opencode"))]
    assert "system" not in cfg["agents"]["build"]


def test_gemini_prints_the_env_line_and_never_edits_an_rc_file(box):
    box.seed()
    rc = (box.home / ".bashrc").read_bytes()
    res = install("gemini", mode="replace")["gemini"]
    assert res.env_line == f"export GEMINI_SYSTEM_MD={harness.prompt_path('gemini')}"
    assert (box.home / ".bashrc").read_bytes() == rc
    assert not any(box.home.glob(".zshrc")) and not any(box.home.glob(".profile"))
    assert (box.gemini / "GEMINI.md").read_bytes() == b"gemini notes\n"
    st = harness.status(["gemini"])["harnesses"][0]
    assert not st["env_active"] and any("GEMINI_SYSTEM_MD" in n for n in st["notes"])
    os.environ["GEMINI_SYSTEM_MD"] = str(harness.prompt_path("gemini"))
    assert harness.status(["gemini"])["harnesses"][0]["env_active"]


def test_cursor_is_reported_unsupported_and_nothing_is_written(box):
    before = box.snapshot()
    r = install("cursor")["cursor"]
    assert r.action == "unsupported" and "additive" in r.notes[0]
    assert box.snapshot() == before


def test_status_says_what_is_not_replaced(box):
    install("codex", mode="replace")
    out = box.run("status", "--harness", "codex").stdout
    assert "mode replace" in out and "not replaced:" in out and "sandbox" in out
    st = json.loads(box.run("status", "--harness", "codex", "--json").stdout)["harnesses"][0]
    assert st["wired"] and st["prompt"]["current"] and st["keeps"]


# ---------------------------------------------------------------------------- modes

def test_mode_switching_moves_the_wiring_and_leaves_no_residue(box):
    box.seed()
    before = box.snapshot()
    install("opencode", "codex", mode="append")
    install("opencode", "codex", mode="replace")
    assert "model_instructions_file" in (box.codex / "config.toml").read_text()
    assert harness.MD_BEGIN not in (box.codex / "AGENTS.md").read_text()
    assert "instructions" not in json.loads((box.opencode / "opencode.json").read_text())
    install("opencode", "codex", mode="default")
    assert not harness.prompt_path("codex").exists()
    assert (box.codex / "config.toml").read_bytes() == before[".codex/config.toml"]
    install("opencode", "codex")  # a plain install keeps the stored mode: default stays default
    assert harness.status(["codex"])["harnesses"][0]["mode"] == "default"
    install("opencode", "codex", mode="append")
    harness.uninstall(["opencode", "codex"])
    assert box.snapshot() == before


# --------------------------------------------------------------------------- dry run

@pytest.mark.parametrize("mode", ["append", "replace", "default"])
def test_dry_run_writes_nothing_but_plans_everything(box, mode):
    box.seed()
    before = box.snapshot()
    results = harness.install(list(SUPPORTED), mode, dry_run=True)
    assert box.snapshot() == before
    assert not box.shapa.exists()
    if mode != "default":
        assert all(r.changes for r in results)
    assert not list(box.home.rglob("*.tmp")) and not list(box.home.rglob("*.lock"))


def test_dry_run_of_a_mode_switch_and_of_uninstall_changes_nothing(box):
    box.seed()
    install(*SUPPORTED, mode="append")
    before = box.snapshot()
    harness.install(list(SUPPORTED), "replace", dry_run=True)
    harness.uninstall(list(SUPPORTED), dry_run=True)
    assert box.snapshot() == before


def test_cli_dry_run_json(box):
    out = json.loads(box.run("install", "--harness", "codex", "--dry-run", "--json").stdout)
    assert out["dry_run"] is True and out["results"][0]["changes"]
    assert not box.shapa.exists()


# -------------------------------------------------------------- opencode fail-open

def test_opencode_refuses_when_the_prompt_file_cannot_be_written(box):
    box.seed()
    box.shapa.mkdir(parents=True)
    (box.shapa / "prompts").write_text("a file where the directory should be")
    before = box.snapshot()
    r = install("opencode", mode="replace")["opencode"]
    assert r.action == "refused"
    assert box.snapshot() == before


def test_opencode_prompt_file_missing_is_reported_as_a_silent_fallback(box):
    box.seed()
    install("opencode", mode="replace")
    harness.prompt_path("opencode").unlink()
    st = harness.status(["opencode"])["harnesses"][0]
    assert any("fall back" in p for p in st["problems"])


def test_an_empty_written_prompt_is_refused_before_any_config_is_touched(box, monkeypatch):
    box.seed()
    before = box.snapshot()
    monkeypatch.setattr(harness, "render", lambda h, mode="append": "")
    r = install("opencode", mode="replace")["opencode"]
    assert r.action == "refused" and box.snapshot() == before


# ------------------------------------------------------------------------ break-it

def test_a_symlinked_config_is_refused_and_untouched(box, tmp_path):
    (box.codex).mkdir(parents=True)
    target = tmp_path / "dotfiles" / "config.toml"
    target.parent.mkdir()
    target.write_text('model = "x"\n')
    (box.codex / "config.toml").symlink_to(target)
    r = install("codex", mode="replace")["codex"]
    assert r.action == "refused" and "symlink" in r.reason
    assert (box.codex / "config.toml").is_symlink() and target.read_text() == 'model = "x"\n'


def test_follow_symlinks_writes_the_target_and_keeps_the_link(box, tmp_path):
    (box.codex).mkdir(parents=True)
    target = tmp_path / "dotfiles" / "config.toml"
    target.parent.mkdir()
    target.write_text('model = "x"\n')
    (box.codex / "config.toml").symlink_to(target)
    r = install("codex", mode="replace", follow_symlinks=True)["codex"]
    assert r.action == "installed" and (box.codex / "config.toml").is_symlink()
    assert "model_instructions_file" in target.read_text()
    harness.uninstall(["codex"], follow_symlinks=True)
    assert target.read_text() == 'model = "x"\n'


def test_a_symlinked_prompt_file_is_refused(box, tmp_path):
    (box.shapa / "prompts").mkdir(parents=True)
    victim = tmp_path / "victim.md"
    victim.write_text("precious")
    harness.prompt_path("codex").symlink_to(victim)
    r = install("codex")["codex"]
    assert r.action == "refused" and victim.read_text() == "precious"


@pytest.mark.parametrize("path,content,name", [
    ("codex/config.toml", b'model = "x\n[broken', "codex"),
    ("claude/settings.json", b'{"hooks": ', "claude-code"),
    ("claude/settings.json", b'[1,2]', "claude-code"),
    ("config/opencode/opencode.json", b'{\n // a comment\n "theme": "x"}', "opencode"),
])
def test_malformed_config_is_refused_with_the_bytes_untouched(box, path, content, name):
    p = box.home / ("." + path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    r = install(name, mode="replace")[name]
    assert r.action == "refused" and r.hint
    assert p.read_bytes() == content
    assert not list(box.home.rglob("*.shapa-bak"))
    cli_run = box.run("install", "--harness", name, "--mode", "replace", check=False)
    assert cli_run.returncode == 1 and "refused" in cli_run.stdout


def test_a_user_model_instructions_file_is_kept_then_taken_over_on_force_and_restored(box):
    cfg = box.codex / "config.toml"
    cfg.parent.mkdir(parents=True)
    original = b'model_instructions_file = "/home/me/mine.md"  # mine\nmodel = "x"\n\n[profiles.a]\nk = 1\n'
    cfg.write_bytes(original)
    r = install("codex", mode="replace")["codex"]
    assert r.action == "refused" and "/home/me/mine.md" in r.reason
    assert cfg.read_bytes() == original and not harness.state_path().exists()
    r = install("codex", mode="replace", force=True)["codex"]
    assert r.action == "installed"
    data = tomllib.loads(cfg.read_text())
    assert data["model_instructions_file"] == str(harness.prompt_path("codex"))
    assert b"/home/me/mine.md" in cfg.with_name("config.toml.shapa-bak").read_bytes()
    assert "# shapa-displaced:" in cfg.read_text()
    install("codex", mode="replace")  # the forced consent persists for this harness
    assert harness.status(["codex"])["harnesses"][0]["wired"]
    harness.uninstall(["codex"])
    assert cfg.read_bytes() == original


def test_a_user_agent_setting_is_kept_then_restored(box):
    box.claude.mkdir(parents=True)
    settings = box.claude / "settings.json"
    original = b'{"agent": "mine", "x": 1}\n'
    settings.write_bytes(original)
    assert install("claude-code", mode="replace")["claude-code"].action == "refused"
    assert settings.read_bytes() == original and not (box.claude / "agents").exists()
    install("claude-code", mode="replace", force=True)
    assert json.loads(settings.read_text())["agent"] == "shapa"
    assert json.loads(settings.with_name("settings.json.shapa-bak").read_text())["agent"] == "mine"
    # the user edits the file after install: uninstall removes only shapa's edit
    data = json.loads(settings.read_text())
    data["added"] = True
    settings.write_text(json.dumps(data))
    harness.uninstall(["claude-code"])
    assert json.loads(settings.read_text()) == {"agent": "mine", "x": 1, "added": True}


def test_a_user_agent_file_named_shapa_is_not_clobbered(box):
    (box.claude / "agents").mkdir(parents=True)
    mine = box.claude / "agents" / "shapa.md"
    mine.write_text("---\nname: shapa\n---\nmy own agent\n")
    r = install("claude-code", mode="replace")["claude-code"]
    assert r.action == "refused" and mine.read_text().endswith("my own agent\n")
    install("claude-code", mode="replace", force=True)
    harness.uninstall(["claude-code"])
    assert mine.read_text() == "---\nname: shapa\n---\nmy own agent\n"


def test_a_user_opencode_system_value_is_kept(box):
    box.opencode.mkdir(parents=True)
    cfg = box.opencode / "opencode.json"
    original = b'{"agents":{"plan":{"system":"my words"}}}\n'
    cfg.write_bytes(original)
    r = install("opencode", mode="replace")["opencode"]
    assert r.action == "refused" and cfg.read_bytes() == original
    install("opencode", mode="replace", force=True)
    harness.uninstall(["opencode"])
    assert cfg.read_bytes() == original


def test_uninstall_after_the_user_edited_the_config_removes_only_shapas_block(box):
    box.seed()
    install("codex")
    agents = box.codex / "AGENTS.md"
    agents.write_text(agents.read_text() + "\nmy later line\n")
    harness.uninstall(["codex"])
    assert agents.read_text().startswith("codex notes") and "my later line" in agents.read_text()
    assert harness.MD_BEGIN not in agents.read_text()


def test_uninstall_without_a_state_file_still_finds_the_wiring(box):
    box.seed()
    install("codex", "opencode", mode="replace")
    harness.state_path().unlink()
    harness.uninstall(["codex", "opencode"])
    assert "model_instructions_file" not in (box.codex / "config.toml").read_text()
    assert "system" not in json.dumps(json.loads((box.opencode / "opencode.json").read_text()))


def test_concurrent_installs_serialize_into_one_clean_result(box):
    box.seed()
    before = box.snapshot()
    procs = [subprocess.Popen([sys.executable, "-m", "shapa", "harness", "install", "--harness", "codex",
                               "--harness", "opencode", "--mode", "replace"],
                              env=box.env(), cwd=str(box.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(8)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0] * 8, outs
    cfg = (box.codex / "config.toml").read_text()
    assert cfg.count("BEGIN shapa-harness") == 1
    tomllib.loads(cfg)
    oc = json.loads((box.opencode / "opencode.json").read_text())
    assert oc["agents"]["build"]["model"] == "m"
    assert not list(box.home.rglob("*.tmp"))
    harness.uninstall(["codex", "opencode"])
    assert box.snapshot() == before


def test_a_config_dir_inside_a_git_repo_is_refused(box, monkeypatch):
    repo = box.home / "work" / "project"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(repo / "cfg"))
    r = install("claude-code", mode="replace")["claude-code"]
    assert r.action == "refused" and "git work tree" in r.reason
    assert not (repo / "cfg").exists()
    r = install("claude-code", mode="replace", allow_repo_config=True)["claude-code"]
    assert r.action == "installed"


def test_claude_config_dir_pointing_at_the_repo_root_itself(box, monkeypatch):
    repo = box.home / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(repo))
    assert install("claude-code")["claude-code"].action == "refused"
    assert sorted(p.name for p in repo.iterdir()) == [".git"]


def test_the_shapa_home_inside_a_repo_is_refused(box, monkeypatch):
    repo = box.home / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("SHAPA_HOME", str(repo / ".shapa"))
    with pytest.raises(harness.HarnessError):
        harness.install(["codex"])
    assert not (repo / ".shapa").exists()


def test_unknown_harness_and_mode_are_usage_errors(box):
    assert box.run("install", "--harness", "vim", check=False).returncode == 2
    assert box.run("install", "--mode", "weird", check=False).returncode == 2


def test_install_with_nothing_detected_is_a_quiet_no_op(box):
    r = box.run("install")
    assert "no supported harness detected" in r.stdout


def test_install_without_a_harness_flag_takes_every_detected_one(box):
    box.codex.mkdir()
    box.gemini.mkdir()
    r = json.loads(box.run("install", "--json").stdout)
    assert sorted(x["harness"] for x in r["results"]) == ["codex", "gemini"]


def test_prompt_and_state_files_hold_no_secret_material(box, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    install(*SUPPORTED)
    blob = b"".join(box.snapshot().values())
    assert b"sk-secret-value" not in blob


# -------------------------------------------------------- doctor and status wiring

def test_status_gather_reports_prompt_modes_and_a_broken_wiring_is_a_problem(box):
    from shapa import status

    box.seed()
    install("codex", mode="replace")
    rep = status.gather(str(box.root), load_model=False)
    assert "codex" in [p["harness"] for p in rep["prompts"]["harnesses"]]
    assert "system prompt: codex replace" in status.render(rep)
    harness.prompt_path("codex").unlink()
    rep = status.gather(str(box.root), load_model=False)
    assert any("system prompt for codex" in p for p in status.problems(rep))


def test_status_json_never_contains_text_of_the_prompt(box):
    install("codex")
    out = box.run("status", "--json").stdout
    assert "Authority, highest first" not in out


def test_cli_dispatches_harness(box, capsys):
    cli.main(["harness", "status", "--harness", "cursor"])
    assert "cursor: mode none" in capsys.readouterr().out


# ---------------------------------------------------- more break-it: IO failures, odd files

def test_a_failed_write_midway_rolls_back_the_files_already_written(box, monkeypatch):
    box.seed()
    before = box.snapshot()
    real = harness._atomic_write

    def flaky(path, data, like=None):
        if path.name == "settings.json":
            raise OSError("disk full")
        return real(path, data, like)

    monkeypatch.setattr(harness, "_atomic_write", flaky)
    r = install("claude-code", mode="replace")["claude-code"]
    assert r.action == "refused" and "disk full" in r.reason
    monkeypatch.undo()
    snap = box.snapshot()
    assert not any(k.endswith("agents/shapa.md") for k in snap)
    keep = lambda d: {k: v for k, v in d.items() if not k.startswith(".shapa/")}  # noqa: E731
    assert keep(snap) == keep(before)
    assert not harness.state_path().exists()


def test_a_non_utf8_and_a_directory_config_are_refused(box):
    box.codex.mkdir(parents=True)
    (box.codex / "config.toml").write_bytes(b"\xff\xfe broken")
    assert install("codex", mode="replace")["codex"].action == "refused"
    (box.codex / "config.toml").unlink()
    (box.codex / "config.toml").mkdir()
    assert install("codex", mode="replace")["codex"].action == "refused"


def test_a_corrupt_state_file_stops_the_run_without_touching_configs(box):
    box.seed()
    box.shapa.mkdir(parents=True)
    harness.state_path().write_text("{not json")
    before = box.snapshot()
    r = box.run("install", "--harness", "codex", check=False)
    assert r.returncode == 2 and "not valid JSON" in r.stderr
    assert box.snapshot() == before


def test_the_gemini_env_line_is_shell_safe_for_odd_paths(box, monkeypatch):
    odd = box.home / "my dir" / "it's"
    monkeypatch.setenv("SHAPA_HOME", str(odd))
    line = install("gemini", mode="replace")["gemini"].env_line
    out = subprocess.run(["bash", "-c", line + '; printf %s "$GEMINI_SYSTEM_MD"'], capture_output=True, text=True)
    assert out.stdout == str(harness.prompt_path("gemini"))


def test_status_survives_a_malformed_config_and_reports_it(box):
    install("codex", mode="replace")
    (box.codex / "config.toml").write_text("[[[")
    st = harness.status(["codex"])["harnesses"][0]
    assert not st["wired"] and any("not valid TOML" in p for p in st["problems"])


@pytest.mark.parametrize("name,mode,rel", [
    ("codex", "append", ".codex/AGENTS.md"),
    ("claude-code", "append", ".claude/CLAUDE.md"),
    ("gemini", "append", ".gemini/GEMINI.md"),
    ("codex", "replace", ".codex/config.toml"),
    ("claude-code", "replace", ".claude/settings.json"),
    ("opencode", "append", ".config/opencode/opencode.json"),
])
def test_uninstall_after_a_reinstall_and_later_edits_never_reverts_the_users_edits(box, name, mode, rel):
    seeded = box.seed()
    path = box.home / rel
    first = seeded[path].decode()
    install(name, mode=mode)
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        data["later_user_key"] = "kept"
        path.write_text(json.dumps(data, indent=2) + "\n")
        later = "later_user_key"
    else:
        later = "later_user_line"
        path.write_text(path.read_text() + f"\n{later} = 1\n")
    install(name, mode=mode)                         # a reinstall after the user's edit
    harness.uninstall([name])
    restored = path.read_text()
    assert later in restored, "the user's later edit survived the uninstall"
    if path.suffix == ".json":                        # what was there before shapa stays
        assert {k: v for k, v in json.loads(restored).items() if k != later} == json.loads(first)
    else:
        assert first.strip().split("\n")[0] in restored
    assert harness.MD_BEGIN not in restored


def test_the_characters_per_token_heuristic_has_one_source():
    from shapa import bootstrap, config

    assert harness.CHARS_PER_TOKEN is config.CHARS_PER_TOKEN is bootstrap.CHARS_PER_TOKEN == 4
    defining = [p.name for p in (REPO / "shapa").glob("*.py")
                if re.search(r"^CHARS_PER_TOKEN\s*=", p.read_text(encoding="utf-8"), re.M)]
    assert defining == ["config.py"]
