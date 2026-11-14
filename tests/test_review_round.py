"""The review round on F17: split-brain focus, grandfathered documents, project key
format, the tag gate, the session-start budget, rank tags, and skill activation."""

import io
from contextlib import redirect_stdout

import pytest

from shapa import bootstrap, db, docs, focus, mcp, registry, skills
from tests.test_sync import Device, KEY, a, b, double  # noqa: F401  (fixtures)


def doc_cli(argv, cwd):
    out = io.StringIO()
    with redirect_stdout(out):
        code = docs.run_doc(argv + ["--cwd", str(cwd)])
    return code, out.getvalue()


# --- 1. one focus record per wiki, whoever creates it ---------------------------------------------

def test_two_devices_that_each_set_a_northstar_offline_end_up_with_one_record(a, b, double):
    c = a.conn()
    focus.set_northstar(c, "north from a")
    c.close()
    c = b.conn()
    focus.set_northstar(c, "north from b")
    c.close()
    assert a.sync().status == "ok"
    res = b.sync()
    assert not res.refused  # the same record, not a second one the server must turn away
    a.sync()
    live = [r for r in double.live(KEY).values() if r["kind"] == "D"]
    assert len(live) == 1
    for dev in (a, b):
        c = dev.conn()
        assert len(db.documents(c)) == 1
        c.close()


def test_later_edits_on_the_second_device_still_sync(a, b, double):
    for dev, text in ((a, "from a"), (b, "from b")):
        c = dev.conn()
        focus.set_northstar(c, text)
        c.close()
    a.sync()
    b.sync()
    a.sync()
    c = b.conn()
    focus.set_northstar(c, "b edits again")
    c.close()
    assert not b.sync().refused
    a.sync()
    c = a.conn()
    assert focus.load(c).northstar == "b edits again"
    c.close()


def test_the_focus_record_has_one_fixed_uid_in_every_wiki(wikis):
    g, r = wikis.conn(wikis.global_wiki), wikis.conn()
    focus.set_northstar(g, "g")
    focus.set_northstar(r, "r")
    assert db.uid_of(g, db.get_doc(g, "focus").id) == db.uid_of(r, db.get_doc(r, "focus").id)
    g.close()
    r.close()


# --- 2. documents already stored are grandfathered ---------------------------------------------------

def make_legacy_pair(dev):
    """Two documents for one project, as an older client allowed."""
    c = dev.conn()
    db.add_doc(c, "Principles", "first")
    with db.write(c):
        c.execute("INSERT INTO docs(id, title, body, hash, created, updated) VALUES "
                  "('D2','Models','second','h','2026-01-02T00:00:00Z','2026-01-02T00:00:00Z')")
        c.execute("INSERT INTO counters(kind, next) VALUES ('D', 3) ON CONFLICT(kind) DO UPDATE SET next = 3")
    db.backfill_uids(c)
    c.close()


def test_documents_that_were_already_local_sync_as_before(a, b, double):
    make_legacy_pair(a)
    assert not a.sync().refused
    assert len([r for r in double.live(KEY).values() if r["kind"] == "D"]) == 2
    assert not b.sync().refused
    c = b.conn()
    assert len(db.documents(c)) == 2
    c.close()


def test_a_new_third_document_is_still_refused_locally(a):
    make_legacy_pair(a)
    c = a.conn()
    with pytest.raises(db.DocExists):
        db.add_doc(c, "Third", "x")
    c.close()


def test_doc_list_flags_a_project_over_the_limit(wikis):
    conn = wikis.conn()
    db.add_doc(conn, "Principles", "first")
    with db.write(conn):
        conn.execute("INSERT INTO docs(id, title, body, hash, created, updated) VALUES "
                     "('D2','Models','second','h','2026-01-02T00:00:00Z','2026-01-02T00:00:00Z')")
    conn.close()
    code, out = doc_cli(["list"], wikis.repo)
    assert code == 0 and "D1" in out and "D2" in out
    assert "over the limit" in out and "shapa doc merge" in out
    conn = wikis.conn()
    assert db.project_overages(conn) == {"": ["D1", "D2"]}
    db.merge_docs(conn, "D2", "D1")
    assert db.project_overages(conn) == {}
    conn.close()
    assert "over the limit" not in doc_cli(["list"], wikis.repo)[1]


# --- 3 and 4. a project key is exactly what clean_project makes of it ----------------------------------

BAD_KEYS = ["Foo", "fo​o", "ｆoo", "fo o", "", "a" * 90, "../x"]


@pytest.mark.parametrize("key", BAD_KEYS)
def test_tagging_a_document_with_a_malformed_project_key_is_refused(wikis, key):
    conn = wikis.conn()
    d = db.add_doc(conn, "Doc", "x", project="real")
    with pytest.raises(db.LedgerError):
        db.tag(conn, d, [f"project:{key}"])
    with pytest.raises(db.LedgerError):
        db.add_doc(conn, "Other", "y", tags=[f"project:{key}"])
    conn.close()


def test_tag_cannot_move_a_document_into_a_held_project(wikis):
    conn = wikis.conn()
    db.add_doc(conn, "Held", "x", project="held")
    other = db.add_doc(conn, "Other", "y", project="other")
    with pytest.raises(db.DocExists) as e:
        db.tag(conn, other, ["project:held"])
    assert "D1" in str(e.value)
    db.tag(conn, other, ["project:fresh"])  # a free project is fine
    db.tag(conn, other, ["note"])  # and so is any other tag
    conn.close()


# --- 5. the budget applies to the focused session start -------------------------------------------------

@pytest.fixture
def huge(wikis):
    for w in (wikis.wiki, wikis.global_wiki):
        registry.write_format(w)
    c = wikis.conn()
    fs = [db.add_item(c, "F", f"Feature {i} with a descriptive title that runs on a while") for i in range(3)]
    for i in range(3):
        for n in range(30):
            j = db.add_item(c, "J", f"Job {n} of feature {i}: change the thing in module {n} and its callers",
                            parent=fs[i], body="touches shapa/remote.py")
            for t in range(6):
                db.add_item(c, "T", f"Task {t} of job {n} with a title long enough to count", parent=j)
        focus.set_fire(c, i + 1, feature=fs[i])
    focus.set_northstar(c, "A repo northstar that is never cut")
    c.close()
    return wikis


def test_the_default_budget_bounds_the_focused_output(huge):
    text = bootstrap.build_context(start=huge.repo)
    assert len(text) <= bootstrap.DEFAULT_BUDGET
    assert "A repo northstar that is never cut" in text
    for n in (1, 2, 3):
        assert f"{n}. F" in text
    assert "more" in text  # what was left out is said


def test_a_smaller_budget_cuts_the_tree_never_the_northstar_or_fires(huge):
    big = bootstrap.build_context(start=huge.repo, budget=7200)
    small = bootstrap.build_context(start=huge.repo, budget=1)
    assert len(small) < len(big)
    assert "A repo northstar that is never cut" in small
    for n in (1, 2, 3):
        assert f"{n}. F" in small
    assert "</shapa-memory>" in small


def test_the_tree_is_cut_by_rank_first_jobs_of_the_first_fire(huge):
    text = bootstrap.build_context(start=huge.repo, budget=4000)
    assert len(text) <= 4000
    assert "Job 0 of feature 0" in text  # the first fire's tree survives longest
    assert "Task 5 of job 29 of" not in text


def test_a_bigger_budget_carries_more_of_the_tree(huge):
    mid = bootstrap.build_context(start=huge.repo, budget=5000)
    roomy = bootstrap.build_context(start=huge.repo, budget=40000)
    assert len(roomy) > len(mid) and len(roomy) <= 40000


# --- 6. rank tags are 1-999 --------------------------------------------------------------------------

@pytest.mark.parametrize("tag", ["rank:0", "rank:1000", "rank:-5", "rank:007", "rank:٣", "rank:", "rank:1.5",
                                 "rank: 7", "rank:+3"])
def test_a_malformed_rank_tag_means_unranked(wikis, tag):
    c = wikis.conn()
    a_, b_ = db.add_item(c, "F", "A"), db.add_item(c, "F", "B")
    db.retag(c, b_, add=[tag])
    assert focus.rank_of(c, b_) is None
    assert [i.id for i in focus.candidates(c)] == [a_, b_]  # unranked: oldest first
    c.close()


def test_a_valid_rank_still_counts(wikis):
    c = wikis.conn()
    a_, b_ = db.add_item(c, "F", "A"), db.add_item(c, "F", "B")
    focus.rank_feature(c, b_, 999)
    assert focus.rank_of(c, b_) == 999
    assert [i.id for i in focus.candidates(c)] == [b_, a_]
    for bad in (0, 1000, -1):
        with pytest.raises(db.LedgerError):
            focus.rank_feature(c, a_, bad)
    c.close()


# --- 8. an agent cannot activate through MCP ------------------------------------------------------------

def test_the_mcp_skill_tool_cannot_activate_anything(wikis):
    cwd = str(wikis.repo)
    assert "error" in mcp.tool_skill({"action": "activate", "scope": "repo", "name": "tdd"}, cwd)
    added = mcp.tool_skill({"action": "add", "scope": "repo", "name": "tdd", "description": "d", "body": "b"}, cwd)
    assert added["status"] == "pending"  # an agent's own new skill waits for the operator
    upd = mcp.tool_skill({"action": "update", "scope": "repo", "name": "tdd", "body": "b2", "active": True}, cwd)
    assert upd["status"] == "pending"  # the flag is ignored
    c = wikis.conn()
    assert skills.get(c, "tdd") is None and skills.get(c, "tdd", 2).version == 2 and all(v.status != "active" for v in skills.versions(c, "tdd"))
    skills.activate(c, "tdd", 1)  # the CLI path still activates
    c.close()
    tools = {t["name"]: t for t in mcp.TOOLS}
    assert "activate" not in tools["skill"]["inputSchema"]["properties"]["action"]["enum"]
    assert "active" not in tools["skill"]["inputSchema"]["properties"]
