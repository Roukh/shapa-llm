"""Global tasks, the operator's task list, project tags and project files (contract 4, 4b, 5)."""

import json
from pathlib import Path

import pytest

from shapa import db, ledger, mcp, projectfile, registry, save, tasks


@pytest.fixture
def two(wikis):
    for w in (wikis.wiki, wikis.global_wiki):
        registry.write_format(w)
    return wikis


def run(entry, argv, wikis, capsys):
    code = 0
    try:
        entry(["--cwd", str(wikis.repo), *argv])
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


def mcp_call(tool, args, wikis):
    r = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": tool, "arguments": args}}, str(wikis.repo))
    return json.loads(r["result"]["content"][0]["text"])


# --- the global wiki takes tasks, standalone --------------------------------------------------------

def test_ledger_add_human_task_and_agent_task_in_the_global_wiki(two, capsys):
    code, out, _ = run(ledger.main, ["add", "human_task", "Renew the passport", "--scope", "global"], two, capsys)
    assert code == 0 and out.strip() == "T1"
    code, out, _ = run(ledger.main, ["add", "agent_task", "Triage the inbox", "--scope", "global",
                                     "--project", "ops"], two, capsys)
    g = two.conn(two.global_wiki)
    assert db.get_item(g, "T1").parent is None
    assert set(db._tags_of(g, "T1")) == {"human_task"} and set(db._tags_of(g, "T2")) == {"agent_task", "project:ops"}
    g.close()


def test_the_global_wiki_refuses_features_jobs_and_ownerless_tasks(two, capsys):
    for kind in ("F", "J"):
        code, _, err = run(ledger.main, ["add", kind, "Nope", "--scope", "global"], two, capsys)
        assert code == 2 and "tasks only" in err
    code, _, err = run(ledger.main, ["add", "T", "Ownerless", "--scope", "global"], two, capsys)
    assert code == 2 and "human_task or an agent_task" in err
    g = two.conn(two.global_wiki)
    assert db.items(g) == []
    with pytest.raises(db.LedgerError):
        tasks.add_task(g, "Has a parent", "human", standalone_only=True, parent="J1")
    with pytest.raises(db.LedgerError):
        tasks.add_task(g, "Both", "human", standalone_only=True, tags=["agent_task"])
    g.close()


def test_the_task_aliases_work_in_a_repo_wiki_too(two, capsys):
    run(ledger.main, ["add", "human_task", "Approve the design"], two, capsys)
    r = two.conn()
    assert set(db._tags_of(r, "T1")) == {"human_task"}
    r.close()


def test_save_types_decision_idea_and_the_task_aliases(two, capsys):
    base = ["--scope", "global", "--root", str(two.repo)]
    for typ, summary in (("decision", "Use SQLite"), ("idea", "A phone widget"),
                         ("human_task", "Call the bank"), ("agent_task", "Summarise the week")):
        with pytest.raises(SystemExit) as e:
            save.main(["--summary", summary, "--body", "why", "--type", typ, *base])
        assert e.value.code == 0, typ
    capsys.readouterr()
    g = two.conn(two.global_wiki)
    assert set(db._tags_of(g, "M1")) == {"decision"} and set(db._tags_of(g, "M2")) == {"idea"}
    assert db.get_item(g, "T1").title == "Call the bank" and set(db._tags_of(g, "T1")) == {"human_task"}
    assert set(db._tags_of(g, "T2")) == {"agent_task"}
    g.close()


def test_the_mcp_save_accepts_the_new_types_and_proposes_stage_zero(two):
    assert mcp_call("save", {"scope": "global", "summary": "Mow the lawn", "type": "human_task"}, two)["id"] == "T1"
    assert "id" in mcp_call("save", {"scope": "global", "summary": "Postgres wins", "type": "decision",
                                     "body": "because"}, two)
    r = mcp_call("save", {"scope": "global", "summary": "Sneaky baseline", "body": "x", "type": "rule",
                          "tags": ["stage:0"]}, two)
    g = two.conn(two.global_wiki)
    assert set(db._tags_of(g, r["id"])) == {"stage-proposed:0"}  # accepted, stored as a proposal
    g.close()
    assert "error" in mcp_call("save", {"scope": "global", "summary": "x", "body": "y", "type": "bogus"}, two)
    assert "error" in mcp_call("save", {"scope": "global", "summary": "", "type": "human_task"}, two)


def test_identical_open_text_is_stored_once(two):
    g = two.conn(two.global_wiki)
    a, created = tasks.add_task(g, "Pay rent", "human", "due friday", standalone_only=True)
    b, again = tasks.add_task(g, "Pay rent", "human", "due friday", standalone_only=True, project="home")
    assert (a, created, b, again) == ("T1", True, "T1", False)
    assert "project:home" in db._tags_of(g, a)  # the repeat still adds what it brings
    c, other = tasks.add_task(g, "Pay rent", "agent", "due friday", standalone_only=True)  # another owner
    assert other and c != a
    db.close(g, a, "agent")
    d, fresh = tasks.add_task(g, "Pay rent", "human", "due friday", standalone_only=True)  # not open any more
    assert fresh and d not in (a, c)
    g.close()


def test_an_agent_can_close_a_global_task(two, capsys):
    run(ledger.main, ["add", "human_task", "Do it", "--scope", "global"], two, capsys)
    code, out, _ = run(ledger.main, ["close", "T1", "--scope", "global"], two, capsys)
    assert code == 0 and "closed T1" in out


# --- shapa tasks ------------------------------------------------------------------------------------

def test_tasks_lists_the_operators_open_tasks_across_the_wikis_rank_first_then_oldest(two, capsys):
    g = two.conn(two.global_wiki)
    r = two.conn()
    for i in range(3):
        tasks.add_task(g, f"Global human {i}", "human", standalone_only=True)
    tasks.add_task(g, "An agent job", "agent", standalone_only=True)
    tasks.add_task(r, "Repo human", "human", project="alpha")
    tasks.add_task(r, "Repo done", "human")
    db.close(r, "T2", "agent")
    db.tag(g, "T3", ["rank:2"])
    db.tag(r, "T1", ["rank:1"])
    g.close()
    r.close()
    code, out, _ = run(tasks.main, ["--json"], two, capsys)
    data = json.loads(out)
    assert data["total"] == 4
    assert [(t["wiki"], t["id"]) for t in data["tasks"]] == [("my-repo", "T1"), ("global", "T3"),
                                                              ("global", "T1"), ("global", "T2")]
    first = data["tasks"][0]
    assert first == {"wiki": "my-repo", "id": "T1", "title": "Repo human", "project": "alpha", "owner": "human",
                     "status": "open", "created": first["created"], "rank": 1}
    code, out, _ = run(tasks.main, ["--owner", "agent"], two, capsys)
    assert "An agent job" in out and "Global human" not in out
    code, out, _ = run(tasks.main, ["--project", "alpha", "--json"], two, capsys)
    assert [t["id"] for t in json.loads(out)["tasks"]] == ["T1"]
    code, out, _ = run(tasks.main, ["--status", "closed", "--json"], two, capsys)
    assert [t["title"] for t in json.loads(out)["tasks"]] == ["Repo done"]
    code, out, _ = run(tasks.main, [], two, capsys)
    assert "global/T3" in out and "4 open human task(s)" in out


def test_tasks_limit_total_and_the_mcp_tool(two):
    g = two.conn(two.global_wiki)
    for i in range(120):
        tasks.add_task(g, f"Task {i}", "human", standalone_only=True)
    g.close()
    out = mcp_call("tasks", {}, two)
    assert out["total"] == 120 and len(out["tasks"]) == 100
    assert mcp_call("tasks", {"owner": "agent"}, two) == {"tasks": [], "total": 0}
    assert "error" in mcp_call("tasks", {"owner": "robot"}, two)
    assert "error" in mcp_call("tasks", {"status": "weird"}, two)
    assert len(mcp_call("tasks", {"wiki": "my-repo"}, two)["tasks"]) == 0


def test_tasks_all_reads_every_registered_wiki(two, tmp_path, capsys):
    other = tmp_path / "elsewhere" / ".shapa"
    other.mkdir(parents=True)
    (other / "AGENTS.md").write_text("# marker\n", encoding="utf-8")
    (other.parent / ".git").mkdir()
    c = db.connect(other, create=True)
    tasks.add_task(c, "Elsewhere task", "human")
    c.close()
    registry.register([other], via="test")
    code, out, _ = run(tasks.main, [], two, capsys)
    assert "Elsewhere task" not in out
    code, out, _ = run(tasks.main, ["--all"], two, capsys)
    assert "Elsewhere task" in out


# --- project tag and about link ----------------------------------------------------------------------

def about(conn, rid):
    return sorted(d for s, d, rel in db.links_of(conn, rid) if s == rid and rel == "about")


def test_a_project_tag_links_the_record_to_the_projects_document(two):
    g = two.conn(two.global_wiki)
    doc, _ = projectfile.new_project(g, "alpha", "Alpha")
    t = db.add_item(g, "T", "Do the thing", tags=["human_task", "project:alpha"])
    m, _ = db.add_row(g, "M", "Alpha facts", "", tags=["project:alpha"])
    plain, _ = db.add_row(g, "M", "Unrelated", "")
    assert about(g, t) == [doc] and about(g, m) == [doc] and about(g, plain) == []
    db.tag(g, plain, ["project:alpha"])  # retag
    assert about(g, plain) == [doc]
    other, _ = projectfile.new_project(g, "beta", "Beta")
    db.tag(g, plain, ["project:beta"])  # moving to another project moves the link
    assert about(g, plain) == [other]
    g.close()


def test_a_document_created_later_links_the_records_already_tagged(two):
    g = two.conn(two.global_wiki)
    t = db.add_item(g, "T", "Early task", tags=["agent_task", "project:late"])
    assert about(g, t) == []
    doc, _ = projectfile.new_project(g, "late")
    assert about(g, t) == [doc]
    g.close()


def test_a_record_carries_one_project_and_a_valid_key(two):
    g = two.conn(two.global_wiki)
    with pytest.raises(db.LedgerError):
        db.add_row(g, "M", "Two projects", "", tags=["project:a", "project:b"])
    for bad in ("project:Alpha", "project:", "project:ａlpha", "project:a​b", "project:" + "a" * 90):
        with pytest.raises(db.LedgerError):
            db.add_row(g, "M", f"bad {len(bad)}", "", tags=[bad])
    g.close()


# --- the project file --------------------------------------------------------------------------------

def test_the_template_is_one_asset_with_the_contracts_text():
    text = Path(projectfile.TEMPLATE_PATH).read_text(encoding="utf-8")
    assert text.startswith("Issues:\n1. \n\nQuestions:\n1. \n\nImplementations:\n1. \n\n---\n\n### Architecture:\n\n")
    assert text.endswith("loop_product:\n1. \n")
    keys = [l[:-1] for l in text.splitlines() if l.endswith(":") and not l.startswith("#")]
    assert [k.lower() for k in keys] == list(projectfile.SECTIONS)
    assert len(text.encode()) < 400


def test_project_new_is_idempotent_and_stores_the_template(two, capsys):
    code, out, _ = run(projectfile.main, ["new", "alpha", "--title", "Alpha"], two, capsys)
    code, again, _ = run(projectfile.main, ["new", "alpha"], two, capsys)
    assert code == 0 and "already has" in again
    g = two.conn(two.global_wiki)
    doc = db.project_doc(g, "alpha")
    assert doc.body == projectfile.template() and doc.title == "Alpha" and "project:alpha" in doc.tags
    assert len(db.project_overages(g)) == 0
    g.close()
    code, _, err = run(projectfile.main, ["new", "Bad Key"], two, capsys)
    assert code == 2


def sections_of(two, key="alpha"):
    g = two.conn(two.global_wiki)
    try:
        return projectfile.sections(db.project_doc(g, key).body)
    finally:
        g.close()


def test_project_add_fills_the_placeholder_then_numbers_on(two, capsys):
    run(projectfile.main, ["new", "alpha"], two, capsys)
    for text in ("First issue", "Second issue", "Third"):
        code, out, _ = run(projectfile.main, ["add", "alpha", "issues", text], two, capsys)
        assert code == 0
    g = two.conn(two.global_wiki)
    body = db.project_doc(g, "alpha").body
    g.close()
    assert body.startswith("Issues:\n1. First issue\n2. Second issue\n3. Third\n\nQuestions:\n1. \n")
    assert sections_of(two)["issues"] == ["First issue", "Second issue", "Third"]
    assert sections_of(two)["questions"] == []  # a placeholder is not an item
    code, out, _ = run(projectfile.main, ["add", "alpha", "Issues", "Second issue"], two, capsys)
    assert "already there" in out and body == db.project_doc(two.conn(two.global_wiki), "alpha").body
    run(projectfile.main, ["add", "alpha", "loop_market", "Sell to teams"], two, capsys)
    assert sections_of(two)["loop_market"] == ["Sell to teams"]


def test_a_missing_section_is_created_at_its_template_position():
    body = "Issues:\n1. one\n\nImplementations:\n1. \n"
    new, changed = projectfile.add_to_body(body, "questions", "Why?")
    assert changed and new.index("Issues:") < new.index("Questions:") < new.index("Implementations:")
    new, _ = projectfile.add_to_body(new, "problem", "A problem")  # the architecture part opens first
    assert new.index("Implementations:") < new.index("---") < new.index("### Architecture:") < new.index("Problem:")
    new, _ = projectfile.add_to_body(new, "villains", "A rival")
    assert new.index("Problem:") < new.index("villains:")
    again, changed = projectfile.add_to_body(new, "villains", "A rival")
    assert not changed and again == new
    new2, _ = projectfile.add_to_body("Only prose\n", "solution", "S")
    assert new2.startswith("Only prose") and "### Architecture:" in new2 and projectfile.sections(new2)["solution"] == ["S"]
    new3, _ = projectfile.add_to_body("Issues:\n1. a\n\n---\n\n### Architecture:\n\nProblem:\n1. p\n", "questions", "Q")
    assert new3.index("Questions:") < new3.index("---")  # a top section never lands under the rule


def test_project_items_are_one_masked_line_and_cannot_forge_structure():
    for bad in ("two\nlines", "carriage\rreturn", "nul\x00", "para sep", "\x1b[31mred", "", "   "):
        with pytest.raises(db.LedgerError):
            projectfile.add_to_body(projectfile.template(), "issues", bad)
    with pytest.raises(db.LedgerError):
        projectfile.add_to_body(projectfile.template(), "issues", "x" * 501)
    with pytest.raises(db.LedgerError):
        projectfile.add_to_body(projectfile.template(), "nonsense", "x")
    forged, _ = projectfile.add_to_body(projectfile.template(), "issues", "Questions: pwned ### Architecture: ---")
    assert forged.count("Questions:") == 2 and projectfile.sections(forged)["questions"] == []
    assert projectfile.sections(forged)["issues"] == ["Questions: pwned ### Architecture: ---"]
    assert forged.count("\n") == projectfile.template().count("\n")  # one line took the placeholder's place
    secret = "api_key = sk-ant-api03-" + "B" * 40
    masked, _ = projectfile.add_to_body(projectfile.template(), "issues", secret)
    assert "sk-ant-api03-BBBB" not in masked


def test_a_project_document_stays_under_one_megabyte(two):
    g = two.conn(two.global_wiki)
    projectfile.new_project(g, "big")
    with db.write(g):
        g.execute("UPDATE docs SET body = ? WHERE id = 'D1'", ("x" * (db.DOC_BODY_MAX - 100),))
    with pytest.raises(db.LedgerError) as exc:
        projectfile.add_item(g, "big", "issues", "y" * 400)
    assert "bytes" in str(exc.value)
    g.close()


def test_project_show_and_list(two, capsys):
    g = two.conn(two.global_wiki)
    doc, _ = projectfile.new_project(g, "alpha", "Alpha")
    projectfile.add_item(g, "alpha", "issues", "Slow build")
    tasks.add_task(g, "Fix the build", "agent", project="alpha", standalone_only=True)
    tasks.add_task(g, "Approve budget", "human", project="alpha", standalone_only=True)
    db.add_row(g, "M", "Alpha idea", "", tags=["idea", "project:alpha"])
    db.add_row(g, "M", "Alpha fact", "", tags=["project:alpha"])
    db.add_row(g, "R", "Alpha rule", "", tags=["project:alpha"])
    db.add_row(g, "M", "Elsewhere", "", tags=["project:beta"])
    g.close()
    r = two.conn()
    db.add_item(r, "F", "Alpha rewrite")
    db.add_item(r, "F", "Alpha rewrite two")
    r.close()
    # the repo wiki's key is `my-repo`: the project of that key lists its open features
    g = two.conn(two.global_wiki)
    projectfile.new_project(g, "my-repo", "The repo")
    g.close()
    code, out, _ = run(projectfile.main, ["show", "alpha", "--json"], two, capsys)
    data = json.loads(out)
    assert data["key"] == "alpha" and data["title"] == "Alpha" and data["document"]["id"] == doc
    assert data["document"]["sections"]["issues"] == ["Slow build"]
    assert [(t["title"], t["owner"]) for t in data["tasks"]] == [("Fix the build", "agent"), ("Approve budget", "human")]
    assert [x["summary"] for x in data["ideas"]] == ["Alpha idea"] and [x["summary"] for x in data["memories"]] == ["Alpha fact"]
    assert [x["summary"] for x in data["rules"]] == ["Alpha rule"] and data["skills"] == [] and data["features"] == []
    code, out, _ = run(projectfile.main, ["show", "my-repo", "--json"], two, capsys)
    assert [f["title"] for f in json.loads(out)["features"]] == ["Alpha rewrite", "Alpha rewrite two"]
    code, out, _ = run(projectfile.main, ["list", "--json"], two, capsys)
    assert {p["key"]: p["open_tasks"] for p in json.loads(out)["projects"]} == {"alpha": 2, "my-repo": 0}
    code, out, _ = run(projectfile.main, ["show", "alpha"], two, capsys)
    assert "issues:" in out and "Fix the build" in out
    code, _, err = run(projectfile.main, ["show", "ghost"], two, capsys)
    assert code == 2 and "no project" in err


def test_project_lists_are_capped_with_a_count_of_the_rest(two):
    g = two.conn(two.global_wiki)
    projectfile.new_project(g, "busy")
    for i in range(60):
        tasks.add_task(g, f"Task {i}", "human", project="busy", standalone_only=True)
    data = projectfile.show(g, "busy")
    g.close()
    assert len(data["tasks"]) == 50 and data["more"]["tasks"] == 10


def test_the_mcp_project_tools(two):
    assert mcp_call("project_new", {"key": "alpha", "title": "Alpha"}, two)["created"] is True
    assert mcp_call("project_add", {"key": "alpha", "section": "questions", "text": "Who owns it?"}, two)["changed"]
    shown = mcp_call("project", {"key": "alpha"}, two)
    assert shown["document"]["sections"]["questions"] == ["Who owns it?"]
    assert [p["key"] for p in mcp_call("projects", {}, two)["projects"]] == ["alpha"]
    assert "error" in mcp_call("project_add", {"key": "alpha", "section": "issues", "text": "a\nb"}, two)
    assert "error" in mcp_call("project_add", {"key": "alpha", "section": "issues", "text": 5}, two)
    assert "error" in mcp_call("project", {"key": "nope"}, two)
    assert "error" in mcp_call("project_new", {"key": "BAD"}, two)
    assert "error" in mcp_call("project", {"key": "alpha", "wiki": "weird"}, two)
