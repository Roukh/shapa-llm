"""Metrics: what is measured, how it is bounded, how it rolls up, and what session start flags."""

import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from datetime import timedelta
from unittest import mock

import pytest

from shapa import bootstrap, db, fetch, focus, learn, metrics
from tests.learnkit import at, correct, lexical_only, make_repo  # noqa: F401


def finish(conn, item, when):
    db.close(conn, item, "commit", now=when)


def work(wikis, fire_jobs=0, off_jobs=0, start=0.0, stalled_fire=False):
    """A fire feature F1 with *fire_jobs* closed jobs, and *off_jobs* closed jobs outside every fire."""
    c = wikis.conn()
    f = db.add_item(c, "F", "Hosted API", now=at(start))
    focus.set_northstar(c, "ship it")
    focus.set_fire(c, 1, f)
    for i in range(fire_jobs):
        j = db.add_item(c, "J", f"on fire {i}", parent=f, now=at(start + i * 0.1))
        finish(c, j, at(start + 1 + i * 0.1))
    for i in range(off_jobs):
        j = db.add_item(c, "J", f"off fire {i}", now=at(start + i * 0.1))
        finish(c, j, at(start + 1 + i * 0.1))
    c.close()
    return f


# --- frustration -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("add a ledger row for the trials", 0),
    ("No, wrong.", 0),
    ("I told you already, still wrong", 6),            # three repeat cues
    ("this is shit, wtf", 6),                            # two profanities
    ("WHY would you DO THAT again!!", 2 + 1 + 1),         # again, one shouted word, a double bang
    ("update the README and the JSON API", 0),           # acronyms are not shouting
])
def test_frustration_is_a_deterministic_word_list(text, expected):
    assert metrics.frustration(text)[0] == expected
    assert metrics.frustration("x " * 50 + "fuck shit " + "again still already " * 3)[0] <= 10


# --- empty and fresh wikis ------------------------------------------------------------------------------------

def test_metrics_on_an_empty_wiki_say_none_not_zero_and_never_fail(wikis):
    rep = metrics.report(wikis.wiki, at(-7), at(0))
    assert rep["derived"]["repeat_rate"] is None and rep["derived"]["fetch_p95_ms"] is None
    assert rep["measures"]["sessions"] == 0 and rep["scale"]["flag"] is False
    assert all(t["better"] is None for t in rep["trend"].values())
    assert "shapa metrics" in metrics.render(rep)
    out = io.StringIO()
    with redirect_stdout(out):
        assert metrics.run_metrics(["--cwd", str(wikis.repo), "--json"]) == 0
    assert json.loads(out.getvalue())["repo"] == "my-repo"


def test_metrics_with_no_wiki_in_scope_still_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAPA_MEMORY", str(tmp_path / "nothing"))
    out = io.StringIO()
    with redirect_stdout(out):
        assert metrics.run_metrics(["--cwd", str(tmp_path), "--json"]) == 0
    assert json.loads(out.getvalue())["derived"]["repeat_rate"] is None
    assert not (tmp_path / "nothing").exists()          # a measurement never creates a wiki


def test_a_bad_since_date_is_refused(wikis):
    with pytest.raises(db.LedgerError):
        metrics.parse_since("last tuesday")


# --- recorded events -------------------------------------------------------------------------------------------

def test_fetch_and_bootstrap_record_their_latency_per_device(wikis, monkeypatch, capsys):
    monkeypatch.chdir(wikis.repo)
    c = wikis.conn()
    db.add_row(c, "M", "The operator prefers npm over pnpm in worktrees", "")
    c.close()
    for _ in range(3):
        with pytest.raises(SystemExit):
            fetch.main(["--query", "which package manager in worktrees"])
    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"cwd": str(wikis.repo), "session_id": "sid00001"}))):
        with pytest.raises(SystemExit):
            bootstrap.main([])
    capsys.readouterr()
    m = metrics.compute(wikis.wiki, at(-1) + (metrics._utc() - at(0)), metrics._utc() + timedelta(days=1))
    assert m["latency"]["fetch"]["n"] == 3 and m["latency"]["fetch"]["p95_ms"] > 0
    assert m["latency"]["bootstrap"]["n"] == 1 and m["sessions"] == 1
    assert m["latency"]["fetch"]["p50_ms"] <= m["latency"]["fetch"]["p95_ms"]


def test_skill_use_is_recorded_by_the_hook_command(wikis):
    for i in range(3):
        metrics.run_metrics(["record", "skill-use", "tdd", "--session", f"s{i}", "--cwd", str(wikis.repo)])
    metrics.run_metrics(["record", "skill-use", "grill-me", "--session", "s9", "--cwd", str(wikis.repo)])
    now = metrics._utc()
    m = metrics.compute(wikis.wiki, now - timedelta(days=1), now + timedelta(days=1))
    assert m["skills"]["uses"] == {"tdd": 3, "grill-me": 1} and m["skills"]["uses_total"] == 4
    assert m["sessions"] == 4
    with pytest.raises(Exception):
        metrics.run_metrics(["record", "skill-use", "../etc", "--cwd", str(wikis.repo)])


# --- goal measures ---------------------------------------------------------------------------------------------------

def test_jobs_under_a_fire_are_told_apart_from_jobs_outside_every_fire(wikis):
    work(wikis, fire_jobs=3, off_jobs=1)
    m = metrics.compute(wikis.wiki, at(-1), at(3))
    assert (m["jobs"]["closed"], m["jobs"]["closed_on_fire"], m["jobs"]["closed_off_fire"]) == (4, 3, 1)
    assert metrics.derive(m)["off_fire_share"] == 0.25


def test_fire_features_closed_and_their_lead_time_and_jobs_per_feature(wikis):
    f = work(wikis, fire_jobs=2, start=0)
    c = wikis.conn()
    db.close(c, f, "merge", now=at(5))
    other = db.add_item(c, "F", "Side feature", now=at(0))
    db.add_item(c, "J", "x", parent=other, now=at(0))
    db.close(c, other, "merge", now=at(2))
    c.close()
    m = metrics.compute(wikis.wiki, at(-1), at(10))
    d = metrics.derive(m)
    assert m["fires"]["closed"] == 1 and d["fire_lead_days"] == 5.0
    assert m["features"]["closed"] == 2 and d["feature_lead_days"] == 3.5 and d["jobs_per_feature"] == 1.5


def test_closed_work_survives_the_sweep_through_the_archive(wikis):
    work(wikis, fire_jobs=2, off_jobs=2)
    c = wikis.conn()
    other = db.add_item(c, "F", "Later feature", now=at(2))
    c.close()
    from shapa import ledger
    ledger.run_sweep(wikis.wiki, keep_closed_since="2099-01-01T00:00:00Z")
    c = wikis.conn()
    assert db.items(c, status="closed") == []                    # the sweep took them
    c.close()
    m = metrics.compute(wikis.wiki, at(-1), at(3))
    assert m["jobs"]["closed"] == 4 and m["jobs"]["closed_off_fire"] == 2    # still measured
    assert other


def test_a_fire_with_no_job_closed_in_a_week_is_stalled_unless_it_is_new(wikis):
    f = work(wikis, fire_jobs=1, start=0)                 # its one job closed on day 1
    c = wikis.conn()
    db.add_item(c, "J", "open job", parent=f, now=at(0))
    c.close()
    assert metrics.compute(wikis.wiki, at(0), at(5))["fires"]["stalled"] == []         # day 5: job closed 4 days ago
    assert metrics.compute(wikis.wiki, at(0), at(12))["fires"]["stalled"] == [f]        # day 12: nothing for 11 days
    assert metrics.compute(wikis.wiki, at(0), at(3))["fires"]["stalled"] == []         # young fire


# --- the drift line (ii) -------------------------------------------------------------------------------------------------

def _start_text(wikis, now=None):
    return bootstrap.build_context(start=wikis.repo)


def test_an_agent_that_keeps_closing_off_fire_jobs_trips_the_drift_line_and_one_that_closes_fire_jobs_does_not(wikis, tmp_path):
    t = metrics._utc() - timedelta(days=3)
    c = wikis.conn()
    f = db.add_item(c, "F", "Hosted API", now=t)
    focus.set_northstar(c, "ship it")
    focus.set_fire(c, 1, f)
    for i in range(6):
        j = db.add_item(c, "J", f"side quest {i}", now=t)
        finish(c, j, t + timedelta(hours=i))
    j = db.add_item(c, "J", "the real work", parent=f, now=t)
    finish(c, j, t + timedelta(hours=8))
    c.close()
    text = bootstrap.build_context(start=wikis.repo)
    drift = [l for l in text.splitlines() if l.startswith("drift:")]
    assert len(drift) == 1 and "6 of 7 jobs closed in 14 days sat outside every fire" in drift[0]

    good = make_repo(tmp_path, "focused")
    c = db.connect(good / ".shapa")
    f = db.add_item(c, "F", "Hosted API", now=t)
    focus.set_northstar(c, "ship it")
    focus.set_fire(c, 1, f)
    for i in range(6):
        j = db.add_item(c, "J", f"fire work {i}", parent=f, now=t)
        finish(c, j, t + timedelta(hours=i))
    c.close()
    assert "drift:" not in bootstrap.build_context(start=good)


def test_a_stalled_fire_is_the_drift_line(wikis):
    old = metrics._utc() - timedelta(days=20)
    c = wikis.conn()
    f = db.add_item(c, "F", "Hosted API", now=old)
    focus.set_fire(c, 1, f)
    j = db.add_item(c, "J", "done long ago", parent=f, now=old)
    finish(c, j, old + timedelta(days=1))
    c.close()
    assert f"drift: fire {f} has had no job closed in 7 days" in bootstrap.build_context(start=wikis.repo)


def test_session_start_has_no_learning_lines_when_all_is_fine(wikis):
    c = wikis.conn()
    f = db.add_item(c, "F", "Hosted API")
    focus.set_northstar(c, "ship it")
    focus.set_fire(c, 1, f)
    c.close()
    text = bootstrap.build_context(start=wikis.repo)
    assert "drift:" not in text and "await" not in text and "learned [" not in text


# --- snapshots, roll-up, bounds -----------------------------------------------------------------------------------------------

def test_a_snapshot_is_one_row_per_day_and_repo_and_the_global_wiki_rolls_them_up(wikis, tmp_path):
    b = make_repo(tmp_path, "repo-b")
    for repo, texts in ((wikis.repo, ["No, wrong pnpm in the worktree, use npm install."]),
                        (b, ["No, the header is too low, move the header above the fold.",
                             "No, the header is still too low: move the header above the fold."])):
        for i, t in enumerate(texts):
            correct(repo, t, session=f"s{i}", now=at(1, hours=i))
    for repo in (wikis.repo, b):
        metrics.snapshot(repo / ".shapa", now=at(1, hours=12))
        metrics.snapshot(repo / ".shapa", now=at(1, hours=13))          # the same day again: replaced, not stacked
    rows = metrics.snapshot_rows(wikis.global_wiki)
    assert sorted((d, r) for d, r, _ in rows) == [("2026-10-02", "*"), ("2026-10-02", "my-repo"), ("2026-10-02", "repo-b")]
    one = metrics.rollup(wikis.global_wiki, at(0), at(3), repo="repo-b")
    assert one["measures"]["corrections"]["total"] == 2 and one["measures"]["corrections"]["repeats"] == 1
    allr = metrics.rollup(wikis.global_wiki, at(0), at(3))
    assert allr["repos"] == ["my-repo", "repo-b"]
    assert allr["measures"]["corrections"]["total"] == 3 and allr["derived"]["repeat_rate"] == pytest.approx(1 / 3, abs=0.01)
    assert len(metrics.snapshot_rows(wikis.wiki)) == 1            # the repo's own wiki holds its row too


def test_the_snapshot_command_stores_records_and_latency_beside_each_other(wikis, monkeypatch, capsys):
    metrics.record(wikis.wiki, "latency", ref="fetch", val=12.0)
    out = io.StringIO()
    with redirect_stdout(out):
        metrics.run_metrics(["--cwd", str(wikis.repo), "--snapshot", "--json"])
    ((day, repo, data),) = metrics.snapshot_rows(wikis.wiki)
    assert data["records"] >= 0 and data["latency"]["fetch"]["p95_ms"] == 12.0
    with redirect_stdout(out):
        metrics.run_metrics(["--cwd", str(wikis.repo), "--history", "3"])
    assert "records=" in out.getvalue()


def test_an_older_database_without_the_metrics_table_gets_it_on_connect(wikis):
    c = wikis.conn()
    c.execute("DROP TABLE metrics_daily")
    c.close()
    c = wikis.conn()                                # connect() migrates lazily and additively
    try:
        assert c.execute("SELECT COUNT(*) FROM metrics_daily").fetchone()[0] == 0
        assert c.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0] == "2"
        assert c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0     # nothing else touched
    finally:
        c.close()


def test_a_simulated_year_of_sessions_stays_under_a_fixed_row_count(wikis):
    """Daily snapshots are kept, raw events are dropped by the sweep: the raw table is bounded by
    RAW_DAYS and MAX_EVENTS however long the wiki lives."""
    root = wikis.wiki
    for day in range(365):
        for s in range(6):                                                   # six sessions a day
            t = at(day, hours=s)
            metrics.record(root, "session", session=f"d{day:03d}s{s}", now=t)
            metrics.record(root, "latency", ref="fetch", val=20.0 + s, now=t)
            metrics.record(root, "latency", ref="bootstrap", val=60.0, now=t)
            metrics.record(root, "prompt", session=f"d{day:03d}s{s}", val=0.0, now=t)
        if day % 30 == 29:                                                   # the sweep runs after a feature merges
            metrics.prune(root, now=at(day, hours=23))
    metrics.prune(root, now=at(364, hours=23))
    raw = len(metrics.events(root))
    snaps = len(metrics.snapshot_rows(root))
    assert raw <= 24 * (metrics.RAW_DAYS + 1) + 24             # at most RAW_DAYS days of the day's volume
    assert raw <= metrics.MAX_EVENTS
    assert snaps <= 365 + 1
    assert snaps >= 365 - metrics.RAW_DAYS - 1                  # every dropped day is still a snapshot
    # the history that was dropped is still answerable from the snapshots
    rows = metrics.snapshot_rows(root, "2026-10-01", "2026-10-11")
    assert len(rows) == 10 and rows[0][2]["latency"]["fetch"]["n"] == 6


def test_the_event_cap_holds_even_inside_the_window(wikis):
    conn = metrics._events_conn(wikis.wiki)
    conn.executemany("INSERT INTO m_events(ts, kind, val) VALUES (?, 'prompt', 0)",
                     [(db.now_iso(at(0) + timedelta(seconds=i)),) for i in range(metrics.MAX_EVENTS + 50)])
    conn.commit()
    conn.close()
    out = metrics.prune(wikis.wiki, now=at(1))
    assert len(metrics.events(wikis.wiki)) == metrics.MAX_EVENTS and out["dropped"] >= 50


# --- latency against the size of the data -------------------------------------------------------------------------------------

def _snapshots(root, series, end_day=14):
    """*series*: [(records, fetch_p95_ms)] for the last len(series) days, oldest first."""
    base = metrics._empty()
    for i, (records, p95) in enumerate(series):
        data = json.loads(json.dumps(base))
        data["records"] = records
        data["latency"]["fetch"] = {"n": 20, "p50_ms": p95 / 2, "p95_ms": p95}
        metrics._write_snapshot(root, (at(end_day - len(series) + 1 + i)).strftime("%Y-%m-%d"),
                                metrics.repo_key(root), data)


def test_data_growing_tenfold_with_flat_latency_is_not_flagged(wikis):
    series = [(200 * 10 ** (i / 13), 40.0) for i in range(14)]          # 200 -> ~2000 records, same speed
    _snapshots(wikis.wiki, series)
    sc = metrics.latency_scale(wikis.wiki, at(14))
    assert sc["flag"] is False and sc["latency_growth"] == 1.0 and sc["data_growth"] > 3
    assert sc["current"]["p95_per_1k_records"] < sc["previous"]["p95_per_1k_records"]    # per record: falling


def test_latency_growing_tenfold_with_the_data_is_flagged_in_the_drift_line(wikis):
    series = [(200 * 10 ** (i / 13), 40.0 * 10 ** (i / 13)) for i in range(14)]
    _snapshots(wikis.wiki, series)
    sc = metrics.latency_scale(wikis.wiki, at(14))
    assert sc["flag"] is True
    c = wikis.conn()
    try:
        lines = learn.drift(c, wikis.wiki, at(14))
    finally:
        c.close()
    assert any("fetch latency grew" in l for l in lines)
    assert "FLAG: latency rising with the data" in metrics.render(metrics.report(wikis.wiki, at(7), at(14)))


def test_latency_growing_faster_than_the_data_is_flagged_and_slower_is_not(wikis):
    _snapshots(wikis.wiki, [(1000, 40.0)] * 7 + [(1100, 200.0)] * 7)
    assert metrics.latency_scale(wikis.wiki, at(14))["flag"] is True       # 5x slower on 10% more data
    _snapshots(wikis.wiki, [(1000, 40.0)] * 7 + [(5000, 100.0)] * 7)
    assert metrics.latency_scale(wikis.wiki, at(14))["flag"] is False      # 2.5x slower on 5x the data
