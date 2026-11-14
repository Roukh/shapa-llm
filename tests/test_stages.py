"""Stages (contract section 1): stage:N tags, the privileged stage 0, the baseline budget."""

import json

import pytest

from shapa import db, ledger, skills, stages, syncrec


@pytest.fixture
def g(wikis):
    """A connection to the global wiki."""
    c = wikis.conn(wikis.global_wiki)
    yield c
    c.close()


def cli(argv, wikis, capsys):
    code = None
    try:
        stages.main(["--cwd", str(wikis.repo), *argv])
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


def tags(conn, rid):
    return set(db._tags_of(conn, rid))


# --- tag rules -----------------------------------------------------------------------------------

def test_a_stage_is_one_digit_on_a_rule_a_memory_or_a_skill_version(g):
    r, _ = db.add_row(g, "R", "Use plain words", "", tags=["stage:3"])
    m, _ = db.add_row(g, "M", "Prefers short diffs", "", tags=["stage:9"])
    assert tags(g, r) == {"stage:3"} and tags(g, m) == {"stage:9"}
    for bad in ("stage:10", "stage:-1", "stage:", "stage:x", "stage:٣", "Stage:1", "stage:1x"):
        with pytest.raises(db.LedgerError):
            db.add_row(g, "R", f"bad {ord(bad[-1])}", "", tags=[bad])


def test_issues_tasks_and_plain_documents_take_no_stage(g):
    i, _ = db.add_row(g, "I", "A mistake", "")
    t = db.add_item(g, "T", "A task")
    d = db.add_doc(g, "Notes", "text")
    for target in (i, t, d):
        with pytest.raises(db.LedgerError):
            db.tag(g, target, ["stage:2"], privileged=True)
    with pytest.raises(db.LedgerError):
        db.add_item(g, "T", "A staged task", tags=["stage:1"])
    with pytest.raises(db.LedgerError):
        db.add_doc(g, "Staged", "text", tags=["stage:1"])


def test_two_stage_tags_on_one_record_are_refused(g):
    r, _ = db.add_row(g, "R", "One stage", "", tags=["stage:2"])
    with pytest.raises(db.LedgerError):
        db.add_row(g, "R", "Two stages", "", tags=["stage:1", "stage:2"])
    with pytest.raises(db.LedgerError):
        db.tag(g, r, ["stage:4"], privileged=True)  # change it with `stage set`, never add a second
    assert tags(g, r) == {"stage:2"}


def test_lookalike_stage_tags_are_refused_not_stored_as_other_tags(g):
    for bad in ("Stage:0", "STAGE:0", "stage​:0", "ｓtage:0", "stage⁠:0", "human_task​",
                "Human_Task"):
        with pytest.raises(db.LedgerError):
            db.add_row(g, "R", f"lookalike {len(bad)}{ord(bad[0])}", "", tags=[bad], privileged=True)


def test_a_tag_name_of_eighty_plus_characters_is_cut_not_trusted(g):
    r, _ = db.add_row(g, "R", "Long tag", "", tags=["a" * 200])
    assert tags(g, r) == {"a" * 80}
    with pytest.raises(db.LedgerError):
        db.add_row(g, "R", "Long project", "", tags=["project:" + "a" * 200])


# --- stage 0 is the operator's -------------------------------------------------------------------

def test_only_the_local_cli_stores_stage_zero(g):
    ok, _ = db.add_row(g, "R", "Operator rule", "", tags=["stage:0"], privileged=True)
    assert tags(g, ok) == {"stage:0"}
    for how in ("add", "tag"):
        if how == "add":
            rid, _ = db.add_row(g, "R", "Agent rule", "", tags=["stage:0"])
        else:
            rid, _ = db.add_row(g, "R", "Agent rule two", "")
            db.tag(g, rid, ["stage:0"])
        assert tags(g, rid) == {db.STAGE_PROPOSED}


def test_a_baseline_record_cannot_be_edited_retagged_or_superseded_by_an_agent(g):
    base, _ = db.add_row(g, "R", "Always cite sources", "body", tags=["stage:0"], privileged=True)
    with pytest.raises(db.LedgerError):
        db.update_row(g, base, summary="Never cite sources")
    with pytest.raises(db.LedgerError):
        db.update_row(g, base, alias="hijack")
    with pytest.raises(db.LedgerError):
        db.tag(g, base, ["extra"])
    with pytest.raises(db.LedgerError):
        db.retag(g, base, drop_prefix="stage:")
    mine, _ = db.add_row(g, "R", "Sneaky replacement", "")
    with pytest.raises(db.LedgerError):
        db.link(g, mine, base, "supersedes")
    assert db.get_row(g, base).summary == "Always cite sources" and tags(g, base) == {"stage:0"}
    # the operator's CLI may
    assert db.update_row(g, base, summary="Always cite two sources", privileged=True).summary.endswith("two sources")
    db.tag(g, base, ["extra"], privileged=True)


def test_saving_the_same_text_again_cannot_touch_a_baseline_record(g):
    base, _ = db.add_row(g, "R", "Same words", "x", tags=["stage:0"], privileged=True)
    # the holder comes back as it is (contract 6d): the writer's tags are not applied to a baseline record
    assert db.add_row(g, "R", "Same words", "x", tags=["something"]) == (base, False)
    assert tags(g, base) == {"stage:0"}
    again, created = db.add_row(g, "R", "Same words", "x", tags=["stage:0"])  # already there: nothing new
    assert (again, created) == (base, False) and tags(g, base) == {"stage:0"}


def test_the_sweep_keeps_staged_ideas_and_decisions_for_disuse(g):
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(days=400)
    keep = [db.add_row(g, "M", f"Kept {t}", "", tags=[t], now=old)[0] for t in ("idea", "decision", "stage:5")]
    gone, _ = db.add_row(g, "M", "Forgotten ordinary memory", "", now=old)
    report = db.sweep(g, uses={})
    assert gone in report.stale_memories
    assert not set(keep) & set(report.stale_memories)


# --- the budget ----------------------------------------------------------------------------------

def test_the_baseline_is_capped_and_the_refusal_names_the_current_size(g):
    db.add_row(g, "R", "a" * 100, "b" * 5800, tags=["stage:0"], privileged=True)
    with pytest.raises(db.LedgerError) as exc:
        db.add_row(g, "R", "Another", "c" * 300, tags=["stage:0"], privileged=True)
    assert "5900 of 6000" in str(exc.value)
    r, _ = db.add_row(g, "R", "Tiny", "ok", tags=["stage:0"], privileged=True)  # 6 chars still fit
    assert r
    with pytest.raises(db.LedgerError):
        db.update_row(g, r, body="d" * 500, privileged=True)  # an edit may not overflow it either


def test_the_budget_constant_is_one_value():
    assert db.BASELINE_BUDGET == 6000


# (a pulled record against the cap runs through the real sync in tests/test_central_review.py)


# --- the CLI -------------------------------------------------------------------------------------

def test_stage_set_clear_list_pending(wikis, g, capsys):
    rule, _ = db.add_row(g, "R", "Write plainly", "No jargon.")
    agent, _ = db.add_row(g, "M", "Agent suggests baseline", "text", tags=["stage:0"])
    assert tags(g, agent) == {db.STAGE_PROPOSED}

    code, out, _ = cli(["pending", "--json"], wikis, capsys)
    assert code == 0 and [p["id"] for p in json.loads(out)["pending"]] == [agent]
    # a proposed record is not served until the operator accepts it
    assert stages.baseline_ids(g) == []

    code, out, _ = cli(["set", rule, "0"], wikis, capsys)
    assert code == 0 and tags(g, rule) == {"stage:0"}
    cli(["set", agent, "0"], wikis, capsys)
    assert tags(g, agent) == {"stage:0"}  # accepting replaces the proposal
    assert stages.baseline_ids(g) == [rule, agent]

    code, out, _ = cli(["list", "--json"], wikis, capsys)
    data = json.loads(out)
    assert [r["id"] for r in data["records"]] == [rule, agent]
    assert data["baseline_used"] == sum(r["size"] for r in data["records"]) and data["baseline_budget"] == 6000

    cli(["set", rule, "4"], wikis, capsys)
    assert tags(g, rule) == {"stage:4"} and stages.baseline_ids(g) == [agent]
    cli(["clear", rule], wikis, capsys)
    assert tags(g, rule) == set()
    code, _, err = cli(["set", rule, "10"], wikis, capsys)
    assert code == 2 and "digit" in err
    code, _, err = cli(["set", "R999", "1"], wikis, capsys)
    assert code == 2


def test_the_cli_row_commands_are_the_operators(wikis, g, capsys):
    ledger.row_main(["--cwd", str(wikis.repo), "add", "R", "CLI baseline", "--scope", "global",
                     "--tag", "stage:0"])
    rid = capsys.readouterr().out.strip()
    assert tags(g, rid) == {"stage:0"}


def test_a_skill_version_carries_its_stage_and_agents_cannot_inherit_stage_zero(g):
    v = skills.add_version(g, "writing", "Write plainly", "Use short words.", new=True, active=True,
                           privileged=True)
    stages.set_stage(g, "skill:writing", 0)
    assert "stage:0" in tags(g, v.doc_id)
    v2 = skills.add_version(g, "writing", "Write plainly", "Use short words.\nMore.", privileged=True)
    assert "stage:0" in tags(g, v2.doc_id) and v2.status == "pending"
    lesson = skills.add_version(g, "writing", "Write plainly", "From an agent.")  # not privileged
    assert db.STAGE_PROPOSED in tags(g, lesson.doc_id) and "stage:0" not in tags(g, lesson.doc_id)
    skills.activate(g, "writing", v2.version)  # the operator's activation keeps the baseline
    assert stages.baseline_ids(g) == [v2.doc_id]
    assert stages.baseline_ids(g) == [v2.doc_id]  # the proposal alone serves nothing
    skills.activate(g, "writing", lesson.version)  # the operator reviewed it: it takes the baseline over
    assert stages.baseline_ids(g) == [lesson.doc_id] and db.STAGE_PROPOSED not in tags(g, lesson.doc_id)


# --- records pulled from a server ------------------------------------------------------------------

def wire(rid="R9", kind="R", tags=("stage:2",)):
    return {"id": rid, "uid": "u" * 16, "kind": kind, "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T00:00:00Z", "rev": 1, "deleted": False, "device": "d",
            "data": {"summary": "s", "body": "b", "alias": None, "source": "", "session": ""}
            if kind in "MRI" else {"title": "t", "body": "b", "parent": None, "status": "open",
                                   "closed_reason": None, "git_ref": None, "alias": None,
                                   "claimed_by": None, "closed": None},
            "tags": list(tags), "links": []}


def test_a_server_record_with_a_malformed_stage_is_refused():
    assert syncrec.validate_record(wire()) is not None
    for bad in (["stage:1", "stage:2"], ["stage:12"], ["stage-proposed:3"], ["stage:0", "stage-proposed:0"],
                ["Stage:0"], ["stage​:0"]):
        assert syncrec.validate_record(wire(tags=bad)) is None, bad
    assert syncrec.validate_record(wire("T1", "T", ["stage:1"])) is None  # a task has no stage
    assert syncrec.validate_record(wire("I1", "I", ["stage-proposed:0"])) is None
