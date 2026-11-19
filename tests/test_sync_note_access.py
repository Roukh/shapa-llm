"""A note's ``stage:0`` is the hosted store's access marker (a baseline device reads only the notes that carry it),
not the baseline: a pulled note keeps it, a local edit pushes it back, and it never counts as a stage-0 record."""

import pytest

from shapa import db, stages, syncrec
from tests.test_sync import KEY, Device, HostedDouble, note_rec, row_rec  # noqa: F401  (the fixtures are imported)
from tests.test_sync import a, b, double  # noqa: F401


def test_a_note_takes_only_the_access_marker_as_a_stage_tag():
    db.check_stage_tags(["stage:0"], "N")
    db.check_stage_tags(["project:p", "stage:0"], "N")
    for bad in (["stage:1"], ["stage-proposed:0"], ["stage:0", "stage:3"], ["stage:0", "stage-proposed:0"], ["stage:x"]):
        with pytest.raises(db.LedgerError):
            db.check_stage_tags(bad, "N")
    # unchanged for everything else: a document and a task still take no stage
    for letter in ("D", "T", "F", "J"):
        with pytest.raises(db.LedgerError):
            db.check_stage_tags(["stage:0"], letter)


def test_a_pulled_note_with_the_marker_is_accepted_and_one_with_another_stage_is_not():
    ok = syncrec.validate_record(note_rec("research/open.md", "hello\n", tags=["project:p", "stage:0"]))
    assert ok is not None and ok["tags"] == ["project:p", "stage:0"]
    for tags in (["stage:1"], ["stage-proposed:0"], ["stage:0", "stage:2"]):
        assert syncrec.validate_record(note_rec("research/bad.md", "x\n", tags=tags)) is None


def test_the_marker_survives_a_local_edit_and_both_directions_of_sync(a, b, double):
    a.sync()  # registers the wiki on the double
    double.remote_put(KEY, note_rec("research/open.md", "from the agent\n", tags=["project:p", "stage:0"], updated="2026-01-01T00:00:00Z"))
    res = a.sync()
    assert res.status == "ok"
    assert (a.wiki / "research" / "open.md").read_text() == "from the agent\n"
    # the note is a file: nothing about it lands in the database, so the baseline budget never sees it
    c = a.conn()
    try:
        assert stages.baseline_size(c) == 0
        assert c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        c.close()
    # the operator edits the file; the push carries the tags it was pulled with
    (a.wiki / "research" / "open.md").write_text("edited by the operator\n", encoding="utf-8")
    res = a.sync()
    assert res.status == "ok" and res.sent_put == 1
    live = double.live(KEY)["research/open.md"]
    assert live["data"]["body"] == "edited by the operator\n"
    assert live["tags"] == ["project:p", "stage:0"]
    # a second device pulls it with the marker, edits it and pushes it back the same
    b.sync()
    (b.wiki / "research" / "open.md").write_text("edited on b\n", encoding="utf-8")
    b.sync()
    assert double.live(KEY)["research/open.md"]["tags"] == ["project:p", "stage:0"]
    # a marker taken off on the server is taken off here too, and a later edit does not bring it back
    double.remote_put(KEY, note_rec("research/open.md", "edited on b\n", tags=["project:p"], updated="2026-06-01T00:00:00Z"))
    a.sync()
    (a.wiki / "research" / "open.md").write_text("after removal\n", encoding="utf-8")
    a.sync()
    assert double.live(KEY)["research/open.md"]["tags"] == ["project:p"]


def test_a_note_with_no_tags_on_the_server_pushes_none(a, double):
    (a.wiki / "plain.md").write_text("plain\n", encoding="utf-8")
    a.sync()
    assert double.live(KEY)["plain.md"]["tags"] == []
    (a.wiki / "plain.md").write_text("plain v2\n", encoding="utf-8")
    a.sync()
    assert double.live(KEY)["plain.md"]["tags"] == []
