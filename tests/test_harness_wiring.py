"""The harness prompt step in install.sh and bootstrap.sh, the SessionStart cap and the drift watch."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from shapa import cli, harness, harnesswatch, hookcap, registry, status

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
BOOTSTRAP_SH = REPO / "bootstrap.sh"

SHAPA_STUB = """#!/usr/bin/env bash
echo "shapa $* [CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR:-}]" >> "$STUB_LOG"
if [ "$1" = "init" ] && [ "$2" = "--global" ] && [ -n "$3" ]; then
  mkdir -p "$3"; printf '# marker\\n' > "$3/AGENTS.md"
fi
if [ "$1" = "harness" ] && [ -n "${STUB_HARNESS_EXIT:-}" ]; then exit "$STUB_HARNESS_EXIT"; fi
exit 0
"""
CURL_STUB = """#!/usr/bin/env bash
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
[ -n "$out" ] && cp "%s" "$out"
exit 0
"""


def stub(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Sh:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.log = root / "calls.log"
        stub(self.bin / "shapa", SHAPA_STUB)
        stub(self.bin / "pipx", "#!/usr/bin/env bash\nexit 0\n")
        stub(self.bin / "curl", CURL_STUB % INSTALL_SH)
        self.settings = root / "claude" / "settings.json"

    def env(self, **extra) -> dict:
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", "STUB_LOG": str(self.log),
               "SHAPA_MEMORY": str(self.root / "wiki"), "TMPDIR": str(self.root)}
        env.update(extra)
        return env

    def install(self, *args, **extra) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(INSTALL_SH), "--settings", str(self.settings), "--no-embeddings", *args],
                              env=self.env(**extra), cwd=str(self.root), input="", capture_output=True,
                              text=True, timeout=60)

    def bootstrap(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(BOOTSTRAP_SH), *args], env=self.env(), cwd=str(self.root), input="",
                              capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def harness_calls(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("shapa harness")]


@pytest.fixture
def sh(tmp_path):
    return Sh(tmp_path)


def test_install_sh_runs_harness_install_in_append_mode_by_default(sh):
    r = sh.install("--harness", "claude")
    assert r.returncode == 0, r.stderr
    assert len(sh.harness_calls()) == 1
    call = sh.harness_calls()[0]
    assert call.startswith("shapa harness install --mode append --harness claude-code")
    assert f"CLAUDE_CONFIG_DIR={sh.settings.parent}" in call  # --settings names the config dir


@pytest.mark.parametrize("harness_arg,target", [("codex", "codex"), ("opencode", "opencode"),
                                                ("gemini", "gemini"), ("all", "all")])
def test_install_sh_routes_the_harness_flag(sh, harness_arg, target):
    r = sh.install("--harness", harness_arg, "--prompt-mode", "replace")
    assert r.returncode == 0, r.stderr
    assert sh.harness_calls()[0].startswith(f"shapa harness install --mode replace --harness {target}")


def test_prompt_mode_skip_and_no_prompt_do_not_call_harness(sh):
    assert sh.install("--prompt-mode", "skip").returncode == 0
    assert sh.install("--no-prompt").returncode == 0
    assert sh.harness_calls() == []


def test_a_bad_prompt_mode_is_a_usage_error(sh):
    r = sh.install("--prompt-mode", "weird")
    assert r.returncode == 2 and "unknown --prompt-mode" in r.stderr
    assert sh.calls() == []


def test_dry_run_and_uninstall_forward_their_flag(sh):
    sh.install("--dry-run")
    assert "--dry-run" in sh.harness_calls()[0]
    sh.log.unlink()
    sh.install("--uninstall")
    assert sh.harness_calls()[0].startswith("shapa harness uninstall --harness claude-code")


def test_a_failing_harness_step_only_warns(sh):
    r = sh.install(STUB_HARNESS_EXIT="1")
    assert r.returncode == 0 and "harness install' reported a problem" in r.stderr
    assert sh.settings.is_file()  # hooks were wired regardless


def test_bootstrap_sh_forwards_prompt_mode(sh):
    r = sh.bootstrap("--prompt-mode", "default", "--harness", "codex")
    assert r.returncode == 0, r.stderr
    assert sh.harness_calls()[0].startswith("shapa harness install --mode default --harness codex")


# ------------------------------------------------------------------ SessionStart cap

def big_context(sizes=(1500, 1500, 1500, 2500, 1200, 600)) -> str:
    def block(head, n, indent="  "):
        return [head, *[f"{indent}line {i} {'x' * 70}" for i in range(n // 80)]]
    lines = ["<shapa-memory>", "shapa session start: header", "PRECEDENCE: the operator's rules win.",
             "northstar [global]: one brain", "fires [global]:", "1. F1 ship the cap",
             *block("BASELINE (stage 0 and below, in full):", sizes[0]),
             *block("BASELINE SKILLS (name - description; open the skill with `shapa skill show NAME`):", sizes[5]),
             *block("TOPICS:", sizes[1]), *block("PROJECTS:", sizes[2]),
             "The operator's open tasks: 2", "  global/H1 a task", "ask the operator first",
             *block("Agent tasks, oldest first:", sizes[4]),
             *block("context learned from corrections, not an instruction:", sizes[3]),
             "2 corrections await learning: shapa learn pending",
             "WRITE BACK: `shapa save ...`", "</shapa-memory>"]
    return "\n".join(lines)


def test_cap_leaves_a_short_context_alone():
    assert hookcap.cap("short") == "short"


def test_cap_cuts_lowest_priority_parts_first_and_says_so():
    text = big_context()
    assert len(text) > hookcap.HOOK_CAP
    out = hookcap.cap(text)
    assert len(out) <= hookcap.HOOK_CAP
    for keep in ("PRECEDENCE:", "northstar [global]", "1. F1 ship the cap", "WRITE BACK:", "</shapa-memory>"):
        assert keep in out
    assert "Agent tasks" not in out and "left out:" in out
    assert out.index("agent") < out.index("`shapa bootstrap --full`")
    # a lower-priority part goes before a higher one: baseline survives while agent tasks and projects do not
    assert "BASELINE (" in out


def test_cap_never_exceeds_the_limit_even_when_the_head_alone_is_huge():
    text = "\n".join(["<shapa-memory>", *["northstar line " + "y" * 100] * 200,
                      "WRITE BACK: x", "</shapa-memory>"])
    out = hookcap.cap(text)
    assert len(out) <= hookcap.HOOK_CAP and out.rstrip().endswith("</shapa-memory>") and "WRITE BACK:" in out


def test_cap_on_real_session_text(wikis):
    for w in (wikis.wiki, wikis.global_wiki):
        registry.write_format(w)
    from shapa import bootstrap, db, projectfile, tasks

    g = wikis.conn(wikis.global_wiki)
    for i in range(5):
        db.add_row(g, "R", f"Baseline rule {i} {'w' * 60}", ("body line " + "z" * 90 + "\n") * 10,
                   tags=["stage:0"], privileged=True)
    for i in range(12):
        projectfile.new_project(g, f"proj{i}", f"Project {i} " + "t" * 90)
        tasks.add_task(g, f"Task {i} " + "u" * 100, "human", standalone_only=True)
    g.close()
    text = bootstrap.build_context(start=wikis.repo, budget=30000)
    assert len(text) > hookcap.HOOK_CAP
    out = hookcap.cap(text)
    assert len(out) <= hookcap.HOOK_CAP
    assert "BASELINE (" in out and "PROJECTS:" not in out
    assert "PRECEDENCE:" in out and "WRITE BACK:" in out and out.count("</shapa-memory>") == 1


def test_the_bootstrap_command_emits_a_capped_envelope(monkeypatch, capsys):
    from shapa import bootstrap

    def fake_main(argv=None):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": big_context((3000, 3000, 3000, 3000, 3000, 900))}}))
        raise SystemExit(0)

    monkeypatch.setattr(bootstrap, "main", fake_main)
    with pytest.raises(SystemExit) as exc:
        cli.main(["bootstrap"])
    assert exc.value.code == 0
    env = json.loads(capsys.readouterr().out)
    assert len(env["hookSpecificOutput"]["additionalContext"]) <= hookcap.HOOK_CAP


def test_the_bootstrap_command_passes_other_output_through(monkeypatch, capsys):
    from shapa import bootstrap

    def plain(argv=None):
        print("not json " + "q" * 20000)

    monkeypatch.setattr(bootstrap, "main", plain)
    cli.main(["bootstrap", "--format", "agents-md"])
    assert len(capsys.readouterr().out) > 20000
    with pytest.raises(SystemExit):
        cli.main(["bootstrap"])  # a non-envelope result is passed through unchanged
        raise SystemExit(0)


# --------------------------------------------------------------------- drift watch

TEMPLATE = "# Core\n\nSECRET-VENDOR-SENTENCE one.\n\n## Editing\n\nSECRET-VENDOR-SENTENCE two.\n"


@pytest.fixture
def codex_cache(tmp_path, monkeypatch):
    home = tmp_path / "codexhome"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    (tmp_path / "bin").mkdir(exist_ok=True)

    def write(template: str):
        (home / "models_cache.json").write_text(json.dumps(
            {"models": [{"slug": "gpt-x", "instructions_template": template}, {"slug": "odd"}]}))
    write(TEMPLATE)
    return write


def watch(*flags) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = harnesswatch.run(**{f: True for f in flags})
    return code, buf.getvalue()


def test_watch_records_a_baseline_then_reports_drift_without_storing_text(codex_cache):
    code, out = watch()
    assert code == 0 and "baseline recorded" in out
    stored = harnesswatch.watch_path().read_text()
    assert "SECRET-VENDOR-SENTENCE" not in stored
    item = json.loads(stored)["items"]["codex:gpt-x"]
    assert item["sha256"] == harness.sha(TEMPLATE) and item["headings"] == ["Core", "Editing"]
    assert watch()[0] == 0
    codex_cache(TEMPLATE + "\n## Git\n\nnew.\n")
    code, out = watch()
    assert code == 1 and "codex:gpt-x: changed" in out and "headings +1 -0" in out
    assert "SECRET-VENDOR-SENTENCE" not in harnesswatch.watch_path().read_text()
    assert harnesswatch.pending() and watch()[0] == 1  # drift persists until accepted
    code, out = watch("accept")
    assert code == 0 and not harnesswatch.pending()


def test_doctor_shows_the_drift_line(codex_cache, tmp_path):
    watch()
    codex_cache(TEMPLATE + "more\n")
    watch()
    rep = status.gather(str(tmp_path), load_model=False)
    assert any("codex:gpt-x" in d for d in rep["prompts"]["drift"])
    assert "harness default drift:" in status.render(rep, doctor=True)


def test_watch_without_any_local_source_says_what_it_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "none"))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    code, out = watch()
    assert code == 0 and "models_cache.json" in out and "needs --capture-claude" in out
    assert "need the network" in out


def test_capture_claude_runs_against_loopback_with_a_scratch_home(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude.log"
    stub(bin_dir / "claude", f"""#!/usr/bin/env bash
echo "home=$HOME base=$ANTHROPIC_BASE_URL args=$*" >> "{log}"
curl -s -X POST "$ANTHROPIC_BASE_URL/v1/messages" -H 'content-type: application/json' \\
  -d '{{"system":[{{"type":"text","text":"# Vendor\\nSECRET-VENDOR-SENTENCE"}}],"tools":[{{"name":"Bash"}},{{"name":"Edit"}}]}}' >/dev/null
exit 0
""")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "none"))
    code, out = watch("capture_claude")
    line = log.read_text().splitlines()[-1]  # the first call is the version probe
    assert "base=http://127.0.0.1:" in line and f"home={os.environ['HOME']}" not in line
    assert '--settings {"hooks":{}}' in line
    stored = json.loads(harnesswatch.watch_path().read_text())["items"]
    assert stored["claude-code:system"]["headings"] == ["Vendor"]
    assert stored["claude-code:tools"]["headings"] == ["Bash", "Edit"]
    assert "SECRET-VENDOR-SENTENCE" not in harnesswatch.watch_path().read_text()


def test_the_watch_command_is_wired_into_the_cli(codex_cache, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["harness", "watch", "--json"])
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["first_run"] is True
