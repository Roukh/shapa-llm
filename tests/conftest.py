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
