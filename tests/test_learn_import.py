"""``shapa learn import``: the operator curates a wiki's principles, mistakes and memories from one file, in
one transaction (privileged CLI only). Tests run on temporary wikis (tests/conftest.py)."""

import json

import pytest

from shapa import bootstrap, db, learn, learnimport, mcp, stages
from tests.learnkit import lexical_only, mempm  # noqa: F401


def entry(text, date="2026-11-14", cls="principle", sources=("src/R1",), **kw):
    return {"text": text, "class": cls, "date": date, "sources": list(sources), **kw}


def memory(text, date="2026-11-10", sources=("src/M1",)):
    return {"text": text, "date": date, "sources": list(sources)}


def dump(tmp_path, key, principles=(), memories=(), delete=(), keep=()):
    path = tmp_path / "curated.json"
    path.write_text(json.dumps({"wikis": {key: {"principles": list(principles), "memories": list(memories)}},
                                "delete": {key: list(delete)}, "keep": {key: list(keep)}}), encoding="utf-8")
    return path


def seed(wiki, kind, text, tags=()):
    c = db.connect(wiki)
    try:
        return db.add_row(c, kind, text, "old body", tags=list(tags), source="test")[0]
    finally:
        c.close()


def snapshot(wiki):
    c = db.connect(wiki)
    try:
        return (sorted(tuple(r) for r in c.execute("SELECT id, summary, body, created, updated FROM mri")),
                sorted(tuple(r) for r in c.execute("SELECT id, tag FROM tags")),
                sorted(t["id"] for t in db.tombstones(c)),
                sorted(tuple(r) for r in c.execute("SELECT kind, next FROM counters")))
    finally:
        c.close()


def rows_of(wiki, kind):
    c = db.connect(wiki)
    try:
        return db.rows(c, kind)
    finally:
        c.close()


def run(wikis, path, key="global", **kw):
    kw.setdefault("cwd", wikis.repo)
    return learnimport.import_curated(path, key, **kw)


# --- 1. validate first, write nothing on any error -----------------------------------------------

def test_one_bad_text_refuses_the_whole_import_and_leaves_the_database_untouched(wikis, tmp_path):
    old = seed(wikis.global_wiki, "R", "Use an old habit nobody wants any more.")
    before = snapshot(wikis.global_wiki)
    path = dump(tmp_path, "global",
                principles=[entry("Keep each change small and reviewed before it merges."),
                            entry("1. Use npm in worktrees. 2. Never run pnpm there.")],
                delete=[old])
    with pytest.raises(learnimport.ImportRefused) as exc:
        run(wikis, path)
    assert any("ONE rule" in p for p in exc.value.problems)
    assert snapshot(wikis.global_wiki) == before


def test_a_mistake_must_be_one_rule_but_a_memory_may_state_a_fact_in_several_sentences(wikis, tmp_path):
    two = "Keep the one database in git and add an export; write no merge driver."
    with pytest.raises(learnimport.ImportRefused):
        run(wikis, dump(tmp_path, "global", principles=[entry(two, cls="mistake")]))
    assert rows_of(wikis.global_wiki, "I") == []
    res = run(wikis, dump(tmp_path, "global", memories=[memory(two)]))
    assert res["written"]["memory"] == 1


def test_personal_data_refuses_the_import(wikis, tmp_path):
    before = snapshot(wikis.global_wiki)
    path = dump(tmp_path, "global", memories=[memory("Write to jane.roe@gmail.com about the weekly report.")])
    with pytest.raises(learnimport.ImportRefused) as exc:
        run(wikis, path)
    assert any("email" in p for p in exc.value.problems)
    assert snapshot(wikis.global_wiki) == before


def test_a_row_in_both_delete_and_keep_is_refused(wikis, tmp_path):
    rid = seed(wikis.global_wiki, "M", "Some fact worth keeping around here.")
    path = dump(tmp_path, "global", delete=[rid], keep=[rid])
    with pytest.raises(learnimport.ImportRefused) as exc:
        run(wikis, path)
    assert any(rid in p for p in exc.value.problems)
    assert any(r.id == rid for r in rows_of(wikis.global_wiki, "M"))


def test_more_than_the_principle_cap_is_refused(wikis, tmp_path):
    many = [entry(f"Keep area number {i} aligned with the written plan.") for i in range(learn.MAX_PRINCIPLES + 1)]
    with pytest.raises(learnimport.ImportRefused) as exc:
        run(wikis, dump(tmp_path, "global", principles=many))
    assert any(str(learn.MAX_PRINCIPLES) in p for p in exc.value.problems)
    assert rows_of(wikis.global_wiki, "R") == []


def test_a_ledger_item_is_never_deleted(wikis, tmp_path):
    c = wikis.conn()
    try:
        f = db.add_item(c, "F", "a feature")
        j = db.add_item(c, "J", "a job", parent=f)
        t = db.add_item(c, "T", "a task", parent=j)
    finally:
        c.close()
    for victim in (f, t):
        with pytest.raises(learnimport.ImportRefused) as exc:
            run(wikis, dump(tmp_path, "my-repo", delete=[victim]), "my-repo")
        assert any(victim in p for p in exc.value.problems)
    c = wikis.conn()
    try:
        assert {i.id for i in db.items(c)} == {f, j, t}
    finally:
        c.close()


def test_a_missing_id_is_reported_and_is_not_fatal(wikis, tmp_path):
    rid = seed(wikis.global_wiki, "M", "A fact that goes away in this import.")
    res = run(wikis, dump(tmp_path, "global", delete=[rid, "R999"]))
    assert res["deleted"] == 1 and res["missing"] == ["R999"]


def test_a_wiki_that_cannot_be_found_is_an_error(wikis, tmp_path):
    with pytest.raises(db.LedgerError, match="no-such-repo"):
        run(wikis, dump(tmp_path, "no-such-repo"), "no-such-repo")


# --- 2. delete and write in one transaction -------------------------------------------------------

def test_deleted_rows_are_tombstoned_and_the_new_rows_are_written(wikis, tmp_path):
    old = seed(wikis.global_wiki, "R", "An older rule that the import replaces.")
    path = dump(tmp_path, "global", principles=[entry("Keep each change small and reviewed before it merges.")],
                memories=[memory("The deploy host is called atlas.")], delete=[old])
    res = run(wikis, path)
    assert res["deleted"] == 1 and res["written"]["principle"] == 1 and res["written"]["memory"] == 1
    assert old not in {r.id for r in rows_of(wikis.global_wiki, "R")}
    assert old in snapshot(wikis.global_wiki)[2]
    assert {r.summary for r in rows_of(wikis.global_wiki, "R")} == {"Keep each change small and reviewed before it merges."}


def test_a_failure_while_writing_rolls_the_deletions_back(wikis, tmp_path, monkeypatch):
    old = seed(wikis.global_wiki, "R", "An older rule that must survive a failed import.")
    before = snapshot(wikis.global_wiki)
    calls = []
    real = db.add_row

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise db.LedgerError("disk on fire")
        return real(*a, **kw)

    monkeypatch.setattr(db, "add_row", flaky)
    path = dump(tmp_path, "global", principles=[entry("Keep each change small and reviewed before it merges."),
                                                 entry("Name each branch after the feature it carries.")], delete=[old])
    with pytest.raises(db.LedgerError, match="disk on fire"):
        run(wikis, path)
    assert snapshot(wikis.global_wiki) == before


def test_a_second_import_is_idempotent(wikis, tmp_path):
    old = seed(wikis.global_wiki, "R", "An older rule that the import replaces.")
    path = dump(tmp_path, "global",
                principles=[entry("Keep each change small and reviewed before it merges."),
                            entry("Never kill an arm without a fresh audit.", cls="mistake")],
                memories=[memory("The deploy host is called atlas.")], delete=[old])
    run(wikis, path)
    first = snapshot(wikis.global_wiki)
    res = run(wikis, path)
    assert res["deleted"] == 0 and res["missing"] == [old]
    assert res["written"]["total"] == 0 and res["skipped"] == 3
    assert snapshot(wikis.global_wiki) == first


# --- 3. the shape of what is written --------------------------------------------------------------

def test_a_principle_is_a_dated_principle_rule_with_its_evidence(wikis, tmp_path):
    path = dump(tmp_path, "my-repo", principles=[entry("Keep each change small and reviewed before it merges.",
                                                        date="2026-11-14", sources=("a/R1", "b/I2"))])
    run(wikis, path, "my-repo")
    [r] = rows_of(wikis.wiki, "R")
    assert r.created.startswith("2026-11-14") and r.updated.startswith("2026-11-14")
    assert learn.date_of(r) == "2026-11-14"
    assert {"principle", "serves:none", "evidence:1"} <= set(r.tags)
    assert any(t.startswith("last-seen:2026-11-14") for t in r.tags)
    assert "- imported 2026-11-14: sources a/R1, b/I2" in r.body
    assert not any(t.startswith("stage") for t in r.tags)      # a repo wiki has no stage 0


def test_a_subproject_principle_carries_its_project_tag(wikis, tmp_path):
    path = dump(tmp_path, "my-repo", principles=[entry("Keep each change small and reviewed before it merges.",
                                                        project="site/blog")])
    run(wikis, path, "my-repo")
    [r] = rows_of(wikis.wiki, "R")
    assert "project:site/blog" in r.tags


def test_identical_text_in_the_file_is_written_once(wikis, tmp_path):
    t = "Keep each change small and reviewed before it merges."
    res = run(wikis, dump(tmp_path, "global", principles=[entry(t), entry(t, date="2026-11-15")]))
    assert res["written"]["principle"] == 1 and res["skipped"] == 1


def test_a_mistake_is_an_issue_that_still_carries_a_lesson_and_shows_in_the_mempm_x_with_its_date(wikis, tmp_path):
    path = dump(tmp_path, "global", principles=[entry("Never kill an arm without a fresh audit.", cls="mistake",
                                                       date="2026-11-12")])
    run(wikis, path)
    [i] = rows_of(wikis.global_wiki, "I")
    assert not set(i.tags) & set(bootstrap.NOT_A_LESSON) and "correction" not in i.tags
    x = mempm(bootstrap.build_context(start=wikis.repo))["X"]
    assert [e[1:] for e in x] == [["2026-11-12", "Never kill an arm without a fresh audit."]]


def test_memories_are_dated_rows(wikis, tmp_path):
    run(wikis, dump(tmp_path, "my-repo", memories=[memory("The deploy host is called atlas.", date="2026-10-06")]),
        "my-repo")
    [m] = rows_of(wikis.wiki, "M")
    assert m.created.startswith("2026-10-06") and m.updated.startswith("2026-10-06")


# --- 4. the baseline: stage 0 on the global wiki, within its budget --------------------------------

def test_global_principles_and_memories_are_staged_and_the_mempm_shows_their_dates(wikis, tmp_path):
    path = dump(tmp_path, "global", principles=[entry("Keep each change small and reviewed before it merges.",
                                                       date="2026-11-14")],
                memories=[memory("The deploy host is called atlas.", date="2026-10-06")])
    res = run(wikis, path)
    assert res["staged"]["principle"] == 1 and res["staged"]["memory"] == 1 and res["staged"]["mistake"] == 0
    mem = mempm(bootstrap.build_context(start=wikis.repo))
    assert [e[1:] for e in mem["P"]] == [["2026-11-14", "Keep each change small and reviewed before it merges."]]
    assert [e[1:] for e in mem["M"]] == [["2026-10-06", "The deploy host is called atlas."]]


def test_the_stage_budget_is_never_raised_the_newest_principles_are_staged_first(wikis, tmp_path):
    budget = db.BASELINE_BUDGET
    pad = " and say so in the review notes before the change is merged into the shared branch for good."
    many = [entry(f"Keep area number {i:02d} aligned with the written plan before any change of kind {i * 7}{pad}"[:150],
                  date=f"2026-11-{1 + i % 28:02d}", sources=("src/R%d" % i, "other/I%d" % i)) for i in range(55)]
    mems = [memory(f"Fact number {i:02d} about the setup of the shared tooling.") for i in range(5)]
    res = run(wikis, dump(tmp_path, "global", principles=many, memories=mems))
    assert db.BASELINE_BUDGET == budget
    c = wikis.conn(wikis.global_wiki)
    try:
        used = stages.baseline_size(c)
        staged = [r for r in db.rows(c, "R") if "stage:0" in r.tags]
        rest = [r for r in db.rows(c, "R") if "stage:0" not in r.tags]
    finally:
        c.close()
    assert used <= budget and res["baseline"] == {"used": used, "budget": budget}
    assert res["staged"]["principle"] == len(staged) and res["not_staged"]["principle"] == len(rest) > 0
    assert min(learn.date_of(r) for r in staged) >= max(learn.date_of(r) for r in rest)
    assert res["staged"]["memory"] == 0 and res["not_staged"]["memory"] == 5     # no memory beside a principle left out
    ov = res["overflow"]
    assert ov["over"] == ov["needed"] - ov["available"] > 0 and ov["budget"] == budget


def test_memories_are_staged_most_recent_first_while_the_budget_holds(wikis, tmp_path):
    base = "x" * 400
    mems = [memory(f"Fact {i:02d} {base}"[:150], date=f"2026-10-{10 + i:02d}") for i in range(12)]
    # 12 memories of 150 characters plus their evidence line do not all fit once a principle is there
    p = entry("Keep each change small and reviewed before it merges.")
    c = wikis.conn(wikis.global_wiki)
    try:
        # the budget counts one-line summaries only: 31 standing rules of 160 characters fill most of it
        for i in range(31):
            db.add_row(c, "R", f"Standing rule {i:02d} " + "f" * 144, "a body that costs nothing", tags=["stage:0"],
                       privileged=True)
    finally:
        c.close()
    res = run(wikis, dump(tmp_path, "global", principles=[p], memories=mems))
    assert res["staged"]["principle"] == 1
    assert 0 < res["staged"]["memory"] < 12 and res["not_staged"]["memory"] == 12 - res["staged"]["memory"]
    c = wikis.conn(wikis.global_wiki)
    try:
        staged = sorted(r.created[:10] for r in db.rows(c, "M") if "stage:0" in r.tags)
        left = sorted(r.created[:10] for r in db.rows(c, "M") if "stage:0" not in r.tags)
        assert stages.baseline_size(c) <= db.BASELINE_BUDGET
    finally:
        c.close()
    assert staged[0] >= left[-1]


def test_a_repo_wiki_import_stages_nothing(wikis, tmp_path):
    res = run(wikis, dump(tmp_path, "my-repo", principles=[entry("Keep each change small and reviewed before it merges.")],
                          memories=[memory("The deploy host is called atlas.")]), "my-repo")
    assert res["staged"] == {"principle": 0, "mistake": 0, "memory": 0, "total": 0}
    c = wikis.conn()
    try:
        assert not [t for (t,) in c.execute("SELECT tag FROM tags") if t.startswith("stage")]
    finally:
        c.close()


# --- 5. dry run --------------------------------------------------------------------------------------

def test_a_dry_run_reports_the_counts_and_writes_nothing(wikis, tmp_path):
    old = seed(wikis.global_wiki, "R", "An older rule that the import replaces.")
    before = snapshot(wikis.global_wiki)
    path = dump(tmp_path, "global", principles=[entry("Keep each change small and reviewed before it merges."),
                                                 entry("Never kill an arm without a fresh audit.", cls="mistake")],
                memories=[memory("The deploy host is called atlas.")], delete=[old, "R404"])
    res = run(wikis, path, dry_run=True)
    assert res["dry_run"] is True and res["deleted"] == 1 and res["missing"] == ["R404"]
    assert res["written"] == {"principle": 1, "mistake": 1, "memory": 1, "total": 3}
    assert res["staged"]["principle"] == 1 and res["staged"]["memory"] == 1 and res["skipped"] == 0
    assert snapshot(wikis.global_wiki) == before


# --- 6. the command ----------------------------------------------------------------------------------

def test_the_cli_prints_the_json_report(wikis, tmp_path, capsys):
    path = dump(tmp_path, "global", principles=[entry("Keep each change small and reviewed before it merges.")])
    with pytest.raises(SystemExit) as stop:
        learn.main(["--cwd", str(wikis.repo), "import", str(path), "--wiki", "global", "--scope", "global",
                    "--dry-run", "--json"])
    assert stop.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["written"]["principle"] == 1


def test_the_cli_refuses_with_every_problem_and_exit_2(wikis, tmp_path, capsys):
    path = dump(tmp_path, "global", principles=[entry("1. Use npm. 2. Never run pnpm there.")])
    with pytest.raises(SystemExit) as stop:
        learn.main(["--cwd", str(wikis.repo), "import", str(path), "--wiki", "global", "--json"])
    assert stop.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid" and out["problems"]
    assert rows_of(wikis.global_wiki, "R") == []


def test_a_scope_that_does_not_match_the_wiki_key_is_refused(wikis, tmp_path):
    with pytest.raises(db.LedgerError, match="scope"):
        run(wikis, dump(tmp_path, "global"), "global", scope="repo")
    with pytest.raises(db.LedgerError, match="scope"):
        run(wikis, dump(tmp_path, "my-repo"), "my-repo", scope="global")


def test_no_agent_tool_can_import():
    names = {t["name"] for t in mcp.TOOLS} if hasattr(mcp, "TOOLS") else set()
    assert not any("import" in n or "curate" in n for n in names)
