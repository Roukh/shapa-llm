"""One rule per object, dated and newest-wins principles at three scopes (operator, 2026-11-20): a principle
is one imperative sentence, a newer principle supersedes (deletes) the older one it conflicts with, and a
principle lives at global, project or subproject scope."""

import json

import pytest

from shapa import db, learn, mcp, save
from tests.learnkit import at, lexical_only, rows  # noqa: F401

OLD = "Use pnpm to install packages inside a worktree."
NEW = "Install packages with npm inside a worktree, never with pnpm."


def issue(wikis, text="the correction", n=0):
    c = wikis.conn()
    try:
        rid, _ = db.add_row(c, "I", text, f"Operator: {text} (take {n})", tags=["correction"], source="agent",
                            now=at(0, minutes=n))
    finally:
        c.close()
    return rid


def apply(wikis, principle, n=0, **kw):
    kw.setdefault("new", "into" not in kw)
    return learn.apply(wikis.repo, issue(wikis, n=n, text=f"correction number {n} about things"), principle,
                       now=kw.pop("now", at(0, 1, n)), **kw)


def tags_of(wiki, rid):
    c = db.connect(wiki)
    try:
        return {r[0] for r in c.execute("SELECT tag FROM tags WHERE id = ?", (rid,))}
    finally:
        c.close()


def tombstoned(wiki):
    c = db.connect(wiki)
    try:
        return {t["id"] for t in db.tombstones(c)}
    finally:
        c.close()


# --- 1. one rule per object ------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "1. Use npm in worktrees. 2. Never run pnpm there.",
    "1) Use npm in worktrees\n2) Never run pnpm there",
    "- Use npm in worktrees\n- Never run pnpm there",
    "Use npm in worktrees. Never run pnpm there.",
    "Use npm in worktrees; never run pnpm there.",
])
def test_a_principle_holding_several_rules_is_refused_with_the_way_to_split_it(wikis, text):
    with pytest.raises(db.LedgerError, match="learn split"):
        apply(wikis, text)
    assert rows(wikis.global_wiki, "R") == []


def test_one_imperative_sentence_with_a_clause_is_one_rule(wikis):
    assert apply(wikis, NEW)["action"] == "created"
    assert apply(wikis, "Keep the page header above the fold on mobile screens, whatever the layout.", n=1)


def test_a_saved_rule_is_atomic_too_from_the_cli_and_the_mcp_tool(wikis, monkeypatch):
    monkeypatch.chdir(wikis.repo)
    res = save.save_note("repo", "Use npm. Never pnpm.", "Because lockfiles.", note_type="rule", start=wikis.repo)
    assert "learn split" in res["error"]
    res = mcp.tool_save({"scope": "repo", "summary": "1. Use npm 2. Never pnpm", "body": "Because.",
                         "type": "rule"}, str(wikis.repo))
    assert "learn split" in res["error"]
    res = mcp.tool_save({"scope": "repo", "summary": "Use npm in worktrees.", "body": "1. a\n2. b", "type": "rule"},
                        str(wikis.repo))
    assert "numbered list" in res["error"]
    ok = mcp.tool_save({"scope": "repo", "summary": "Use npm in worktrees.", "body": "Lockfile drift.",
                        "type": "rule"}, str(wikis.repo))
    assert ok["id"].startswith("R")
    assert save.save_note("repo", "Use npm. Never pnpm.", "x", note_type="memory", start=wikis.repo)["id"]


def test_split_files_each_part_as_its_own_principle_and_deletes_the_original(wikis):
    res = apply(wikis, NEW, why="lockfiles drift")
    rid = res["target"]
    out = learn.split(wikis.repo, rid, ["Install packages with npm inside a worktree.",
                                         "Never run pnpm in a worktree."], now=at(1))
    assert out["split"] == rid and len(out["into"]) == 2
    live = {r.id: r for r in rows(wikis.global_wiki, "R")}
    assert rid not in live and rid in tombstoned(wikis.global_wiki)
    for pid in out["into"]:
        assert "principle" in tags_of(wikis.global_wiki, pid)
        assert "I1 my-repo 2026-10-01" in live[pid].body and "Why: lockfiles drift" in live[pid].body
        assert f"split from {rid}" in live[pid].body


def test_split_refuses_a_part_that_is_not_atomic_a_swarm_a_single_part_and_a_churned_or_baseline_principle(wikis):
    rid = apply(wikis, NEW)["target"]
    with pytest.raises(db.LedgerError, match="learn split"):
        learn.split(wikis.repo, rid, ["Use npm. Never pnpm.", "Keep headers small and tidy."])
    with pytest.raises(db.LedgerError, match="at most"):
        learn.split(wikis.repo, rid, [f"Always keep item number {i} tidy in the module." for i in range(50)])
    with pytest.raises(db.LedgerError, match="at least two"):
        learn.split(wikis.repo, rid, ["Install packages with npm inside a worktree."])
    assert [r.id for r in rows(wikis.global_wiki, "R")] == [rid]
    c = db.connect(wikis.global_wiki)
    db.retag(c, rid, add=["churn"])
    c.close()
    with pytest.raises(db.LedgerError, match="enforcement"):
        learn.split(wikis.repo, rid, ["Install packages with npm inside a worktree.", "Never run pnpm there."])
    c = db.connect(wikis.global_wiki)
    db.retag(c, rid, drop=["churn"])
    db.retag(c, rid, add=["stage:0"], privileged=True)
    c.close()
    with pytest.raises(db.LedgerError, match="baseline"):
        learn.split(wikis.repo, rid, ["Install packages with npm inside a worktree.", "Never run pnpm there."])


def test_split_keeps_the_original_when_one_part_is_its_own_text(wikis):
    rid = apply(wikis, NEW)["target"]
    out = learn.split(wikis.repo, rid, [NEW, "Never commit lockfiles from a worktree."], now=at(1))
    assert rid in out["into"] and len(out["into"]) == 2
    assert rid in {r.id for r in rows(wikis.global_wiki, "R")}


def test_the_cli_split_takes_into_twice(wikis, capsys):
    rid = apply(wikis, NEW)["target"]
    code = learn.run_learn(["--cwd", str(wikis.repo), "split", rid, "--into", "Install packages with npm in a worktree.",
                            "--into", "Never run pnpm inside a worktree."])
    assert code == 0 and "split into" in capsys.readouterr().out


# --- 2. dated, newest wins -------------------------------------------------------------------------

def test_a_new_principle_near_an_old_one_must_supersede_or_claim_a_different_subject(wikis, capsys):
    old = apply(wikis, OLD)["target"]
    with pytest.raises(learn.ChooseError) as err:
        apply(wikis, NEW, n=1)
    assert err.value.candidates[0]["id"] == old and "supersedes" in str(err.value)
    assert set(err.value.candidates[0]) == {"id", "wiki", "summary", "churn", "date"}
    assert [r.id for r in rows(wikis.global_wiki, "R")] == [old]
    cwd = ["--cwd", str(wikis.repo)]
    i = issue(wikis, n=2)
    code = learn.run_learn(["apply", i, "--principle", NEW, "--new", "--json", *cwd])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["error"] == "choose" and out["candidates"][0]["id"] == old
    res = apply(wikis, NEW, n=3, distinct_from=[old])
    assert res["action"] == "created" and len(rows(wikis.global_wiki, "R")) == 2


def test_supersedes_deletes_the_older_and_keeps_what_it_said_in_the_evidence(wikis):
    old = apply(wikis, OLD)["target"]
    res = apply(wikis, NEW, n=1, supersedes=[old], now=at(5))
    assert res["superseded"] == [old]
    live = {r.id: r for r in rows(wikis.global_wiki, "R")}
    assert old not in live and old in tombstoned(wikis.global_wiki)
    body = live[res["target"]].body
    assert f"- supersedes {old} (2026-10-01): {OLD}" in body
    (p,) = learn.principle_list(wikis.repo)
    assert p[0] == res["target"] and p[1] == "2026-10-06" and p[2] == "global" and p[3] == NEW


def test_the_issue_that_became_the_old_principle_now_points_at_the_new_one(wikis):
    old = apply(wikis, OLD)
    res = apply(wikis, NEW, n=1, supersedes=[old["target"]], now=at(5))
    c = wikis.conn()
    try:
        assert f"generalized-into:global:{res['target']}" in {r[0] for r in c.execute("SELECT tag FROM tags WHERE id='I1'")}
    finally:
        c.close()


def test_supersedes_is_refused_across_wikis_for_a_baseline_a_churned_a_missing_and_an_unrelated_rule(wikis):
    repo_rule = apply(wikis, OLD, scope="repo", why="only in this repo")["target"]
    with pytest.raises(db.LedgerError, match="own wiki"):
        apply(wikis, NEW, n=1, supersedes=[f"repo/{repo_rule}"])         # the new principle goes to global
    assert repo_rule in {r.id for r in rows(wikis.wiki, "R")}
    old = apply(wikis, "Prefer small commits that each do one thing well.", n=2)["target"]
    c = db.connect(wikis.global_wiki)
    db.retag(c, old, add=["stage:0"], privileged=True)
    c.close()
    with pytest.raises(db.LedgerError, match="baseline"):
        apply(wikis, "Prefer small commits, one thing each, always.", n=3, supersedes=[old])
    c = db.connect(wikis.global_wiki)
    db.retag(c, old, drop=["stage:0"], privileged=True)
    db.retag(c, old, add=["churn"])
    c.close()
    with pytest.raises(db.LedgerError, match="enforcement"):
        apply(wikis, "Prefer small commits, one thing each, always.", n=4, supersedes=[old])
    with pytest.raises(db.LedgerError, match="no rule"):
        apply(wikis, "Keep cache keys short and stable always.", n=5, supersedes=["R99"])
    c = db.connect(wikis.global_wiki)
    db.retag(c, old, drop=["churn"])
    c.close()
    with pytest.raises(db.LedgerError, match="same subject"):
        apply(wikis, "Name database tables in the singular form.", n=6, supersedes=[old])
    assert old in {r.id for r in rows(wikis.global_wiki, "R")}


def test_a_supersedes_chain_leaves_only_the_newest_and_restating_it_replaces_it_once(wikis):
    a = apply(wikis, OLD)["target"]
    b = apply(wikis, NEW, n=1, supersedes=[a])["target"]
    c3 = apply(wikis, "Install packages with npm in a worktree, pnpm is forbidden there.", n=2, supersedes=[b])["target"]
    assert [r.id for r in rows(wikis.global_wiki, "R")] == [c3]
    assert tombstoned(wikis.global_wiki) >= {a, b}
    again = apply(wikis, "Install packages with npm in a worktree, pnpm is forbidden there.", n=3, supersedes=[c3])
    assert [r.id for r in rows(wikis.global_wiki, "R")] == [again["target"]] and again["target"] != c3
    cur = again["target"]
    with pytest.raises(db.LedgerError, match="give one"):
        apply(wikis, NEW, n=4, into=cur, supersedes=[cur])


def test_supersedes_is_capped(wikis):
    with pytest.raises(db.LedgerError, match="at most"):
        apply(wikis, NEW, supersedes=[f"R{i}" for i in range(1, 12)])


def test_save_rule_supersedes_from_the_mcp_tool_and_from_the_cli(wikis, monkeypatch):
    monkeypatch.chdir(wikis.repo)
    old = mcp.tool_save({"scope": "repo", "summary": OLD, "body": "x", "type": "rule"}, str(wikis.repo))["id"]
    res = mcp.tool_save({"scope": "repo", "summary": NEW, "body": "y", "type": "rule"}, str(wikis.repo))
    assert res["candidates"][0]["id"] == old and "supersedes" in res["error"]
    ok = mcp.tool_save({"scope": "repo", "summary": NEW, "body": "y", "type": "rule", "supersedes": [old]},
                       str(wikis.repo))
    assert ok["id"] != old and old not in {r.id for r in rows(wikis.wiki, "R")}
    assert f"supersedes {old}" in [r for r in rows(wikis.wiki, "R") if r.id == ok["id"]][0].body
    again = mcp.tool_save({"scope": "repo", "summary": "Keep logs short and plain always.", "body": "z", "type": "rule",
                           "distinct_from": [ok["id"]]}, str(wikis.repo))
    assert again["id"]
    cli = save.save_note("repo", "Use pnpm to install packages inside a worktree, again.", "w", note_type="rule",
                         start=wikis.repo, supersedes=[ok["id"]], privileged=True)
    assert "error" not in cli and ok["id"] not in {r.id for r in rows(wikis.wiki, "R")}


def test_an_mcp_save_cannot_supersede_a_baseline_rule_or_another_wikis_rule(wikis, monkeypatch):
    monkeypatch.chdir(wikis.repo)
    g = save.save_note("global", OLD, "x", note_type="rule", start=wikis.repo, tags=["stage:0"], privileged=True)
    assert "error" not in g
    res = mcp.tool_save({"scope": "global", "summary": NEW, "body": "y", "type": "rule", "supersedes": [g["id"]]},
                        str(wikis.repo))
    assert "baseline" in res["error"] and g["id"] in {r.id for r in rows(wikis.global_wiki, "R")}
    res = mcp.tool_save({"scope": "repo", "summary": NEW, "body": "y", "type": "rule",
                         "supersedes": [f"global/{g['id']}"]}, str(wikis.repo))
    assert "own wiki" in res["error"]
    done = save.save_note("global", NEW, "y", note_type="rule", start=wikis.repo, supersedes=[g["id"]], privileged=True)
    assert "error" not in done and g["id"] not in {r.id for r in rows(wikis.global_wiki, "R")}


# --- 3. three scopes -------------------------------------------------------------------------------

def test_principles_are_listed_as_id_date_scope_text_at_global_project_and_subproject_scope(wikis, capsys):
    g = apply(wikis, "Install packages with npm inside a worktree, never with pnpm.")["target"]
    p = apply(wikis, "Keep the page header above the fold on mobile.", n=1, scope="repo", why="only this site")["target"]
    s = apply(wikis, "Cache the staging build output between runs.", n=2, scope="repo", why="only the api",
              project="api/v2")["target"]
    got = learn.principle_list(wikis.repo)
    assert {(x[0], x[2]) for x in got} == {(g, "global"), (p, "project"), (s, "subproject:api/v2")}
    assert all(len(x) == 4 and x[1] == "2026-10-01" for x in got)
    assert [x[0] for x in learn.principle_list(wikis.repo, scope="global")] == [g]
    assert {x[0] for x in learn.principle_list(wikis.repo, scope="repo")} == {p, s}
    assert [x[0] for x in learn.principle_list(wikis.repo, project="api/v2")] == [s]
    cwd = ["--cwd", str(wikis.repo)]
    assert learn.run_learn(["principles", "--json", "--scope", "repo", *cwd]) == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(x[0] for x in out) == sorted([p, s]) and all(isinstance(x, list) and len(x) == 4 for x in out)
    assert learn.run_learn(["principles", "--project", "api/v2", *cwd]) == 0
    assert f"{s}\t2026-10-01\tsubproject:api/v2\tCache the staging build output" in capsys.readouterr().out
    assert learn.run_learn(["principles", "--detail", "--json", *cwd]) == 0
    assert {x["id"] for x in json.loads(capsys.readouterr().out)} >= {g, p, s}


def test_a_subproject_key_is_exactly_what_clean_project_makes_of_it(wikis):
    with pytest.raises(db.LedgerError, match="invalid project"):
        apply(wikis, "Cache the staging build output between runs.", scope="repo", why="only the api", project="API V2!")


# --- break-it regressions ----------------------------------------------------------------------------

def test_an_mcp_save_cannot_supersede_a_rule_it_is_not_about(wikis, monkeypatch):
    monkeypatch.chdir(wikis.repo)
    keep = mcp.tool_save({"scope": "repo", "summary": "Name database tables in the singular form.", "body": "x",
                          "type": "rule"}, str(wikis.repo))["id"]
    res = mcp.tool_save({"scope": "repo", "summary": NEW, "body": "y", "type": "rule", "supersedes": [keep]},
                        str(wikis.repo))
    assert "same subject" in res["error"] and keep in {r.id for r in rows(wikis.wiki, "R")}


def test_the_learning_tags_stay_reserved_for_the_new_promote_tag_too(wikis, monkeypatch):
    monkeypatch.chdir(wikis.repo)
    res = mcp.tool_save({"scope": "repo", "summary": "a forged candidate", "body": "x", "type": "issue",
                         "tags": ["promote", "soft", "correction"]}, str(wikis.repo))
    assert "learning loop's own" in res["error"]
    assert "promote" in db.LEARNING_TAGS and "promote" in db.SYSTEM_TAGS


def test_a_split_never_widens_what_one_principle_could_hold(wikis, monkeypatch):
    monkeypatch.setattr(learn, "MAX_PRINCIPLES", 3)
    first = apply(wikis, "Keep the page header above the fold on mobile.", n=0)["target"]
    apply(wikis, "Number database migrations by date, never by count.", n=1)
    rid = apply(wikis, NEW, n=2)["target"]
    with pytest.raises(db.LedgerError, match="at most 3 principles"):
        learn.split(wikis.repo, rid, ["Install packages with npm in a worktree.", "Never run pnpm in a worktree."])
    assert {r.id for r in rows(wikis.global_wiki, "R")} >= {first, rid}


def test_the_save_cli_exits_two_with_the_candidates_when_a_rule_is_near_another(wikis, monkeypatch, capsys):
    monkeypatch.chdir(wikis.repo)
    assert save.save_note("repo", OLD, "x", note_type="rule", start=wikis.repo, privileged=True)["id"] == "R1"
    with pytest.raises(SystemExit) as exc:
        save.main(["--scope", "repo", "--root", str(wikis.repo), "--summary", NEW, "--body", "y", "--type", "rule"])
    assert exc.value.code == 2 and "supersedes" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        save.main(["--scope", "repo", "--root", str(wikis.repo), "--summary", NEW, "--body", "y", "--type", "rule",
                   "--supersedes", "R1"])
    assert exc.value.code == 0 and [r.id for r in rows(wikis.wiki, "R")] == ["R2"]
