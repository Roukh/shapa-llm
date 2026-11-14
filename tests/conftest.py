"""Suite-wide isolation for the wiki registry (shapa/registry.py) and for
any test that shells out to git.

``init``/``bootstrap``/``fetch``/``upgrade`` register every wiki they see in
``~/.shapa/wikis.json``. Point ``$SHAPA_REGISTRY`` at a throwaway file before
any test runs, so neither in-process calls nor subprocesses that inherit the
environment ever write the real one.

A test that runs real git commands must never see this machine's actual
global or system git config - in particular a shared ``core.hooksPath`` some
other project's setup may point at every repo on the box. ``isolate_git``
below points ``GIT_CONFIG_GLOBAL`` at a throwaway file (with just enough
identity for a commit) and skips the system config, for every test in the
suite.
"""

import atexit
import os
import shutil
import tempfile

import pytest

_REGISTRY_DIR = tempfile.mkdtemp(prefix="shapa-test-registry-")
os.environ["SHAPA_REGISTRY"] = os.path.join(_REGISTRY_DIR, "wikis.json")
atexit.register(shutil.rmtree, _REGISTRY_DIR, True)

_GIT_GLOBAL_DIR = tempfile.mkdtemp(prefix="shapa-test-git-global-")
_GIT_GLOBAL_CONFIG = os.path.join(_GIT_GLOBAL_DIR, ".gitconfig")
with open(_GIT_GLOBAL_CONFIG, "w", encoding="utf-8") as f:
    f.write("[user]\n\tname = Shapa Test\n\temail = shapa-test@example.com\n")
atexit.register(shutil.rmtree, _GIT_GLOBAL_DIR, True)


@pytest.fixture(autouse=True)
def isolate_git(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", _GIT_GLOBAL_CONFIG)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


# The hosted-remote client keeps credentials and sync state under one directory
# ($SHAPA_REMOTE_DIR, default ~/.shapa). No test may ever read or write the real one.
_REMOTE_DIR = tempfile.mkdtemp(prefix="shapa-test-remote-")
os.environ["SHAPA_REMOTE_DIR"] = _REMOTE_DIR
atexit.register(shutil.rmtree, _REMOTE_DIR, True)


@pytest.fixture(autouse=True)
def isolate_remote(monkeypatch, tmp_path):
    """A private, empty remote dir per test; the owner key never leaks in."""
    monkeypatch.setenv("SHAPA_REMOTE_DIR", str(tmp_path / "remote-state"))
    monkeypatch.delenv("SHAPA_OWNER_KEY", raising=False)


@pytest.fixture(autouse=True)
def inline_snapshot(monkeypatch):
    """A session start stores its daily snapshot in a detached process; the suite runs it in-process."""
    monkeypatch.setenv("SHAPA_INLINE_SNAPSHOT", "1")


class Wikis:
    """A global wiki and one repo wiki, both format 4, with an isolated pointer."""

    def __init__(self, root):
        from pathlib import Path

        from shapa import db

        self.tmp = Path(root)
        self.global_wiki = self.tmp / "global"
        self.repo = self.tmp / "my-repo"
        (self.repo / ".git").mkdir(parents=True)
        # a repo with an origin has an automatic wiki key (without one, sync needs --key)
        (self.repo / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = https://git.example/owner/my-repo.git\n', encoding="utf-8")
        self.wiki = self.repo / ".shapa"
        for w in (self.global_wiki, self.wiki):
            w.mkdir(parents=True)
            (w / "AGENTS.md").write_text("# marker\n", encoding="utf-8")
            db.connect(w, create=True).close()
        self.pointer = self.tmp / "config.json"
        self.pointer.write_text('{"memory": "%s"}' % self.global_wiki, encoding="utf-8")

    def conn(self, wiki=None):
        from shapa import db

        return db.connect(wiki or self.wiki)


@pytest.fixture
def wikis(tmp_path, monkeypatch):
    from shapa import config

    w = Wikis(tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", w.pointer)
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    return w
