"""The security-audit round on F17: reserved tags are never edited, a skill with no active
version is not served, and lessons cannot multiply storage."""

import pytest

from shapa import db, focus, skills


@pytest.fixture
def conn(wikis):
    c = wikis.conn()
    yield c
    c.close()


# --- 1. no tag edit touches the focus record or a skill version ---------------------------------

def test_tag_refuses_a_focus_record_and_a_skill_version(conn):
    focus.set_northstar(conn, "north")
    fid = db.get_doc(conn, "focus").id
    sk = skills.add_version(conn, "tdd", "d", "body", new=True, active=True)
    for target in (fid, sk.doc_id):
        for tags in (["note"], ["skill", "skill:evil", "v:99", "status:active"], ["project:x"]):
            with pytest.raises(db.LedgerError):
                db.tag(conn, target, tags)
    assert sorted(db.get_doc(conn, fid).tags) == ["focus"]
    assert skills.get(conn, "evil") is None


# --- 3. a skill with no active version is not served ---------------------------------------------

def test_a_skill_with_no_active_version_is_not_served_but_its_pending_one_is_read_by_number(conn):
    v = skills.add_version(conn, "tdd", "Test first.", "pending text", new=True)  # an agent's: pending
    assert v.status == "pending"
    assert skills.get(conn, "tdd") is None
    assert skills.get(conn, "tdd", 1).body.strip() == "pending text"
    assert skills.summaries(conn)[0]["active"] is None
    assert skills.summaries(conn)[0]["description"] == "Test first."


# --- 4. lessons are capped ------------------------------------------------------------------------

def test_a_row_names_at_most_three_skills(conn):
    for n in range(6):
        skills.add_version(conn, f"s{n}", "d", "base", new=True, active=True)
    db.add_row(conn, "I", "Many skills", "x", tags=[f"skill:s{n}" for n in range(6)])
    filed = [n for n in range(6) if len(skills.versions(conn, f"s{n}")) == 2]
    assert len(filed) == 3


def test_a_skill_holds_at_most_five_pending_versions(conn):
    skills.add_version(conn, "tdd", "d", "base", new=True, active=True)
    for n in range(9):
        db.add_row(conn, "I", f"Lesson number {n}", "details", tags=["skill:tdd"])
    vs = skills.versions(conn, "tdd")
    assert sum(1 for v in vs if v.status == "pending") == skills.MAX_PENDING
    assert len(vs) == 1 + skills.MAX_PENDING
    # activating one frees nothing by itself, but clearing the queue lets lessons through again
    for v in vs[1:]:
        db.retag(conn, v.doc_id, add=["status:previous"], drop_prefix="status:")
    db.add_row(conn, "I", "A later lesson", "details", tags=["skill:tdd"])
    assert len(skills.versions(conn, "tdd")) == 2 + skills.MAX_PENDING
