"""What the learning loop writes is ordinary rows, tags and links, so it syncs with no Worker change;
what is per device (events, latency, use counts, serve counts, snapshots) never leaves the machine."""

import pytest

from shapa import db, focus, learn, metrics
from tests.learnkit import at, lexical_only  # noqa: F401
from tests.test_sync import Device, KEY, a, b, double  # noqa: F401  (fixtures)


def test_principles_their_evidence_and_the_learned_issue_sync_to_another_device(a, b):
    c = a.conn()
    issue, _ = db.add_row(c, "I", "No, pnpm in the worktree.", "Operator: No, pnpm in the worktree, use npm install.",
                          tags=["correction", "repeat-checked"], source="correction", now=at(0))
    rid, _ = db.add_row(
        c, "R", "Install packages with npm inside a worktree, never with pnpm.",
        "Why: this holds in every repo\nAlso: Use npm install in worktrees.\nEvidence:\n- I1 repo-a 2026-10-01: No, pnpm in the worktree",
        tags=["principle", "violations:2", "revisions:1", "evidence:1", "serves:F1", "last-seen:2026-10-02T00:00:00Z"],
        source="learn", now=at(0))
    db.link(c, issue, rid, "related")
    db.retag(c, issue, add=["learned", "learned-at:2026-10-02T00:00:00Z", f"generalized-into:{rid}", "serves:F1"])
    f = db.add_item(c, "F", "Hosted API")
    focus.set_fire(c, 1, f)                       # tags the feature `fire`
    c.close()
    a.sync()
    b.sync()
    c = b.conn()
    try:
        got = db.get_row(c, rid)
        assert got.body.count("Evidence:") == 1 and "- I1 repo-a 2026-10-01" in got.body
        assert {"principle", "violations:2", "revisions:1", "evidence:1"} <= set(got.tags)
        assert {"learned", f"generalized-into:{rid}"} <= set(db.get_row(c, issue).tags)
        assert (issue, rid, "related") in db.links_of(c, issue)
        assert "fire" in {r[0] for r in c.execute("SELECT tag FROM tags WHERE id = ?", (f,))}
        assert learn.awaiting_count(c) == 0
    finally:
        c.close()


def test_a_churn_mark_syncs_and_blocks_a_fourth_revision_on_the_other_device(a, b):
    c = a.conn()
    rid, _ = db.add_row(c, "R", "Always run the formatter before committing code.", "",
                        tags=["principle", "violations:4", "revisions:3", "churn"], source="learn")
    c.close()
    a.sync()
    b.sync()
    c = b.conn()
    iid, _ = db.add_row(c, "I", "No, formatter again", "Operator: No, you skipped the formatter again.",
                        tags=["correction"], source="correction")
    c.close()
    with pytest.raises(db.LedgerError, match="enforcement"):
        learn.apply(b.wikis.repo, iid, "Always run the formatter before committing code, every time.", wiki="repo",
                    scope="repo", why="this repo only, the formatter is repo-specific")


def test_metrics_snapshots_and_events_stay_on_the_device(a, b):
    metrics.record(a.wiki, "latency", ref="fetch", val=9.0)
    metrics.snapshot(a.wiki)
    a.sync()
    b.sync()
    assert metrics.snapshot_rows(b.wiki) == []
    assert metrics.events(b.wiki) == []
    assert len(metrics.snapshot_rows(a.wiki)) == 1
