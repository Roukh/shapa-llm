"""The personal-data gate on every write path (shapa/pii.py wired into db, save, mcp, capture, syncrec).

Unprivileged writers (an MCP tool, a hook, a sync pull) are refused every category; the operator's CLI is
refused only the severe ones; anything going to stage 0 is refused every category, whoever writes it."""

import json

import pytest

from shapa import capture, db, mcp, pii, save, stages, syncrec, validate

EMAIL = "write to jane.roe@gmail.com"
CARD = "card 4111 1111 1111 1111"
SECRET = "sk-ant-api03-Q7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7x"


@pytest.fixture
def g(wikis):
    c = wikis.conn(wikis.global_wiki)
    yield c
    c.close()


def refused(fn, *args, **kw):
    with pytest.raises(db.PiiRejected) as exc:
        fn(*args, **kw)
    return str(exc.value)


# --- the database layer ---------------------------------------------------------------------------------

def test_an_unprivileged_row_write_is_refused_and_the_message_never_holds_the_value(g):
    msg = refused(db.add_row, g, "M", "Contact", EMAIL)
    assert "email" in msg and "jane" not in msg and "gmail" not in msg
    assert db.counts(g)["M"] == 0
    assert "secret" in refused(db.add_row, g, "M", f"key {SECRET}", "body")  # refused, not masked into a row


def test_the_operator_cli_may_store_an_email_but_never_the_severe_categories(g):
    rid, _ = db.add_row(g, "M", "Contact", EMAIL, privileged=True)
    assert db.get_row(g, rid).body == EMAIL
    for text, cat in ((CARD, "card"), ("dob: 1990-05-04", "dob"), ("passport number: A12345678", "gov-id"),
                      ("I have been diagnosed with anxiety", "health"), ("ssn 123-45-6789", "ssn"),
                      ("-----BEGIN PGP PRIVATE KEY BLOCK-----", "private-key")):
        assert pii.LABELS[cat] in refused(db.add_row, g, "M", "Note", text, privileged=True)


def test_anything_going_to_stage_zero_is_refused_every_category_whoever_writes_it(g):
    assert "email" in refused(db.add_row, g, "R", "Rule", EMAIL, tags=["stage:0"], privileged=True)
    assert "email" in refused(db.add_row, g, "R", "Rule", EMAIL, tags=["stage-proposed:0"])  # an app's proposal too
    assert "secret" in refused(db.add_row, g, "R", f"Rule {SECRET}", "", tags=["stage:0"], privileged=True)
    rid, _ = db.add_row(g, "R", "A clean rule", "kept plainly", tags=["stage:0"], privileged=True)
    assert rid


def test_a_stored_row_with_an_email_cannot_be_moved_into_stage_zero(g, wikis, capsys):
    rid, _ = db.add_row(g, "R", "Contact rule", EMAIL, privileged=True)  # the operator's own row, allowed
    assert "email" in refused(db.retag, g, rid, add=["stage:0"], privileged=True)
    assert "email" in refused(db.tag, g, rid, ["stage:0"], privileged=True)
    with pytest.raises(db.PiiRejected):
        stages.set_stage(g, rid, 0)
    assert "stage:0" not in db._tags_of(g, rid)
    db.retag(g, rid, add=["stage:3"], privileged=True)  # any other stage is not the baseline


def test_an_edit_is_screened_like_a_write_and_a_stage_zero_row_is_screened_in_full(g):
    rid, _ = db.add_row(g, "M", "Plain", "text")
    assert "email" in refused(db.update_row, g, rid, body=EMAIL)
    db.update_row(g, rid, body=EMAIL, privileged=True)
    base, _ = db.add_row(g, "R", "Baseline", "text", tags=["stage:0"], privileged=True)
    assert "email" in refused(db.update_row, g, base, body=EMAIL, privileged=True)


def test_documents_and_ledger_items_are_screened_too(g):
    assert "email" in refused(db.add_doc, g, "Notes", EMAIL)
    d = db.add_doc(g, "Notes", EMAIL, privileged=True)
    assert "email" in refused(db.update_doc, g, d, body=EMAIL + " again")
    assert "email" in refused(db.add_item, g, "F", "Feature", body=EMAIL)
    f = db.add_item(g, "F", "Feature", body=EMAIL, privileged=True)
    assert "card" in refused(db.update_item, g, f, body=CARD, privileged=True)
    assert "email" in refused(db.add_row, g, "M", "Tagged", "ok", tags=["jane.roe@gmail.com"])  # a value in a tag


def test_a_value_split_across_summary_and_body_is_still_one_value(g):
    assert "card" in refused(db.add_row, g, "M", "card 4111 1111", "1111 1111")


def test_the_migration_helper_moves_old_data_unscreened(g):
    with db.unscreened():
        rid, _ = db.add_row(g, "M", "Old", EMAIL)
    assert db.get_row(g, rid)
    assert "email" in refused(db.add_row, g, "M", "New", EMAIL)  # and only inside the block


# --- save, the MCP tool, the hooks ------------------------------------------------------------------------

def test_save_row_returns_the_refusal_and_the_warnings(wikis):
    out = save.save_row(wikis.wiki, "repo", "memory", "Contact", EMAIL, [])
    assert "error" in out and "email" in out["error"] and "jane" not in out["error"]
    ok = save.save_row(wikis.wiki, "repo", "memory", "Server", "runs at 93.184.216.34", [])
    assert "id" in ok and len(ok["warnings"]) == 1 and "93.184" not in ok["warnings"][0]
    cli = save.save_row(wikis.wiki, "repo", "memory", "Contact", EMAIL, [], privileged=True)
    assert "id" in cli  # the operator's CLI


def test_the_local_mcp_save_refuses_every_category_and_stage_zero(wikis):
    out = mcp.tool_save({"scope": "repo", "type": "memory", "summary": "Contact", "body": EMAIL}, str(wikis.repo))
    assert "error" in out and "email" in out["error"]
    out = mcp.tool_save({"scope": "repo", "type": "memory", "summary": "Key", "body": f"use {SECRET}"}, str(wikis.repo))
    assert "error" in out and "secret" in out["error"]
    out = mcp.tool_save({"scope": "repo", "type": "human_task", "summary": f"Call 415-555-2671", "body": ""},
                        str(wikis.repo))
    assert "error" in out and "phone" in out["error"]
    out = mcp.tool_save({"scope": "repo", "type": "memory", "summary": "Fine", "body": "nothing personal"},
                        str(wikis.repo))
    assert "id" in out


def test_a_note_path_and_tags_are_screened(wikis):
    out = mcp.tool_save({"scope": "repo", "type": "memory", "summary": "Fine", "body": "ok",
                         "tags": ["jane.roe@gmail.com"]}, str(wikis.repo))
    assert "error" in out and "email" in out["error"]


def test_the_capture_hook_drops_a_candidate_that_holds_personal_data(wikis):
    def msg(text):
        return json.dumps({"message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    t = wikis.tmp / "t.jsonl"
    t.write_text("\n".join([msg("From now on always email jane.roe@gmail.com the weekly report."),
                            msg("From now on always keep scrap notes under the feature.")]) + "\n")
    capture.capture_session(str(t), "sessPII00001", root=wikis.wiki)
    c = wikis.conn()
    try:
        texts = " ".join(r.summary + r.body for r in db.rows(c))
    finally:
        c.close()
    assert "jane.roe" not in texts and "scrap notes" in texts


# --- a record pulled from the server ------------------------------------------------------------------------

def wire(data, tags=()):
    return {"id": "M50", "uid": "u" * 16, "kind": "M", "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T00:00:00Z", "rev": 1, "deleted": False, "device": "d",
            "data": {"summary": "s", "body": "", "source": "", "session": "", **data}, "tags": list(tags), "links": []}


def test_a_pulled_record_with_a_severe_category_is_dropped_and_one_with_an_email_is_the_operators(g):
    assert syncrec.validate_record(wire({"body": CARD})) is None
    assert syncrec.validate_record(wire({"body": "ssn 123-45-6789"})) is None
    assert syncrec.validate_record(wire({"body": EMAIL})) is not None  # the operator's own device may push it
    assert syncrec.validate_record(wire({"body": EMAIL}, tags=["stage:0"])) is None  # stage 0: every category
    assert syncrec.validate_record(wire({"body": f"k {SECRET}"}, tags=["stage:0"])) is None


# --- validate --pii -------------------------------------------------------------------------------------------

def test_validate_pii_lists_id_and_category_only(wikis, capsys):
    c = wikis.conn()
    db.add_row(c, "M", "Contact", EMAIL, privileged=True)
    ok, _ = db.add_row(c, "M", "Clean", "nothing here")
    c.close()
    with pytest.raises(SystemExit) as exc:
        validate.main(["--pii", str(wikis.wiki)])
    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "M1: email" in out and ok not in out and "jane" not in out and "1 record(s)" in out


def test_validate_pii_exits_zero_on_a_clean_wiki(wikis, capsys):
    c = wikis.conn()
    db.add_row(c, "M", "Clean", "nothing here")
    c.close()
    with pytest.raises(SystemExit) as exc:
        validate.main(["--pii", str(wikis.wiki)])
    assert exc.value.code == 0 and "0 record(s)" in capsys.readouterr().out


# --- the gate itself ------------------------------------------------------------------------------------------

def test_scan_wiki_reads_only(wikis):
    c = wikis.conn()
    db.add_row(c, "M", "Contact", EMAIL, privileged=True)
    c.close()
    before = db.db_path(wikis.wiki).read_bytes()
    assert pii.scan_wiki(wikis.wiki) == [("M1", ["email"])]
    assert db.db_path(wikis.wiki).read_bytes() == before
