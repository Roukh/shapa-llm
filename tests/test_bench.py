"""Latency budgets for the read path on a wiki of 2,000 rows, and for the learning hooks.

The bounds are at least five times the time measured on the development machine, so a slow CI runner does
not fail them while a regression to something quadratic or to a per-row model call does. Bounds apply to a
warm process (the first call builds the derived index and loads the embedding model if there is one)."""

import json
import random
import time

import pytest

from shapa import bootstrap, db, fetch, focus, learn, metrics
from tests.learnkit import at, correct, lexical_only  # noqa: F401

ROWS = 2000
WORDS = ("cache deploy worktree header payment queue webhook retry schema migration index latency "
         "button modal session token billing invoice scraper parser router hook ledger branch commit "
         "feature wiki memory skill vector tag stage project tracing lint format compile bundle "
         "image font layout mobile desktop contrast keyboard focus error timeout socket proxy").split()


def sentence(rng, n=9):
    return " ".join(rng.choice(WORDS) for _ in range(n))


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    from tests.conftest import Wikis
    from shapa import config

    w = Wikis(tmp_path_factory.mktemp("bench"))
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "CONFIG_FILE", w.pointer)
    mp.delenv(config.ENV_VAR, raising=False)
    rng = random.Random(7)
    conn = w.conn()
    f = db.add_item(conn, "F", "Hosted API latency")
    focus.set_northstar(conn, "fast recall")
    focus.set_fire(conn, 1, f)
    for i in range(ROWS):
        kind = "MRI"[i % 3]
        db.add_row(conn, kind, f"{sentence(rng, 7)} {i}", sentence(rng, 25), tags=[rng.choice(WORDS)])
    conn.close()
    yield w
    mp.undo()


def timings(fn, n):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return out


def test_fetch_and_bootstrap_stay_inside_their_budget_on_2000_rows(big):
    root = big.wiki
    q = "why does the webhook retry hit a timeout on mobile"
    fetch.fetch_context(q, root=root, record=False)                       # warm: builds the index
    f = timings(lambda: fetch.fetch_context(q, root=root, record=False), 25)
    bootstrap.build_context(start=big.repo)
    b = timings(lambda: bootstrap.build_context(start=big.repo), 25)
    p = lambda v, x: metrics.percentile(v, x)
    print(f"2000 rows: fetch p50 {p(f, 50):.1f} ms p95 {p(f, 95):.1f} ms; bootstrap p50 {p(b, 50):.1f} ms p95 {p(b, 95):.1f} ms")
    assert p(f, 95) < FETCH_P95_BUDGET_MS
    assert p(b, 95) < BOOTSTRAP_P95_BUDGET_MS


def test_the_correction_hook_and_pending_stay_inside_their_budget(big):
    c = [correct(big.repo, f"No, wrong {sentence(random.Random(i), 8)}: the {WORDS[i % len(WORDS)]} again.",
                 session=f"s{i % 9:07d}", now=at(1, minutes=i))
         for i in range(3)]
    t = timings(lambda: learn.on_correction(big.wiki, "I1", False), 5)           # the same words again: counts
    t2 = timings(lambda: correct(big.repo, f"No, wrong, the {WORDS[3]} {WORDS[9]} {WORDS[11]} is off.", now=at(2)), 1)
    pend = timings(lambda: learn.pending(big.repo, limit=5), 3)
    print(f"2000 rows: repeat scan {max(t2):.1f} ms, pending {max(pend):.1f} ms")
    assert max(t2) < HOOK_BUDGET_MS and max(pend) < PENDING_BUDGET_MS
    assert c


def test_the_metrics_report_is_cheap_on_a_big_wiki(big):
    t = timings(lambda: metrics.report(big.wiki, at(-7), at(7)), 3)
    print(f"2000 rows: metrics report {max(t):.1f} ms")
    assert max(t) < REPORT_BUDGET_MS
    json.dumps(metrics.report(big.wiki, at(-7), at(7)))


FETCH_P95_BUDGET_MS = 250        # measured p95 14 ms (bm25 mode, warm)
BOOTSTRAP_P95_BUDGET_MS = 100     # measured p95 4 ms
HOOK_BUDGET_MS = 3000          # measured 35 ms; a vector run also loads the model once
PENDING_BUDGET_MS = 3000       # measured 45 ms
REPORT_BUDGET_MS = 600         # measured 46 ms
