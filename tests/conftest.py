"""Suite-wide isolation for the wiki registry (shapa/registry.py).

``init``/``bootstrap``/``fetch``/``upgrade`` register every wiki they see in
``~/.shapa/wikis.json``. Point ``$SHAPA_REGISTRY`` at a throwaway file before
any test runs, so neither in-process calls nor subprocesses that inherit the
environment ever write the real one.
"""

import atexit
import os
import shutil
import tempfile

import pytest

_REGISTRY_DIR = tempfile.mkdtemp(prefix="shapa-test-registry-")
os.environ["SHAPA_REGISTRY"] = os.path.join(_REGISTRY_DIR, "wikis.json")
atexit.register(shutil.rmtree, _REGISTRY_DIR, True)


@pytest.fixture(autouse=True)
def _isolated_git_config(tmp_path_factory, monkeypatch):
    """This machine has a GLOBAL ``core.hooksPath`` (a shared hooks dir
    whose pre-commit checks author identity) - every test that shells out
    to git must never see it. Point ``GIT_CONFIG_GLOBAL`` at a throwaway
    file with ``user.name``/``user.email`` set (so a bare commit succeeds)
    and skip the system config entirely."""
    gitconfig = tmp_path_factory.mktemp("git-config") / "gitconfig"
    gitconfig.write_text("[user]\n\tname = shapa-test\n\temail = shapa-test@example.com\n",
                         encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
