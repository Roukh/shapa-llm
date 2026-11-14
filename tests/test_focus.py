"""The northstar and the three fires: one focus record per wiki scope."""

import io
import json
from contextlib import redirect_stdout

import pytest

from shapa import db, focus, mcp


@pytest.fixture
def conn(wikis):
    c = wikis.conn()
    yield c
    c.close()


def feats(conn, *titles):
    return [db.add_item(conn, "F", t) for t in titles]


def run_cli(argv, cwd):
    out = io.StringIO()
    with redirect_stdout(out):
        code = focus.run_focus(argv + ["--cwd", str(cwd)])
    return code, out.getvalue()


# --- the record -----------------------------------------------------------------------------

def test_an_empty_wiki_has_no_northstar_and_three_free_slots(conn):
    f = focus.load(conn)
    assert f.northstar == "" and f.fires == [None, None, None]
    assert db.documents(conn) == []  # reading creates nothing


def test_the_northstar_is_one_text_and_setting_it_again_replaces_it(conn):
    focus.set_northstar(conn, "Unify memory across every device")
    focus.set_northstar(conn, "Unify memory across every device and app")
    assert focus.load(conn).northstar == "Unify memory across every device and app"
    docs = db.documents(conn)
    assert len(docs) == 1 and docs[0].alias == "focus" and "focus" in docs[0].tags
    assert docs[0].mime == "application/json"


def test_a_fire_points_at_a_feature_or_is_free_text(conn):
    f1, f2 = feats(conn, "Hosted API", "Skills")
    focus.set_fire(conn, 1, feature=f1)
    focus.set_fire(conn, 2, text="Ship the paid tier across repos")
    got = focus.load(conn).fires
    assert got[0].feature == f1 and got[1].feature == "" and got[1].text.startswith("Ship")
    assert got[2] is None
    assert focus.set_fire(conn, 1, feature=f2).fires[0].feature == f2


def test_a_fourth_fire_is_refused_and_so_is_a_bad_slot(conn):
    for slot in (0, 4, -1):
        with pytest.raises(db.LedgerError, match="three"):
            focus.set_fire(conn, slot, text="too many")
    f1 = feats(conn, "One")[0]
    focus.set_fire(conn, 1, feature=f1)
    with pytest.raises(db.LedgerError, match="already a fire"):
        focus.set_fire(conn, 2, feature=f1)


def test_a_fire_names_an_open_feature_that_exists(conn):
    j = db.add_item(conn, "J", "a job")
    done = feats(conn, "Old")[0]
    db.close(conn, done, "operator")
    for bad in ("F99", j, done):
        with pytest.raises(db.LedgerError):
            focus.set_fire(conn, 1, feature=bad)
    with pytest.raises(db.LedgerError, match="feature or text"):
        focus.set_fire(conn, 1)


def test_the_slots_can_be_reordered_and_cleared(conn):
    a, b, c = feats(conn, "A", "B", "C")
    for slot, f in enumerate((a, b, c), 1):
        focus.set_fire(conn, slot, feature=f)
    focus.reorder(conn, [3, 1, 2])
    assert [x.feature for x in focus.load(conn).fires] == [c, a, b]
    focus.clear_fire(conn, 1)  # the rest move up
    assert [x.feature if x else None for x in focus.load(conn).fires] == [a, b, None]
    with pytest.raises(db.LedgerError):
        focus.reorder(conn, [1, 1, 2])


def test_a_malformed_record_reads_as_empty_never_raises(conn):
    db.add_doc(conn, "Focus", "{not json", tags=["focus"], alias="focus", mime="application/json", system=True)
    assert focus.load(conn).fires == [None, None, None]
    d = db.get_doc(conn, "focus")
    db.update_doc(conn, d.id, body=json.dumps({"northstar": 5, "fires": [1, "x", {"feature": 7}, {}, {}]}))
    f = focus.load(conn)
    assert f.northstar == "" and len(f.fires) == 3


def test_oversized_text_is_cut_and_secrets_never_stored(conn):
    focus.set_northstar(conn, "x" * 5000 + "\nsecond line")
    assert len(focus.load(conn).northstar) <= focus.NORTHSTAR_MAX
    assert "\n" not in focus.load(conn).northstar
    focus.set_northstar(conn, "key sk-ant-api03-Q7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7x here")
    assert "sk-ant" not in focus.load(conn).northstar


def test_the_focus_record_is_not_a_project_document_and_never_swept(conn):
    focus.set_northstar(conn, "north")
    db.add_doc(conn, "Project doc", "the project's one document")
    db.sweep(conn, keep_closed_since="2999-01-01T00:00:00Z")
    assert focus.load(conn).northstar == "north"
    assert all("focus" not in r.tags for r in db.doc_rows(conn))  # not in recall


# --- candidates and promotion ------------------------------------------------------------------

def test_candidates_are_open_features_ranked_by_rank_tag_then_oldest(conn):
    a, b, c, d = feats(conn, "A", "B", "C", "D")
    db.add_item(conn, "J", "standalone job")
    focus.rank_feature(conn, c, 1)
    focus.rank_feature(conn, d, 2)
    db.close(conn, a, "operator")
    assert [i.id for i in focus.candidates(conn)] == [c, d, b]
    focus.rank_feature(conn, c, None)
    assert [i.id for i in focus.candidates(conn)] == [d, b, c]


def test_a_feature_that_is_a_fire_is_not_a_candidate(conn):
    a, b = feats(conn, "A", "B")
    focus.set_fire(conn, 1, feature=a)
    assert [i.id for i in focus.candidates(conn)] == [b]


def test_closing_a_fires_feature_frees_the_slot_and_promotes_the_next(conn):
    a, b, c, d, e = feats(conn, "A", "B", "C", "D", "E")
    for slot, f in enumerate((a, b, c), 1):
        focus.set_fire(conn, slot, feature=f)
    focus.rank_feature(conn, e, 1)  # ranked beats older
    db.close(conn, b, "merge")
    ids = [x.feature for x in focus.load(conn).fires]
    assert ids == [a, c, e]  # the rest keep their order, the new one goes last
    assert d not in ids
    db.close(conn, a, "merge")
    assert [x.feature for x in focus.load(conn).fires] == [c, e, d]


def test_closing_a_feature_that_is_not_a_fire_changes_nothing(conn):
    a, b, c, d = feats(conn, "A", "B", "C", "D")
    for slot, f in enumerate((a, b, c), 1):
        focus.set_fire(conn, slot, feature=f)
    before = db.get_doc(conn, "focus").body
    db.close(conn, d, "merge")
    assert db.get_doc(conn, "focus").body == before


def test_with_no_candidate_the_slot_stays_free(conn):
    (a,) = feats(conn, "A")
    focus.set_fire(conn, 1, feature=a)
    db.close(conn, a, "merge")
    assert focus.load(conn).fires == [None, None, None]


def test_the_sweep_expiring_a_feature_frees_its_slot_too(conn):
    a, b = feats(conn, "A", "B")
    focus.set_fire(conn, 1, feature=a)
    db.close(conn, a, "expired")
    assert [x.feature for x in focus.load(conn).fires if x] == [b]


def test_the_ledger_close_command_and_the_merge_hook_promote_too(wikis):
    from shapa import ledger

    c = wikis.conn()
    a, b, c3, d = feats(c, "A", "B", "C", "D")
    for slot, f in enumerate((a, b, c3), 1):
        focus.set_fire(c, slot, feature=f)
    c.close()
    assert ledger.cmd_close(wikis.wiki, a, "agent", None, False) == 0
    c = wikis.conn()
    assert [x.feature for x in focus.load(c).fires] == [b, c3, d]
    db.update_item(c, b, git_ref="F2-b")
    c.close()
    ledger.on_merge(str(wikis.repo), branch="F2-b")
    c = wikis.conn()
    assert b not in [x.feature for x in focus.load(c).fires if x]
    c.close()


def test_free_text_fires_are_never_freed_by_a_close(conn):
    (a,) = feats(conn, "A")
    focus.set_fire(conn, 1, text="spans every repo")
    db.close(conn, a, "merge")
    assert focus.load(conn).fires[0].text == "spans every repo"


def test_the_operator_overrides_the_promotion(conn):
    a, b, c, d = feats(conn, "A", "B", "C", "D")
    for slot, f in enumerate((a, b, c), 1):
        focus.set_fire(conn, slot, feature=f)
    with pytest.raises(db.LedgerError, match="no free"):
        focus.promote(conn)
    focus.promote(conn, feature=d, slot=2)  # replaces slot 2 on purpose
    assert [x.feature for x in focus.load(conn).fires] == [a, d, c]
    focus.clear_fire(conn, 3)
    assert focus.promote(conn).fires[2].feature == b  # the next candidate in line


def test_a_closed_feature_left_in_a_fire_is_healed_on_load(conn):
    a, b = feats(conn, "A", "B")
    focus.set_fire(conn, 1, feature=a)
    with db.write(conn):  # closed behind the hook's back (an older shapa, a sync)
        conn.execute("UPDATE items SET status='closed' WHERE id=?", (a,))
    assert focus.heal(conn).fires[0].feature == b


# --- the tree ------------------------------------------------------------------------------------

def test_a_fires_tree_is_its_feature_open_jobs_tasks_and_named_files(conn):
    a, b = feats(conn, "Hosted API", "Other")
    j1 = db.add_item(conn, "J", "Write shapa/remote.py client", parent=a,
                     body="touches hosted/src/api.ts and [[hosted]]")
    j2 = db.add_item(conn, "J", "closed job", parent=a)
    db.add_item(conn, "T", "a task", parent=j1)
    db.close(conn, j2, "commit")
    db.add_item(conn, "J", "other feature job", parent=b)
    focus.set_fire(conn, 1, feature=a)
    tree = focus.tree(conn, focus.load(conn).fires[0])
    assert tree.title == "Hosted API"
    assert [j.id for j in tree.jobs] == [j1]
    assert [t.id for t in tree.jobs[0].tasks] == ["T1"]
    assert "shapa/remote.py" in tree.files and "hosted/src/api.ts" in tree.files
    assert tree.boxes == ["hosted"]


# --- the CLI and MCP -------------------------------------------------------------------------------

def test_the_cli_sets_and_shows_both_scopes(wikis):
    c = wikis.conn()
    f = db.add_item(c, "F", "Hosted API")
    c.close()
    assert run_cli(["northstar", "--scope", "global", "One brain for every device"], wikis.repo)[0] == 0
    assert run_cli(["fire", "1", f, "--scope", "repo"], wikis.repo)[0] == 0
    assert run_cli(["fire", "2", "--text", "ship it everywhere", "--scope", "global"], wikis.repo)[0] == 0
    code, out = run_cli(["show"], wikis.repo)
    assert code == 0
    assert out.index("One brain") < out.index("Hosted API")
    assert "ship it everywhere" in out
    code, out = run_cli(["show", "--json"], wikis.repo)
    data = json.loads(out)
    assert data["global"]["northstar"].startswith("One brain")
    assert data["repo"]["fires"][0]["feature"] == f


def test_the_cli_refuses_a_feature_in_the_global_wiki_and_a_fourth_fire(wikis):
    with pytest.raises(db.LedgerError):
        focus.run_focus(["fire", "1", "F1", "--scope", "global", "--cwd", str(wikis.repo)])
    with pytest.raises(SystemExit):
        focus.main(["fire", "4", "--text", "no", "--scope", "repo", "--cwd", str(wikis.repo)])


def test_the_cli_promotes_ranks_and_orders(wikis):
    c = wikis.conn()
    a, b = db.add_item(c, "F", "A"), db.add_item(c, "F", "B")
    c.close()
    assert run_cli(["rank", b, "1", "--scope", "repo"], wikis.repo)[0] == 0
    code, out = run_cli(["candidates", "--scope", "repo"], wikis.repo)
    assert out.splitlines()[0].startswith(b)
    assert run_cli(["promote", "--scope", "repo"], wikis.repo)[0] == 0
    assert run_cli(["promote", a, "--scope", "repo"], wikis.repo)[0] == 0
    c = wikis.conn()
    assert [x.feature for x in focus.load(c).fires if x] == [b, a]
    c.close()
    assert run_cli(["order", "2", "1", "3", "--scope", "repo"], wikis.repo)[0] == 0
    c = wikis.conn()
    assert [x.feature for x in focus.load(c).fires if x] == [a, b]
    c.close()


def test_the_mcp_tool_reads_and_sets_the_focus(wikis):
    c = wikis.conn()
    f = db.add_item(c, "F", "Hosted API")
    c.close()
    r = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "focus", "arguments": {"action": "northstar", "scope": "repo", "text": "north"}}},
        str(wikis.repo))
    assert r["result"]["isError"] is False
    mcp.tool_focus({"action": "fire", "scope": "repo", "slot": 1, "feature": f}, str(wikis.repo))
    shown = mcp.tool_focus({"action": "show"}, str(wikis.repo))
    assert shown["repo"]["northstar"] == "north" and shown["repo"]["fires"][0]["feature"] == f
    assert "error" in mcp.tool_focus({"action": "fire", "scope": "repo", "slot": 4, "text": "x"},
                                      str(wikis.repo))
    assert "error" in mcp.tool_focus({"action": "bogus"}, str(wikis.repo))
