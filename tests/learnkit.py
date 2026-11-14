"""Shared fixtures for the learning-loop tests: temporary repo wikis around one temporary global wiki."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from shapa import db, ledger

import pytest

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def lexical_only(monkeypatch):
    """The learning tests measure the lexical channel the same on every machine; the vector channel is
    exercised with a deterministic stand-in embedder (``FakeVectors`` in test_learn.py), because the
    thresholds were set against it and a real model's cosines are not part of a unit test."""
    from shapa import learn

    monkeypatch.setattr(learn, "default_embedder", lambda: None)


def at(days=0, hours=0, minutes=0):
    return T0 + timedelta(days=days, hours=hours, minutes=minutes)


def make_repo(tmp: Path, name: str) -> Path:
    """A git checkout with its own format-4 wiki; returns the checkout (a session's cwd)."""
    repo = Path(tmp) / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = https://git.example/owner/{name}.git\n', encoding="utf-8")
    wiki = repo / ".shapa"
    wiki.mkdir()
    (wiki / "AGENTS.md").write_text("# marker\n", encoding="utf-8")
    db.connect(wiki, create=True).close()
    return repo


def correct(repo, text, *, session="sess0001", now=None):
    """What the UserPromptSubmit hook does with one operator message: (result, info)."""
    info: dict = {}
    hit = ledger.record_correction(text, cwd=repo, session=session, now=now or T0, info=info)
    return hit, info


def rows(wiki: Path, kind: str):
    conn = db.connect(wiki)
    try:
        return db.rows(conn, kind)
    finally:
        conn.close()
