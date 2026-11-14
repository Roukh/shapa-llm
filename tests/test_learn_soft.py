"""Soft candidates: a short statement made while work is claimed ("move this header up") is filed as an
issue tagged `soft` through the same path as a strong correction, and counts for nothing until applied."""

import time

import pytest

from shapa import db, ledger, learn, metrics
from tests.learnkit import at, correct, lexical_only, rows  # noqa: F401

SOFT = ["move this header up", "the title is too long", "the compile time is too slow"]


def claim(wikis, session="s1"):
    c = wikis.conn()
    f = db.add_item(c, "F", "Mobile site")
    j = db.add_item(c, "J", "tidy the header", parent=f)
    db.claim(c, j, session)
    c.close()
    return j


def soft_ids(wikis):
    return [r.id for r in rows(wikis.wiki, "I") if "soft" in r.tags]


def test_three_soft_sentences_are_filed_mid_build_as_soft_issues_linked_to_the_claimed_item(wikis):
    j = claim(wikis)
    for i, text in enumerate(SOFT):
        hit, info = correct(wikis.repo, text, session="sess0001", now=at(0, minutes=i))
        assert hit is not None and info["soft"] is True
    rs = rows(wikis.wiki, "I")
    assert [r.summary for r in rs] == SOFT and all({"correction", "soft", "repeat-checked"} <= set(r.tags) for r in rs)
    conn = wikis.conn()
    try:
        assert all((r.id, j, "about") in db.links_of(conn, r.id) for r in rs)
    finally:
        conn.close()
    pend = learn.pending(wikis.repo, now=at(1))
    assert pend["count"] == 3 and all(i["soft"] for i in pend["pending"])


def test_nothing_is_filed_without_a_claimed_item(wikis):
    for text in SOFT:
        assert correct(wikis.repo, text, now=at(0))[0] is None
    assert rows(wikis.wiki, "I") == []


@pytest.mark.parametrize("text", [
    "can you move the header up?", "yes", "ok", "Okay.", "continue", "go", "go ahead", "proceed", "merge", "done",
    "thanks", "do 3", "do #12", "/compact", "/clear now", "x" * 241, ""])
def test_a_question_an_approval_a_slash_command_or_a_long_message_is_not_a_candidate(wikis, text):
    claim(wikis)
    assert correct(wikis.repo, text, now=at(0))[0] is None
    assert rows(wikis.wiki, "I") == []


def test_a_strong_match_is_filed_once_not_twice(wikis):
    claim(wikis)
    _, info = correct(wikis.repo, "No, the header is wrong, move it up.", now=at(0))
    assert info["soft"] is False
    assert len(rows(wikis.wiki, "I")) == 1 and "soft" not in rows(wikis.wiki, "I")[0].tags


def test_at_most_ten_unapplied_soft_candidates_per_session(wikis):
    claim(wikis)
    for i in range(14):
        correct(wikis.repo, f"make zebra{i} quokka{i} larger", session="sess0001", now=at(0, minutes=i))
    assert len(soft_ids(wikis)) == 10
    correct(wikis.repo, "make another walrus larger", session="sess0002", now=at(1))      # another session has its own ten
    assert len(soft_ids(wikis)) == 11


def test_soft_candidates_do_not_count_as_corrections_repeats_or_frustration_until_applied(wikis):
    claim(wikis)
    correct(wikis.repo, "move this header up", session="sess0001", now=at(0))
    correct(wikis.repo, "move this header up please", session="sess0001", now=at(0, minutes=5))
    m = metrics.compute(wikis.wiki, at(-1), at(1))
    assert m["corrections"]["total"] == 0 and m["corrections"]["repeats"] == 0
    d = metrics.derive(m)
    assert d["repeat_rate"] in (None, 0.0)
    out = learn.apply(wikis.repo, "I1", "Keep the page header above the fold on mobile screens.", now=at(0, 2))
    assert out["learned"]
    m = metrics.compute(wikis.wiki, at(-1), at(1))
    assert m["corrections"]["total"] == 2 and m["corrections"]["repeats"] == 1     # both count once the pair is applied


def test_a_dismissed_soft_candidate_is_gone_and_never_measured(wikis):
    claim(wikis)
    correct(wikis.repo, "the title is too long", now=at(0))
    assert metrics.compute(wikis.wiki, at(-1), at(1))["corrections"]["total"] == 0
    res = learn.dismiss(wikis.repo, "I1")
    assert res["status"] == "removed" and rows(wikis.wiki, "I") == []
    assert learn.pending(wikis.repo, now=at(1))["count"] == 0
    m = metrics.compute(wikis.wiki, at(-1), at(1))
    assert m["corrections"]["total"] == 0 and m["corrections"]["dismissed"] == 0


def test_a_soft_candidate_never_changes_how_a_strong_one_is_counted(wikis):
    claim(wikis)
    correct(wikis.repo, "move this header up", now=at(0))
    _, info = correct(wikis.repo, "No, wrong, never leave debug prints in library code.", now=at(1))
    assert info["soft"] is False and not info["repeat"]
    assert metrics.compute(wikis.wiki, at(-1), at(2))["corrections"]["total"] == 1


def test_the_added_prompt_check_costs_microseconds(wikis):
    claim(wikis)
    t0 = time.perf_counter()
    for _ in range(2000):
        ledger.is_soft_candidate("move this header up a little more please")
    per_ms = (time.perf_counter() - t0) * 1000 / 2000
    assert per_ms < 5                          # the budget is 5 ms; the check itself takes microseconds
