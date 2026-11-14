"""Simulated weeks of sessions across two repos: does learning make the repeat corrections stop?

Everything the engine does runs for real: the operator's corrections go through :func:`ledger.record_correction`
(and so :func:`learn.on_correction`), the learner agent works from :func:`learn.pending` and calls
:func:`learn.apply`, and the agent under test sees only the real :func:`bootstrap.build_context` text of its repo.
The one modelled thing is the agent's obedience: it repeats a mistake unless the principle for it was printed at
session start. What these tests measure is the engine: whether a repeat is detected, whether a principle learned in
repo A reaches repo B, whether the rule count stays bounded however a correction is worded, and how the four display
slots are spent when there are more principles than slots. They cannot measure whether a real model obeys a line."""

from datetime import timedelta

import pytest

from shapa import bootstrap, db, focus, learn, metrics
from tests.learnkit import at, correct, lexical_only, make_repo, rows  # noqa: F401

TOPICS = {
    "pnpm": ("Install packages with npm inside a worktree, never with pnpm.", [
        "No, you used pnpm to install packages in the worktree, use npm install there instead.",
        "Wrong again, you ran pnpm install in the worktree, I said use npm install.",
        "No, pnpm again: this worktree needs npm install, not pnpm install."]),
    "header": ("Keep the page header above the fold on mobile screens.", [
        "No, the page header is too low, move the header up above the fold on mobile.",
        "Wrong, the header on mobile is still below the fold: move the page header up.",
        "No, I said the mobile page header goes above the fold, move it up."]),
    "commit": ("Start every commit message with the job id it closes.", [
        "No, the commit message must start with the job id it closes, you left it out.",
        "Wrong again, the commit message has no job id at the start, add the job id it closes.",
        "No, commit message again: the job id it closes goes first."]),
}
MORE = {
    "toast": ("An error toast stays visible for at least five seconds.", [
        "No, the error toast vanished too fast, keep the error toast visible five seconds.",
        "Wrong, that error toast disappears too fast, keep it visible five seconds.",
        "No, error toast again: visible for five seconds minimum."]),
    "sorting": ("Sort a date column with the newest entry first.", [
        "No, the date column must sort the newest entry first.",
        "Wrong, you sorted the date column oldest first, newest entry goes on top.",
        "No, date column order again: newest entry first."]),
    "debug": ("Remove debug prints from library code before committing.", [
        "No, you left a debug print in the library code, remove it before committing.",
        "Wrong again, a debug print is still in the library code, take it out before committing.",
        "No, debug print in library code again, remove it."]),
}
WEEKS = 6


def topic_of(topics, text):
    t = text.lower()
    keys = {"pnpm": "pnpm", "header": "header", "commit": "commit message", "toast": "toast",
            "sorting": "date column", "debug": "debug print"}
    return next(k for k in topics if keys[k] in t)


def printed(text):
    """The principle lines of a session start text, and whether they sit under the recalled-context heading."""
    lines = text.splitlines()
    out = [l for l in lines if l.startswith("[global R")]
    assert not out or f"{learn.HEAD_NOTE}:" in lines
    return out


def simulate(wikis, topics, learning: bool, leak=None):
    """Weekly rows: corrections, repeats, violations, sessions, the rule count, and how many lines were printed."""
    a = wikis.repo
    b = make_repo(wikis.tmp, "repo-b")
    for repo in (a, b):
        c = db.connect(repo / ".shapa")
        f = db.add_item(c, "F", "Mobile header redesign")
        db.add_item(c, "J", "move the page header on mobile", parent=f)
        focus.set_northstar(c, "A site that feels fast on a phone")
        focus.set_fire(c, 1, f)
        c.close()
    learned: dict[str, str] = {}
    weekly = []
    for w in range(WEEKS):
        repos = [a] if w == 0 else [a, b]
        shown_counts = []
        for ri, repo in enumerate(repos):
            sid = f"w{w}r{ri}".ljust(8, "0")
            start = at(7 * w, hours=ri)
            metrics.record(repo / ".shapa", "session", session=sid, now=start)
            lines = printed(bootstrap.build_context(start=repo))
            shown_counts.append(len(lines))
            for k, topic in enumerate(topics):
                if any(topics[topic][0] in l for l in lines) and (w, repo.name, topic) != leak:
                    continue                                  # the principle was in front of the agent
                correct(repo, topics[topic][1][w % 3], session=sid, now=start + timedelta(minutes=10 + k))
        if learning:                                          # the learner agent, at the end of the week
            for repo in repos:
                for item in learn.pending(repo, limit=100, now=at(7 * w + 6))["pending"]:
                    topic = topic_of(topics, item["text"])
                    try:
                        if topic in learned:
                            learn.apply(repo, item["id"], topics[topic][0], into=learned[topic],
                                        wiki=item["wiki"], now=at(7 * w + 6))
                        else:
                            res = learn.apply(repo, item["id"], topics[topic][0], new=True, wiki=item["wiki"],
                                              now=at(7 * w + 6))
                            learned[topic] = f"global/{res['target']}"
                    except db.LedgerError as exc:
                        assert "enforcement" in str(exc)      # a churned principle: the operator's to enforce
        row = {"total": 0, "repeats": 0, "violations": 0, "sessions": 0, "shown": max(shown_counts)}
        for repo in repos:
            m = metrics.compute(repo / ".shapa", at(7 * w - 1), at(7 * w + 7))
            for k in ("total", "repeats", "violations"):
                row[k] += m["corrections"][k]
            row["sessions"] += m["sessions"]
        row["repeats_per_session"] = row["repeats"] / row["sessions"]
        row["rules"] = len(rows(wikis.global_wiki, "R"))
        weekly.append(row)
    return weekly


def test_without_learning_the_repeats_never_stop(wikis):
    weekly = simulate(wikis, TOPICS, learning=False)
    assert weekly[0]["repeats"] == 0 and weekly[0]["total"] == 3          # the first time is not a repeat
    assert [w["repeats"] for w in weekly[1:]] == [6] * 5                  # two repos, three topics, every week
    assert [w["rules"] for w in weekly] == [0] * WEEKS


def test_with_learning_and_room_to_show_every_principle_the_repeats_stop_in_both_repos(wikis):
    weekly = simulate(wikis, TOPICS, learning=True, leak=(3, "repo-b", "header"))
    assert [w["total"] for w in weekly[:3]] == [3, 0, 0]   # learned in A, printed in A and in B: B never has to say it
    # one lapse (the agent ignoring a printed principle in repo B) is caught as a violation of the global principle
    assert (weekly[3]["total"], weekly[3]["violations"], weekly[3]["repeats"]) == (1, 1, 1)
    assert [w["total"] for w in weekly[4:]] == [0, 0]
    assert sum(w["repeats"] for w in weekly) == 1
    assert [w["rules"] for w in weekly] == [3] * WEEKS and all(w["shown"] == 3 for w in weekly[1:])


def test_more_topics_than_display_slots_bound_the_repeats_and_the_rule_count(wikis):
    topics = {**TOPICS, **MORE}
    assert len(topics) == 6 > learn.PRINCIPLES_SHOWN
    weekly = simulate(wikis, topics, learning=True)
    assert weekly[0]["total"] == 6 and weekly[0]["rules"] == 6
    assert all(w["rules"] == 6 for w in weekly)                            # however often it was worded, six rules
    assert all(w["shown"] <= learn.PRINCIPLES_SHOWN for w in weekly)       # the display never grows
    spill = len(topics) - learn.PRINCIPLES_SHOWN
    # the two principles that do not fit are still repeated, one correction per repo for each, no more
    assert [w["repeats"] for w in weekly[1:3]] == [2 * spill] * 2
    assert all(w["repeats"] <= 2 * spill for w in weekly[1:])
    assert all(w["total"] <= 2 * spill for w in weekly[1:])


def test_the_fire_decides_which_principles_take_the_slots(wikis):
    topics = {**TOPICS, **MORE}
    simulate(wikis, topics, learning=True)
    text = bootstrap.build_context(start=wikis.repo)
    lines = printed(text)
    assert len(lines) == learn.PRINCIPLES_SHOWN
    assert any(TOPICS["header"][0] in l for l in lines)                    # the fire is about the mobile header


def test_the_two_runs_side_by_side(tmp_path_factory, monkeypatch):
    """The headline numbers: repeat corrections per session by simulated week, learning off versus on."""
    from shapa import config
    from tests.conftest import Wikis

    runs = {}
    for name, learning in (("off", False), ("on", True)):
        w = Wikis(tmp_path_factory.mktemp(name))
        monkeypatch.setattr(config, "CONFIG_FILE", w.pointer)
        monkeypatch.delenv(config.ENV_VAR, raising=False)
        runs[name] = (simulate(w, {**TOPICS, **MORE}, learning), w)
    off, on = runs["off"][0], runs["on"][0]
    off_r = [round(x["repeats_per_session"], 2) for x in off]
    on_r = [round(x["repeats_per_session"], 2) for x in on]
    print("repeats per session by week  off:", off_r, " on:", on_r)
    assert off_r[1:] == [6.0] * 5                                          # every topic, in both repos, every week
    assert all(o < f for o, f in zip(on_r[1:], off_r[1:]))                  # learning is better every week
    assert max(on_r[1:]) <= 2.0 and sum(x["repeats"] for x in on) <= 2 * 2 * (WEEKS - 1)
    assert sum(x["repeats"] for x in off) == 6 * 2 * (WEEKS - 1)
    # the trend the operator reads: the last 7 days against the 7 before, from the real events
    monkeypatch.setattr(config, "CONFIG_FILE", runs["on"][1].pointer)
    t_on = metrics.trend(runs["on"][1].wiki, at(7 * WEEKS))
    cps = t_on["corrections_per_session"]
    assert cps["current"] is not None and cps["previous"] is not None and cps["better"] in (True, False, None)
    assert cps["better"] is not False
