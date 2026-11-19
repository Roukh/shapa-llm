"""``shapa remote devices --mint``: a device token goes into an env file and nowhere else."""

import os
import stat
import subprocess

import pytest

from shapa import remote
from tests.hosted_double import OWNER_KEY
from tests.test_remote import double, logged_in, run  # noqa: F401  (the fixtures are imported)


def mint(capsys, path, *extra, var="OPENAI_TOKEN", name="openai"):
    return run(capsys, ["devices", "--mint", name, "--env-file", str(path), "--env-var", var, *extra])


def token_of(double, name):
    return next(d["token"] for d in double.devices.values() if d["name"] == name)


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setenv("SHAPA_OWNER_KEY", OWNER_KEY)


def test_a_minted_token_goes_into_a_new_0600_file_and_is_printed_nowhere(logged_in, double, capsys, tmp_path):
    env = tmp_path / "agent.env"
    code, out, err = mint(capsys, env, "--reach", "baseline", "--wikis", "global", "--scopes", "read,write")
    assert code == 0
    token = token_of(double, "openai")
    assert env.read_text() == f"OPENAI_TOKEN={token}\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert token not in out + err and OWNER_KEY not in out + err and OWNER_KEY not in env.read_text()
    assert "openai" in out and "reach baseline" in out and "d2" in out
    made = double.devices["d2"]
    assert (made["reach"], made["wikis"], made["scopes"]) == ("baseline", ["global"], ["read", "write"])


def test_it_replaces_its_own_line_and_keeps_every_other_line(logged_in, double, capsys, tmp_path):
    env = tmp_path / "agent.env"
    env.write_text("# keys\nOTHER=1\nexport OPENAI_TOKEN=old\n  \nLAST=no newline", encoding="utf-8")
    env.chmod(0o644)
    assert mint(capsys, env)[0] == 0
    token = token_of(double, "openai")
    assert env.read_text() == f"# keys\nOTHER=1\nOPENAI_TOKEN={token}\n  \nLAST=no newline"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    # a second variable is appended (the missing final newline is added), the first is untouched
    assert mint(capsys, env, var="INSTINCT_TOKEN", name="instinct")[0] == 0
    lines = env.read_text().splitlines()
    assert lines[-1] == f"INSTINCT_TOKEN={token_of(double, 'instinct')}" and f"OPENAI_TOKEN={token}" in lines
    assert "LAST=no newline" in lines and "OTHER=1" in lines


def test_the_default_reach_is_baseline_and_wikis_are_all(logged_in, double, capsys, tmp_path):
    assert mint(capsys, tmp_path / "e.env")[0] == 0
    made = double.devices["d2"]
    assert made["reach"] == "baseline" and made["wikis"] is None and made["scopes"] == ["read", "write"]
    assert mint(capsys, tmp_path / "f.env", "--reach", "full", name="full-one")[0] == 0
    assert double.devices["d3"]["reach"] == "full"


def test_it_refuses_a_symlink_and_mints_nothing(logged_in, double, capsys, tmp_path):
    target = tmp_path / "real.env"
    target.write_text("KEEP=1\n", encoding="utf-8")
    link = tmp_path / "link.env"
    os.symlink(target, link)
    code, out, err = mint(capsys, link)
    assert code == 1 and "symlink" in err
    assert target.read_text() == "KEEP=1\n" and len(double.devices) == 1  # only the logged-in laptop


def test_it_refuses_a_file_tracked_by_git_and_mints_nothing(logged_in, double, capsys, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    env = repo / "tracked.env"
    env.write_text("A=1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.env"], cwd=repo, check=True)
    code, out, err = mint(capsys, env)
    assert code == 1 and "tracked by git" in err
    assert env.read_text() == "A=1\n" and len(double.devices) == 1
    # an untracked file in the same repo is fine
    assert mint(capsys, repo / "untracked.env")[0] == 0


def test_it_needs_a_file_and_a_variable_and_a_valid_name(logged_in, double, capsys, tmp_path):
    assert run(capsys, ["devices", "--mint", "x"])[0] == 1
    assert run(capsys, ["devices", "--mint", "x", "--env-file", str(tmp_path / "e")])[0] == 1
    assert run(capsys, ["devices", "--env-file", str(tmp_path / "e")])[0] == 1  # env options without --mint
    for var in ("1BAD", "A B", "A;rm", ""):
        assert mint(capsys, tmp_path / "e.env", var=var)[0] == 1
    assert mint(capsys, tmp_path / "e.env", name=" padded ")[0] == 1
    assert mint(capsys, tmp_path / "missing" / "e.env")[0] == 1
    assert mint(capsys, tmp_path)[0] == 1  # a directory
    assert len(double.devices) == 1 and not (tmp_path / "e.env").exists()


def test_a_failed_write_revokes_the_device_and_never_shows_the_token(logged_in, double, capsys, tmp_path, monkeypatch):
    def boom(path, var, token):
        raise OSError(f"disk full while writing {token}")

    monkeypatch.setattr(remote, "_write_env_file", boom)
    env = tmp_path / "agent.env"
    code, out, err = mint(capsys, env)
    assert code == 1 and "revoked" in err
    token = double.devices["d2"]["token"]
    assert double.devices["d2"]["revoked"] is True
    assert token not in out + err and not env.exists()


def test_a_wrong_owner_key_writes_nothing(logged_in, double, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SHAPA_OWNER_KEY", "wrong-" + "k" * 40)
    env = tmp_path / "agent.env"
    code, out, err = mint(capsys, env)
    assert code == 1 and not env.exists() and "wrong-" not in out + err


def test_the_device_list_shows_the_reach(logged_in, double, capsys, tmp_path):
    mint(capsys, tmp_path / "e.env")
    code, out, _ = run(capsys, ["devices"])
    assert code == 0 and "openai" in out and "baseline" in out
