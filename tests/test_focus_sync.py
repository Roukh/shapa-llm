"""The focus record, skills and project documents ride on plain documents, so they
sync like any record; the store's lean gate keeps one document per project."""

import pytest

from shapa import db, focus, skills
from tests.test_sync import Device, KEY, a, b, double  # noqa: F401  (fixtures)


def test_the_northstar_and_fires_sync_to_another_device(a, b):
    c = a.conn()
    f = db.add_item(c, "F", "Hosted API")
    focus.set_northstar(c, "One brain everywhere")
    focus.set_fire(c, 1, feature=f)
    c.close()
    a.sync()
    b.sync()
    c = b.conn()
    got = focus.load(c)
    assert got.northstar == "One brain everywhere" and got.fires[0].feature == f
    c.close()


def test_closing_a_fire_feature_on_one_device_moves_the_slot_on_the_other(a, b):
    c = a.conn()
    f1, f2, f3, f4 = (db.add_item(c, "F", f"Feature {i}") for i in range(4))
    for slot, f in enumerate((f1, f2, f3), 1):
        focus.set_fire(c, slot, feature=f)
    c.close()
    a.sync()
    b.sync()
    c = b.conn()
    db.close(c, f2, "merge")  # promotes f4 on this device
    c.close()
    b.sync()
    a.sync()
    c = a.conn()
    assert [x.feature for x in focus.load(c).fires] == [f1, f3, f4]
    c.close()


def test_skill_versions_and_activation_sync(a, b):
    c = a.conn()
    skills.add_version(c, "tdd", "Test first.", "v1", new=True, active=True)
    skills.add_version(c, "tdd", "Test first.", "v2")
    c.close()
    a.sync()
    b.sync()
    c = b.conn()
    assert [(v.version, v.status) for v in skills.versions(c, "tdd")] == [(1, "active"), (2, "pending")]
    skills.activate(c, "tdd")
    c.close()
    b.sync()
    a.sync()
    c = a.conn()
    assert [(v.version, v.status) for v in skills.versions(c, "tdd")] == [(1, "previous"), (2, "active")]
    c.close()


def test_a_lesson_filed_on_one_device_arrives_pending_on_the_other(a, b):
    c = a.conn()
    skills.add_version(c, "tdd", "Test first.", "base", new=True, active=True)
    db.add_row(c, "I", "Skipped the red step", "run it red first", tags=["skill:tdd"])
    c.close()
    a.sync()
    b.sync()
    c = b.conn()
    vs = skills.versions(c, "tdd")
    assert [(v.version, v.status) for v in vs] == [(1, "active"), (2, "pending")]
    assert len(vs) == 2  # the pull never re-derives a lesson
    assert skills.get(c, "tdd").version == 1
    c.close()


def test_two_devices_writing_a_project_document_offline_both_sync_then_get_folded(a, b, double):
    """Sync never refuses a stored document (older clients and first logins are grandfathered), so
    a race leaves two: `doc list` flags the project and `doc merge` folds them."""
    c = a.conn()
    db.add_doc(c, "Principles", "from device a")
    c.close()
    c = b.conn()
    db.add_doc(c, "Principles too", "from device b")
    c.close()
    assert not a.sync().refused
    assert not b.sync().refused
    c = b.conn()
    assert db.project_overages(c) == {"": [d.id for d in db.documents(c)]}
    keep = [d.id for d in db.documents(c) if d.body == "from device a"][0]
    mine = [d.id for d in db.documents(c) if d.body == "from device b"][0]
    db.merge_docs(c, mine, keep)
    c.close()
    b.sync()
    a.sync()
    c = a.conn()
    assert len(db.documents(c)) == 1 and "from device b" in db.documents(c)[0].body
    c.close()


def test_focus_records_and_skills_are_exempt_from_the_gate(a, b, double):
    c = a.conn()
    db.add_doc(c, "Principles", "the one")
    focus.set_northstar(c, "north")
    skills.add_version(c, "s1", "d", "b", new=True, active=True)
    skills.add_version(c, "s2", "d", "b", new=True, active=True)
    c.close()
    res = a.sync()
    assert not res.refused
    assert len([r for r in double.live(KEY).values() if r["kind"] == "D"]) == 4


def test_a_subproject_document_is_a_different_project(a, b, double):
    c = a.conn()
    db.add_doc(c, "Principles", "the one")
    db.add_doc(c, "Hosted", "sub", project="hosted")
    c.close()
    assert not a.sync().refused
