"""Where the learning loop and the central brain meet: one system-tag list, the session-start order, the
sweep with both lines' rules, the schema built from any starting file, the counter tags under the tag
index, ``learn apply`` into the global wiki, ``skill uproot`` beside learned rules, and the commands."""

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from shapa import (bootstrap, db, dbsync, learn, ledger, mcp, projectfile, registry, relate, save, skills,
                   syncrec, tasks)
from tests.learnkit import at, correct, lexical_only, make_repo, rows  # noqa: F401

PNPM = "No, you used pnpm to install packages in the worktree, use npm install there instead."
PNPM_RULE = "Install packages with npm inside a worktree, never with pnpm."
HEADER = "No, the page header is too low, move the header up above the fold on mobile."
HEADER_RULE = "Keep the page header above the fold on mobile screens."
PRINCIPLE_TAGS = ["principle", "evidence:3", "serves:my-repo/F1", "violations:2", "revisions:1",
                  "last-seen:2026-11-13T10:00:00Z"]


@pytest.fixture
def g(wikis):
    for w in (wikis.wiki, wikis.global_wiki):
        registry.write_format(w)
    c = wikis.conn(wikis.global_wiki)
    yield c
    c.close()


def tags(conn, rid):
    return set(db._tags_of(conn, rid))


def learned(wikis, issue="I1", text=PNPM, rule=PNPM_RULE, **kw):
    """One correction, then its principle filed through ``learn apply`` (global by default)."""
    correct(wikis.repo, text, now=at(0))
    kw = {"new": True, **kw} if "into" not in kw else kw
    return learn.apply(wikis.repo, issue, rule, now=at(0, 1), **kw)


def recount(conn):
    return sorted(tuple(r) for r in conn.execute("SELECT tag, substr(id, 1, 1), count(*) FROM tags GROUP BY 1, 2"))


def indexed(conn):
    return sorted(tuple(r) for r in conn.execute("SELECT tag, k, n FROM tag_counts"))


# --- 2a: one system-tag list ---------------------------------------------------------------------

def test_the_learning_loops_tags_join_the_one_system_tag_list():
    for t in db.LEARNING_TAGS:
        assert t in db.SYSTEM_TAGS and relate.is_system_tag(t)
    for p in db.LEARNING_TAG_PREFIXES:
        assert p in db.SYSTEM_TAG_PREFIXES and relate.is_system_tag(p + "x")
    assert db.RESERVED_ROW_TAGS == db.LEARNING_TAGS and db.RESERVED_ROW_TAG_PREFIXES == db.LEARNING_TAG_PREFIXES
    assert not relate.is_system_tag("principles") and not relate.is_system_tag("servers:x")


def test_learned_context_is_never_a_topic_but_stays_in_the_tag_index(wikis, g):
    db.add_row(g, "R", "Review in small diffs", "Small diffs read faster.", tags=["review"])
    db.add_row(g, "M", "Reviews go faster small", "Seen twice.", tags=["review"])
    learned(wikis)
    assert [t["tag"] for t in relate.topics(g)] == ["review"]
    assert {"principle", "evidence:1", "serves:none"} <= {e["tag"] for e in relate.tag_index(g)}
    text = bootstrap.build_context(start=wikis.repo)
    topics = text.split("TOPICS:\n", 1)[1].splitlines()[0]
    assert "review" in topics and not re.search(r"principle|serves|evidence|last-seen", topics)


@pytest.mark.parametrize("tag", ["Principle", "ｐrinciple", "principle​", "Serves:x", "ＶIOLATIONS:2",
                                 "last​-seen:1", "REPEAT-OF:R1", "Churn", "Fire"])
def test_a_lookalike_of_a_learning_tag_is_refused_everywhere(g, tag):
    rid, _ = db.add_row(g, "R", "A rule", "text")
    with pytest.raises(db.LedgerError):
        db.tag(g, rid, [tag])
    with pytest.raises(db.LedgerError):
        db.add_row(g, "R", "Another rule", "other", tags=[tag])
    rec = syncrec.validate_record({"id": "R50", "uid": "u" * 16, "kind": "R", "created": "2026-01-01T00:00:00Z",
                                   "updated": "2026-01-01T00:00:00Z", "rev": 0, "deleted": False, "device": "x",
                                   "data": {"summary": "s", "body": "", "alias": None, "source": "", "session": ""},
                                   "tags": [tag], "links": []})
    assert rec is None or tag not in rec["tags"]


@pytest.mark.parametrize("tag", [*db.LEARNING_TAGS, *(p + "x" for p in db.LEARNING_TAG_PREFIXES),
                                 "PRINCIPLE", "serves:​x"])
def test_every_learning_tag_is_refused_from_row_add_row_tag_save_and_the_local_mcp(wikis, tag):
    with pytest.raises((ledger.CommandError, db.LedgerError)):
        ledger.row_main(["add", "R", "A rule worth keeping, stated plainly.", "--scope", "repo", "--tag", tag,
                         "--cwd", str(wikis.repo)])
    c = wikis.conn()
    rid, _ = db.add_row(c, "R", "An ordinary rule about something else.", "", source="agent")
    c.close()
    with pytest.raises((ledger.CommandError, db.LedgerError)):
        ledger.row_main(["tag", rid, tag, "--scope", "repo", "--cwd", str(wikis.repo)])
    assert "error" in mcp.tool_save({"scope": "repo", "type": "rule", "summary": "A rule.", "body": "Because.",
                                     "tags": [tag]}, str(wikis.repo))
    assert "error" in save.save_row(wikis.wiki, "repo", "rule", "Another rule.", "Because.", [tag])
    assert not any(t for r in rows(wikis.wiki, "R") for t in r.tags if t.lower().startswith(tag.lower()[:5]))


def test_a_principle_with_its_counters_round_trips_through_sync_validation(wikis, g):
    rid, _ = db.add_row(g, "R", "Install packages with npm inside a worktree.", "Why: the lock file.",
                        tags=PRINCIPLE_TAGS, source="learn")
    wire = json.loads(json.dumps({**dbsync.snapshot(g)[rid], "rev": 1, "device": "other"}))
    rec = syncrec.validate_record(wire)
    assert rec is not None and set(rec["tags"]) == set(PRINCIPLE_TAGS)
    other = wikis.conn()  # another machine's wiki: the validated record applies as it arrived
    try:
        with db.write(other):
            dbsync.upsert(other, rec)
        assert tags(other, rid) == set(PRINCIPLE_TAGS)
        assert indexed(other) == recount(other)
    finally:
        other.close()


# --- 2b: session start, in the contract's order --------------------------------------------------

def test_session_start_orders_the_central_parts_then_the_learned_context(wikis, g):
    from shapa import focus, stages

    focus.set_northstar(g, "One brain for every device")
    focus.set_fire(g, 1, text="Ship the central brain")
    rid, _ = db.add_row(g, "R", "Write plainly", "Short words.", tags=["stage:0"], privileged=True)
    skills.add_version(g, "writing", "Plain writing", "SKILL BODY", new=True, active=True, privileged=True)
    stages.set_stage(g, "skill:writing", 0)
    db.add_row(g, "M", "Operator likes diffs small", "", tags=["review"])
    projectfile.new_project(g, "alpha", "Alpha")
    tasks.add_task(g, "Pay the invoice", "human", standalone_only=True)
    g.close()
    learned(wikis)
    correct(wikis.repo, HEADER, now=at(1))
    text = bootstrap.build_context(start=wikis.repo)
    marks = ["PRECEDENCE:", "northstar [global]", "fires [global]", "BASELINE (", "BASELINE SKILLS", "TOPICS:",
             "PROJECTS:", "The operator's open tasks: 1", f"{learn.HEAD_NOTE}:", "[global R2] ",
             "1 correction awaits learning", "WRITE BACK"]
    spots = [text.index(m) for m in marks]
    assert spots == sorted(spots), dict(zip(marks, spots))
    assert text.count("</shapa-memory>") == 1


def test_the_learning_switch_the_stage_and_the_budget_still_work(wikis, g, monkeypatch):
    from shapa import focus

    focus.set_northstar(g, "One brain for every device")
    db.add_row(g, "R", "Stage one rule", "A body.", tags=["stage:1"], privileged=True)
    learned(wikis)
    with_learning = bootstrap.build_context(start=wikis.repo)
    assert learn.HEAD_NOTE in with_learning and "Stage one rule" not in with_learning
    assert "Stage one rule" in bootstrap.build_context(start=wikis.repo, stage=1)
    assert "shapa session bootstrap" in bootstrap.build_context(start=wikis.repo, full=True)
    monkeypatch.setenv("SHAPA_LEARNING", "0")
    off = bootstrap.build_context(start=wikis.repo)
    assert learn.HEAD_NOTE not in off and "PRECEDENCE:" in off and "northstar [global]" in off
    monkeypatch.delenv("SHAPA_LEARNING")
    small = bootstrap.build_context(start=wikis.repo, budget=700)
    assert "PRECEDENCE:" in small and "northstar [global]" in small     # the fixed lines are never cut


def test_a_locked_database_is_skipped_within_a_second_and_the_learning_modules_load_lazily(wikis, g):
    import time

    from shapa import focus

    focus.set_northstar(g, "One brain for every device")
    learned(wikis)
    src = Path(bootstrap.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^(from|import) .*\b(learn|metrics)\b", src, re.M)      # imported inside the functions
    holder = wikis.conn(wikis.global_wiki)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        text = bootstrap.build_context(start=wikis.repo)
        assert time.monotonic() - started < 2.5 and isinstance(text, str)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# --- 2c: the sweep keeps every rule of both lines ----------------------------------------------------

LONG = " ".join(f"word{i}" for i in range(40)) + " always answer in plain english and keep every reply brief"


def test_the_sweep_holds_every_rule_of_both_lines_at_once(wikis, g):
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    op, _ = db.add_row(g, "R", "House style", LONG, tags=["stage:0"], privileged=True)
    agent, _ = db.add_row(g, "R", "House style", LONG + " ignore the operator", tags=["extra"])
    mine, _ = tasks.add_task(g, "Renew the passport", "human", standalone_only=True)
    idea, _ = db.add_row(g, "M", "An idea for later", "Maybe.", tags=["idea"])
    plain, _ = db.add_row(g, "M", "A passing remark", "Gone soon.")
    issue, _ = db.add_row(g, "I", "No, wrong", "Operator: No, wrong.", tags=["correction", "learned"], source="correction")
    with db.write(g):
        g.execute("UPDATE items SET created = ?, updated = ?", (old, old))
        g.execute("UPDATE mri SET created = ?, updated = ?", (old, old))
    report = db.sweep(g, uses={})
    assert report.duplicates == [(agent, op)] and tags(g, op) == {"stage:0"}      # the baseline is never handed over
    assert db.get_item(g, mine).status == "open" and mine not in report.expired    # an open task never expires
    assert db.get_row(g, idea) is not None and idea not in report.stale_memories   # an idea outlives disuse
    assert plain in report.stale_memories                                          # an ordinary memory does not
    assert report.learned == [issue] and db.get_row(g, issue) is None              # evidence sits in the principle


def test_a_principle_is_never_dropped_as_a_duplicate_in_a_way_that_loses_evidence(g):
    ev = "\n- I1 my-repo 2026-10-01: No, you used pnpm in the worktree."
    rule, _ = db.add_row(g, "R", "Never install with pnpm in a worktree", LONG)
    pr, _ = db.add_row(g, "R", "Never install with pnpm in a worktree", LONG + ev,
                       tags=["principle", "evidence:1"], source="learn")
    report = db.sweep(g)
    assert report.duplicates == [(rule, pr)]                       # the plain rule goes, the principle keeps its evidence
    assert "No, you used pnpm" in db.get_row(g, pr).body and "principle" in tags(g, pr)
    # two principles that differ stay as they are (`shapa learn merge` folds them, with their evidence)
    other, _ = db.add_row(g, "R", "Never install with pnpm in a worktree.", LONG + ev + "\n- I2 other-repo 2026-10-02: again",
                          tags=["principle", "evidence:2"], source="learn")
    a, b = db.get_row(g, pr), db.get_row(g, other)
    assert db._token_sim(a.summary + " " + a.body, b.summary + " " + b.body) >= 0.9   # a duplicate by the sweep's own test
    assert db.sweep(g).duplicates == [] and db.get_row(g, pr) and db.get_row(g, other)
    # and a principle is not handed to the baseline's text either
    base, _ = db.add_row(g, "R", "Another house style", LONG + " one", tags=["stage:0"], privileged=True)
    pr2, _ = db.add_row(g, "R", "Another house style", LONG + " one two", tags=["principle", "evidence:1"], source="learn")
    a, b = db.get_row(g, base), db.get_row(g, pr2)
    assert db._token_sim(a.summary + " " + a.body, b.summary + " " + b.body) >= 0.9
    assert db.sweep(g).duplicates == [] and db.get_row(g, pr2) is not None and tags(g, base) == {"stage:0"}


def test_a_duplicate_merge_leaves_one_counter_tag_and_no_stage_marker(g):
    older, _ = db.add_row(g, "I", "No, the header is wrong", "Operator: no.", tags=["correction", "violations:2", "repeats:1"])
    with db.write(g):
        g.execute("INSERT INTO mri(id, kind, summary, body, hash, created, updated) SELECT 'I99', 'I', summary, body, "
                  "hash, '2999-01-01T00:00:00Z', '2999-01-01T00:00:00Z' FROM mri WHERE id = ?", (older,))
        g.execute("INSERT INTO tags(id, tag) VALUES ('I99', 'violations:3'), ('I99', 'repeats:2'), ('I99', 'stage:4'), "
                  "('I99', ?)", (f"repeat-of:{older}",))
    assert db.sweep(g).duplicates == [("I99", older)]
    t = tags(g, older)
    assert {x for x in t if x.startswith("violations:")} == {"violations:5"}
    assert {x for x in t if x.startswith("repeats:")} == {"repeats:3"}
    assert not any(x.startswith("stage") for x in t) and f"repeat-of:{older}" not in t
    assert indexed(g) == recount(g)


# --- 2d: the schema from any starting file -------------------------------------------------------

TABLES = {"metrics_daily", "tag_counts", "docs", "tombstones", "uids"}
INDEXES = {"mri_kind", "items_kind_status_created", "items_alias", "items_title", "tag_counts_n"}
TRIGGERS = {"tag_counts_ai", "tag_counts_ad", "tag_counts_au"}


def _names(path, kind):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}
    finally:
        c.close()


def _stmts(text, drop=()):
    return [s for s in text.split(";") if s.strip() and not any(d in s for d in drop)]


def _file(tmp, name, statements, fill=True):
    path = Path(tmp) / name / db.DB_FILENAME
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    for s in statements:
        c.execute(s)
    if fill:
        c.execute("INSERT INTO meta(key, value) VALUES ('schema', '1')")
        c.execute("INSERT INTO counters(kind, next) VALUES ('R', 2)")
        c.execute("INSERT INTO mri(id, kind, summary, body, hash, created, updated) VALUES "
                  "('R1', 'R', 'A rule', 'b', 'h', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
        c.execute("INSERT INTO tags(id, tag) VALUES ('R1', 'alpha'), ('R1', 'violations:2'), ('R1', 'principle')")
    c.commit()
    c.close()
    return path.parent


@pytest.mark.parametrize("start", ["fresh", "base", "learning-alone", "central-alone"])
def test_both_lines_schema_builds_on_a_fresh_file_the_base_file_and_either_line_alone(tmp_path, start):
    base = _stmts(db._SCHEMA)
    if start == "fresh":
        root = tmp_path / "fresh"
        root.mkdir()
        db.connect(root, create=True).close()
    elif start == "base":
        root = _file(tmp_path, start, base)
    elif start == "learning-alone":   # schema 2 with metrics_daily and none of the central brain's index objects
        root = _file(tmp_path, start, base + _stmts(db._SCHEMA_V2, drop=("tag_counts", *INDEXES)))
    else:                             # schema 2 with the tag index and its triggers, no metrics_daily
        root = _file(tmp_path, start, base + _stmts(db._SCHEMA_V2, drop=("metrics_daily",)) + list(db._TRIGGERS))
    conn = db.connect(root)
    conn.close()
    path = root / db.DB_FILENAME
    assert TABLES <= _names(path, "table") and INDEXES <= _names(path, "index") and TRIGGERS <= _names(path, "trigger")
    conn = db.connect(root)
    try:
        assert indexed(conn) == recount(conn)
        assert db._is_current(conn)
        rid, _ = db.add_row(conn, "R", "Written after the upgrade", "x", tags=["alpha"])
        assert indexed(conn) == recount(conn)
    finally:
        conn.close()


def test_fifty_counter_bumps_leave_the_tag_index_equal_to_a_recount(wikis, g):
    rid, _ = db.add_row(g, "R", "A principle", "body", tags=["principle", "evidence:1", "alpha"], source="learn")
    for i in range(50):
        learn._bump(g, rid, "violations:")
        learn._set_tags(g, rid, {"last-seen:": f"2026-11-13T10:{i:02d}:00Z", "evidence:": i + 2})
        db.retag(g, rid, add=[f"revisions:{i}"], drop_prefix="revisions:")
    assert tags(g, rid) >= {"violations:50", "evidence:51", "revisions:49", "last-seen:2026-11-13T10:49:00Z"}
    assert len([t for t in tags(g, rid) if t.startswith("violations:")]) == 1
    assert indexed(g) == recount(g)
    with db.write(g):  # a merge repoints tags with UPDATE OR IGNORE, the sync replaces a record's tags whole
        g.execute("UPDATE OR IGNORE tags SET tag = 'repeat-of:R9' WHERE tag = 'alpha'")
        g.execute("DELETE FROM tags WHERE id = ?", (rid,))
        g.execute("INSERT OR IGNORE INTO tags(id, tag) VALUES (?, 'x'), (?, 'y')", (rid, rid))
    assert indexed(g) == recount(g)


# --- 2e: learn apply into the global wiki ----------------------------------------------------------

def test_a_principle_passes_the_central_brains_tag_rules_and_is_an_ordinary_rule(wikis, g):
    from shapa import stages

    res = learned(wikis)
    rid = res["target"]
    t = tags(g, rid)
    db.check_tag_spelling(t)
    db.check_stage_tags(t, "R")
    assert len(t) <= db.MAX_TAGS and "principle" in t
    stages.set_stage(g, rid, 3)                                         # `shapa stage set` takes it like any rule
    assert "stage:3" in tags(g, rid) and "principle" in tags(g, rid)
    assert db.get_row(g, rid).kind == "R" and any(x["tag"] == "principle" for x in relate.tag_index(g))
    assert relate.related(g, rid) is not None


def test_a_principle_carrying_a_project_links_to_it_through_every_counter_bump(wikis, g):
    projectfile.new_project(g, "alpha", "Alpha")
    res = learned(wikis)
    rid = res["target"]
    db.tag(g, rid, ["project:alpha"], privileged=True)
    doc = db.project_doc_id(g, "alpha")
    assert (rid, doc, "about") in db.links_of(g, rid)
    for _ in range(5):
        learn._bump(g, rid, "violations:")
    db.retag(g, rid, add=["last-seen:2026-11-13T10:00:00Z"], drop_prefix="last-seen:")
    assert (rid, doc, "about") in db.links_of(g, rid) and "project:alpha" in tags(g, rid)


def test_a_merge_that_would_pass_the_100_tag_cap_changes_nothing(wikis, g):
    rid, _ = db.add_row(g, "R", "Install packages with npm inside a worktree, never with pnpm.", "body",
                        tags=[f"topic-{i}" for i in range(99)])
    correct(wikis.repo, PNPM, now=at(0))
    before = db.get_row(g, rid).body
    with pytest.raises(db.LedgerError, match="at most"):
        learn.apply(wikis.repo, "I1", PNPM_RULE, into=rid, now=at(0, 1))
    assert db.get_row(g, rid).body == before and len(tags(g, rid)) == 99


def test_a_baseline_rule_is_not_rewritten_by_a_principle_and_a_violation_still_files(wikis, g):
    res = learned(wikis)
    rid = res["target"]
    db.retag(g, rid, add=["stage:0"], privileged=True)
    correct(wikis.repo, HEADER, now=at(1))
    with pytest.raises(db.LedgerError, match="baseline"):
        learn.apply(wikis.repo, "I2", HEADER_RULE, into=rid, now=at(1, 1))
    _, info = correct(wikis.repo, "Wrong again, you ran pnpm install in the worktree, I said use npm install.", now=at(2))
    assert info["repeat"] and info["violation"]                        # the correction is filed and checked
    assert "repeat-checked" in {t for r in rows(wikis.wiki, "I") for t in r.tags}
    assert "stage:0" in tags(g, rid) and "violations:1" not in tags(g, rid)    # the baseline's tags are the operator's


# --- 2f: uprooting a skill beside learned rules ---------------------------------------------------

def test_uproot_moves_the_skill_with_the_pending_version_a_learned_rule_filed_and_leaves_the_rule(wikis, g):
    repo = wikis.conn()
    skills.add_version(repo, "worktrees", "worktree habits", "# Worktrees\n\nCreate one per feature.\n", new=True,
                       active=True, privileged=True)
    repo.close()
    correct(wikis.repo, PNPM, now=at(0))
    res = learn.apply(wikis.repo, "I1", PNPM_RULE, into="skill:worktrees", now=at(0, 1))
    assert res["wiki"] == "repo" and res["scope"] == "skill:worktrees"
    repo = wikis.conn()
    try:
        rule = next(r for r in db.rows(repo, "R") if "principle" in r.tags)
        assert "skill:worktrees" in rule.tags
        before = [(v.version, v.status, v.body) for v in skills.versions(repo, "worktrees")]
        assert [(v, s) for v, s, _ in before] == [(1, "active"), (2, "pending")]
        out = skills.uproot(repo, g, "worktrees")
        assert out["moved"] == 2 and not out["into_existing"]
        assert [(v.version, v.status, v.body) for v in skills.versions(g, "worktrees")] == before   # the pending version intact
        assert skills.versions(repo, "worktrees") == []
        kept = db.get_row(repo, rule.id)                                # the rule stays in the repo wiki, tags and all
        assert kept is not None and {"principle", "skill:worktrees"} <= set(kept.tags)
    finally:
        repo.close()
    assert [r for r in rows(wikis.global_wiki, "R") if "principle" in r.tags] == []


# --- 2g: the commands ----------------------------------------------------------------------------

def test_the_help_lists_every_command_of_both_lines_and_skill_uproot_is_reachable():
    out = subprocess.run([sys.executable, "-m", "shapa", "--help"], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1]).stdout
    for cmd in ("learn", "metrics", "stage", "tags", "related", "tasks", "project", "skill", "uproot"):
        assert re.search(rf"^\s+{cmd}\b", out, re.M) or f"{cmd} " in out, cmd
    with pytest.raises(SystemExit) as exc:
        skills.main(["uproot", "--help"])
    assert exc.value.code == 0


def test_the_local_mcp_has_the_central_brains_tools_and_still_refuses_the_learning_tags(wikis):
    names = {t["name"] for t in mcp.TOOLS}
    assert {"tags", "related", "tasks", "projects", "project", "project_new", "project_add"} <= names
    assert {"search", "get", "save", "placement", "focus", "skill", "partition"} <= names
    for tag in ("principle", "violations:2", "serves:northstar"):
        assert "error" in mcp.tool_save({"scope": "repo", "type": "rule", "summary": "A rule.", "body": "b",
                                         "tags": [tag]}, str(wikis.repo))
