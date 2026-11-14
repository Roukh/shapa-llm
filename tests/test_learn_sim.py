"""Simulated weeks of sessions across two repos: does learning make the repeat corrections stop?

The agent is a model: it repeats a mistake unless the principle for it was in front of it at session start
(the real :func:`bootstrap.build_context` text of that repo), and the operator corrects every mistake through
the real correction path. What these tests measure is the engine: whether a repeat is *detected*, whether a
principle learned in repo A reaches repo B, and whether the metrics move the right way. They do not (cannot)
measure whether a real model obeys a line it is shown."""

from datetime import timedelta

import pytest

from shapa import bootstrap, learn, metrics
from tests.learnkit import at, correct, lexical_only, make_repo  # noqa: F401

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
WEEKS = 6


def topic_of(text):
    t = text.lower()
    return next(k for k in TOPICS if k in t)


def simulate(wikis, learning: bool, leak=None):
    a = wikis.repo
    b = make_repo(wikis.tmp, "repo-b")
    weekly = []
    for w in range(WEEKS):
        repos = [a] if w == 0 else [a, b]
        for ri, repo in enumerate(repos):
            sid = f"w{w}r{ri}".ljust(8, "0")
            start = at(7 * w, hours=ri)
            root = repo / ".shapa"
            metrics.record(root, "session", session=sid, now=start)
            text = bootstrap.build_context(start=repo)
            for k, topic in enumerate(TOPICS):
                shown = any(l.startswith("learned [") and TOPICS[topic][0] in l for l in text.splitlines())
                if shown and (w, repo.name, topic) != leak:
                    continue                                  # the principle was in front of the agent
                correct(repo, TOPICS[topic][1][w % 3], session=sid, now=start + timedelta(minutes=10 + k))
        if learning:                                          # the learner agent, at the end of the week
            for repo in repos:
                for item in learn.pending(repo, limit=100)["pending"]:
                    learn.apply(repo, item["id"], TOPICS[topic_of(item["text"])][0], wiki=item["wiki"],
                                now=at(7 * w + 6))
        row = {}
        for repo in repos:
            m = metrics.compute(repo / ".shapa", at(7 * w - 1), at(7 * w + 7))
            for k in ("total", "repeats", "violations"):
                row[k] = row.get(k, 0) + m["corrections"][k]
            row["prompts"] = row.get("prompts", 0) + m["prompts"]
            row["fr"] = row.get("fr", 0.0) + m["frustration_sum"] + metrics.REPEAT_FRUSTRATION * m["corrections"]["repeats"]
            row["sessions"] = row.get("sessions", 0) + m["sessions"]
        row["rate"] = row["repeats"] / row["total"] if row["total"] else 0.0
        row["frustration"] = row["fr"] / row["prompts"] if row["prompts"] else 0.0
        weekly.append(row)
    return weekly, a, b


def test_without_learning_the_repeats_never_stop(wikis, tmp_path):
    weekly, a, b = simulate(wikis, learning=False)
    assert weekly[0]["repeats"] == 0                      # the first time is not a repeat
    assert [w["repeats"] for w in weekly[2:]] == [6, 6, 6, 6]       # both repos repeat all three, every week
    assert weekly[-1]["rate"] == 1.0 and weekly[-1]["total"] == 6


def test_with_learning_the_repeat_rate_falls_and_stays_down_across_two_repos(wikis, tmp_path):
    weekly, a, b = simulate(wikis, learning=True, leak=(3, "repo-b", "header"))
    totals = [w["total"] for w in weekly]
    assert totals[0] == 3                                  # repo A's three first-time corrections
    assert totals[1] == 0 and totals[2] == 0               # learned in A, served in A and in B (B never had to say it)
    # one lapse (the agent ignoring a served principle in repo B) is caught as a violation of the global principle
    assert weekly[3]["total"] == 1 and weekly[3]["violations"] == 1 and weekly[3]["rate"] == 1.0
    assert totals[4] == 0 and totals[5] == 0               # and after it was revised: down again
    assert sum(w["repeats"] for w in weekly) == 1
    # frustration (messages + 3 per repeat) is lowest where nothing repeats
    assert weekly[5]["frustration"] <= weekly[0]["frustration"]


def test_the_two_runs_side_by_side(tmp_path_factory, monkeypatch):
    """The headline numbers: repeat-correction rate per simulated week, learning off versus on."""
    from shapa import config
    from tests.conftest import Wikis

    runs = {}
    for name, learning in (("off", False), ("on", True)):
        w = Wikis(tmp_path_factory.mktemp(name))
        monkeypatch.setattr(config, "CONFIG_FILE", w.pointer)
        monkeypatch.delenv(config.ENV_VAR, raising=False)
        runs[name] = (simulate(w, learning)[0], w)
    off, on = runs["off"][0], runs["on"][0]
    off_rates = [round(x["rate"], 2) for x in off]
    on_rates = [round(x["rate"], 2) for x in on]
    print("repeat rate by week, off:", off_rates, " on:", on_rates)
    assert sum(x["repeats"] for x in on) == 0 and sum(x["repeats"] for x in off) == 27
    assert on_rates[-1] == 0.0 and off_rates[-1] == 1.0
    assert off[-1]["frustration"] > on[-1]["frustration"]
    # the trend the operator reads: the last 7 days against the 7 before
    monkeypatch.setattr(config, "CONFIG_FILE", runs["on"][1].pointer)
    t_on = metrics.trend(runs["on"][1].wiki, at(7 * WEEKS))
    assert t_on["corrections_per_session"]["better"] in (True, None)
