"""The learning loop and the metrics at three sizes of wiki, lexical mode: every operation a session pays for
stays inside its budget at 50,000 rows and does not grow with the row count.

Budgets (the largest wiki, asserted on the median of warm runs, and 3x on the slowest): the correction hook 150 ms, `learn pending --limit 20` 500 ms, `shapa metrics`
300 ms, what session start adds 10 ms. A size is built by direct inserts (the loop itself is tested elsewhere);
the derived similarity index is caught up once, as the sweep and `learn pending` do, then every operation is timed
warm. The bounds the test asserts are the budgets above; the measured numbers are printed, and the test also
requires the largest size to cost at most three times the smallest (or to sit under a 30 ms floor, where timer
noise is the whole difference)."""

import hashlib
import random
import time
from datetime import timedelta

import pytest

from shapa import bootstrap, config, db, focus, learn, ledger, metrics
from tests.learnkit import at, lexical_only  # noqa: F401

SIZES = (500, 5_000, 50_000)
BUDGET_MS = {"on_correction": 150, "pending_20": 500, "metrics": 300, "session_start_added": 10}
FLOOR_MS = 30
WORDS = ("cache deploy worktree header payment queue webhook retry schema migration index latency "
         "button modal session token billing invoice scraper parser router hook ledger branch commit "
         "feature wiki memory skill vector tag stage project tracing lint format compile bundle "
         "image font layout mobile desktop contrast keyboard focus error timeout socket proxy").split()


def build(root, n):
    """*n* rows in the wiki at *root* by direct inserts: a third memories, rules and issues, a fifth of the
    issues unlearned corrections, sixty principles, one tag a row, 48 rows a day of history
    (so the window a report reads holds the same few hundred rows at every size)."""
    rng = random.Random(n)
    conn = db.connect(root)
    f = db.add_item(conn, "F", "Hosted API latency")
    focus.set_northstar(conn, "fast recall")
    focus.set_fire(conn, 1, f)
    rows_, tags_ = [], []
    counts = {"M": 0, "R": 0, "I": 0}
    for i in range(n):
        kind = "MRI"[i % 3]
        counts[kind] += 1
        rid = f"{kind}{counts[kind]}"
        summary = " ".join(rng.choice(WORDS) for _ in range(7)) + f" {i}"
        body = " ".join(rng.choice(WORDS) for _ in range(25))
        if kind == "I" and counts[kind] % 5 == 0:
            body = "Operator: " + summary
            tags_.append((rid, "correction"))
            tags_.append((rid, "repeat-checked"))
        if kind == "R" and counts[kind] <= 60:
            tags_ += [(rid, "principle"), (rid, "evidence:1")]
        tags_.append((rid, rng.choice(WORDS)))
        stamp = db.now_iso(at(0) - timedelta(minutes=(n - i) * 30))     # 48 rows a day, the newest now
        rows_.append((rid, kind, summary, body, "agent", hashlib.sha256(f"{i}{summary}".encode()).hexdigest()[:16],
                      stamp, stamp))
    with conn:
        conn.executemany("INSERT INTO mri(id, kind, summary, body, source, hash, created, updated) "
                         "VALUES (?,?,?,?,?,?,?,?)", rows_)
        conn.executemany("INSERT OR IGNORE INTO tags(id, tag) VALUES (?, ?)", tags_)
        conn.executemany("INSERT OR REPLACE INTO uids(id, uid) VALUES (?, ?)", [(r[0], "u-" + r[0]) for r in rows_])
        for kind, k in counts.items():
            conn.execute("INSERT OR REPLACE INTO counters(kind, next) VALUES (?, ?)", (kind, k + 1))
    conn.close()


def best_of(fn, n):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return sorted(out)[len(out) // 2], max(out)


def measure(tmp_path_factory, n, mp):
    from tests.conftest import Wikis

    w = Wikis(tmp_path_factory.mktemp(f"scale{n}"))
    mp.setattr(config, "CONFIG_FILE", w.pointer)
    mp.delenv(config.ENV_VAR, raising=False)
    build(w.wiki, n)
    learn.reindex(w.repo)                                          # the sweep / first `learn pending` does this once
    out = {}
    n_corr = [0]

    def correct_one():
        n_corr[0] += 1
        ledger.record_correction(f"No, wrong, the {WORDS[n_corr[0] % 40]} {WORDS[(n_corr[0] * 7) % 40]} is off, "
                                 f"change it {n_corr[0]}.", cwd=w.repo, session=f"s{n_corr[0]:07d}",
                                 now=at(1, minutes=n_corr[0]))

    correct_one()                                                   # warm
    out["on_correction"] = best_of(correct_one, 7)
    learn.pending(w.repo, limit=20)
    out["pending_20"] = best_of(lambda: learn.pending(w.repo, limit=20), 5)
    metrics.report(w.wiki, at(-7), at(7))
    out["metrics"] = best_of(lambda: metrics.report(w.wiki, at(-7), at(7)), 5)

    sconn = db.connect(w.wiki)          # session start already holds its connection: only the added calls are timed

    def added():
        learn.awaiting_count(sconn)
        learn.awaiting_activation(conn=sconn)
        cands = learn.principle_candidates(sconn, "repo", w.wiki)
        learn.rank_principles(cands, "hosted api latency", {"my-repo/F1"})
        learn.drift(sconn, w.wiki)

    added()
    out["session_start_added"] = best_of(added, 15)
    sconn.close()
    out["bootstrap_total"] = best_of(lambda: bootstrap.build_context(start=w.repo), 5)
    return out


def test_every_operation_stays_in_budget_at_fifty_thousand_rows_and_does_not_grow_with_them(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    t0 = time.perf_counter()
    results = {}
    try:
        for n in SIZES:
            results[n] = measure(tmp_path_factory, n, mp)
    finally:
        mp.undo()
    ops = [*BUDGET_MS, "bootstrap_total"]
    print("\nrows".ljust(10) + "".join(op.rjust(22) for op in ops) + "   (median / max ms)")
    for n in SIZES:
        print(str(n).ljust(10) + "".join(f"{results[n][op][0]:8.1f} /{results[n][op][1]:8.1f}".rjust(22) for op in ops))
    small, large = results[SIZES[0]], results[SIZES[-1]]
    for op, budget in BUDGET_MS.items():
        assert large[op][0] <= budget, f"{op}: {large[op][0]:.1f} ms (median) over its {budget} ms budget at {SIZES[-1]} rows"
        assert large[op][1] <= 3 * budget, f"{op}: one run took {large[op][1]:.1f} ms against a {budget} ms budget"
        assert large[op][0] <= max(3 * small[op][0], FLOOR_MS), \
            f"{op}: {large[op][0]:.1f} ms at {SIZES[-1]} rows against {small[op][0]:.1f} ms at {SIZES[0]}"
    assert time.perf_counter() - t0 < 30


def test_a_big_wiki_with_no_index_yet_is_caught_up_a_bounded_part_at_a_time_never_all_at_once(tmp_path, monkeypatch):
    from tests.conftest import Wikis

    w = Wikis(tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", w.pointer)
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    build(w.wiki, 12_000)
    t = time.perf_counter()
    ledger.record_correction("No, wrong, the cache deploy is off, change it.", cwd=w.repo, session="s0000001", now=at(1))
    first_ms = (time.perf_counter() - t) * 1000
    with learn.LearnIndex(w.wiki) as ix:
        done = int(ix._meta("l_wm"))
        indexed = ix.conn.execute("SELECT COUNT(DISTINCT rid) FROM l_chunks").fetchone()[0]
    assert first_ms < BUDGET_MS["on_correction"] and 0 < indexed <= learn.CATCHUP_ROWS + 2
    conn = db.connect(w.wiki)
    try:
        top = conn.execute("SELECT MAX(rowid) FROM mri").fetchone()[0]
    finally:
        conn.close()
    assert done < top                                                # the rest waits for a sweep or `learn pending`
    learn.reindex(w.repo)
    with learn.LearnIndex(w.wiki) as ix:
        assert int(ix._meta("l_wm")) >= top


PROCESS_ADDED_BUDGET_MS = 15


def test_the_whole_session_start_process_costs_under_15_ms_more_with_learning_than_without_at_50000_rows(tmp_path):
    """The hook is a process: imports, connections and the after-output measurement count. The same wiki is
    started with `SHAPA_LEARNING=0` (no learning lines, no measurement, no import of learn or metrics) and
    with it, interleaved so machine noise hits both alike. Every learning run is made the first session of a
    day, so it also pays for launching its detached snapshot."""
    import json
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    from tests.conftest import Wikis

    w = Wikis(tmp_path)
    build(w.wiki, 50_000)
    (tmp_path / "home" / ".shapa").mkdir(parents=True)
    (tmp_path / "home" / ".shapa" / "config.json").write_text(json.dumps({"memory": str(w.global_wiki)}))
    for wiki in (w.wiki, w.global_wiki):
        (wiki / ".shapa-format").write_text("4\n")
    root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "HOME": str(tmp_path / "home"), "SHAPA_REGISTRY": str(tmp_path / "reg.json"),
           "SHAPA_REMOTE_DIR": str(tmp_path / "remote"), "PYTHONPATH": root}
    env.pop("SHAPA_INLINE_SNAPSHOT", None)           # a real session start: the snapshot is a detached process
    payload = json.dumps({"cwd": str(w.repo), "session_id": "bench0001"})

    def once(learning: bool) -> float:
        if learning:                                  # make it the first session of a day again
            c = sqlite3.connect(w.wiki / ".shapa-index.db")
            try:
                c.execute("DELETE FROM m_meta WHERE key IN ('snap_day', 'snap_try')")
                c.commit()
            except sqlite3.OperationalError:
                pass
            c.close()
            time.sleep(0.25)                          # the previous run's detached snapshot finishes first
        t = time.perf_counter()
        r = subprocess.run([sys.executable, "-m", "shapa", "bootstrap"], input=payload, text=True,
                           capture_output=True, cwd=str(w.repo),
                           env={**env, "SHAPA_LEARNING": "1" if learning else "0"})
        ms = (time.perf_counter() - t) * 1000
        assert r.returncode == 0 and "SessionStart" in r.stdout, r.stderr
        return ms

    once(True)
    once(False)                                       # warm: bytecode, the derived index
    added = []
    for attempt in range(3):
        runs = {True: [], False: []}
        for _ in range(9):
            for learning in (True, False):
                runs[learning].append(once(learning))
        med = {k: sorted(v)[len(v) // 2] for k, v in runs.items()}
        added.append(med[True] - med[False])
        print(f"\nsession start process at 50,000 rows: learning off {med[False]:.1f} ms, on {med[True]:.1f} ms, "
              f"added {added[-1]:.1f} ms (attempt {attempt + 1})")
        if added[-1] < PROCESS_ADDED_BUDGET_MS:
            break
    assert min(added) < PROCESS_ADDED_BUDGET_MS, f"added {added} ms against a {PROCESS_ADDED_BUDGET_MS} ms budget"
