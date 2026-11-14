"""Tags are the relations (contract section 3): the tag index, fetch by tag, related."""

import json

import pytest

from shapa import db, fetch, mcp, registry, relate, skills


@pytest.fixture
def web(wikis):
    """A global wiki whose records share tags, linked and staged in known ways."""
    for w in (wikis.wiki, wikis.global_wiki):
        registry.write_format(w)
    g = wikis.conn(wikis.global_wiki)
    ids = {}
    ids["r1"], _ = db.add_row(g, "R", "Write option pricing code in Python", "numpy only", tags=["python", "options"])
    ids["r2"], _ = db.add_row(g, "R", "Backtests need walk-forward splits", "", tags=["options", "backtest", "stage:2"])
    ids["m1"], _ = db.add_row(g, "M", "Operator trades monthly options", "", tags=["options", "stage:1"])
    ids["m2"], _ = db.add_row(g, "M", "Operator prefers rust for tools", "", tags=["rust"])
    ids["i1"], _ = db.add_row(g, "I", "Broke the pricing test", "", tags=["python", "options"])
    ids["t1"] = db.add_item(g, "T", "Review the pricing code", tags=["human_task", "options"])
    skills.add_version(g, "pricing", "Price options", "body", new=True, active=True, privileged=True)
    ids["s1"] = db.get_doc(g, "skill:pricing.v1").id
    db.retag(g, ids["s1"], add=["options"], privileged=True)
    g.close()
    return wikis, ids


def test_the_tag_index_counts_records_per_type(web):
    wikis, ids = web
    g = wikis.conn(wikis.global_wiki)
    index = {e["tag"]: e for e in relate.tag_index(g)}
    g.close()
    assert index["options"]["counts"] == {"rule": 2, "memory": 1, "issue": 1, "skill": 1, "document": 0,
                                          "task": 1, "job": 0, "feature": 0, "note": 0}
    assert index["options"]["total"] == 6 and index["rust"]["total"] == 1
    assert "human_task" in index  # the index lists system tags too
    assert list(index)[0] == "options"  # busiest first


def test_tag_counts_follow_every_write_path(web):
    wikis, ids = web
    g = wikis.conn(wikis.global_wiki)

    def snapshot():
        return sorted(tuple(r) for r in g.execute("SELECT tag, k, n FROM tag_counts"))

    def truth():
        return sorted(tuple(r) for r in g.execute("SELECT tag, substr(id, 1, 1), count(*) FROM tags GROUP BY 1, 2"))

    db.delete(g, [ids["r2"]])
    db.tag(g, ids["m2"], ["extra"])
    db.retag(g, ids["m1"], drop_prefix="stage:", privileged=True)
    db.sweep(g)
    assert snapshot() == truth()
    with db.write(g):  # a file written before the tag index existed
        for name in ("tag_counts_ai", "tag_counts_ad", "tag_counts_au"):
            g.execute(f"DROP TRIGGER {name}")
        g.execute("DROP TABLE tag_counts")
    g.close()
    g = wikis.conn(wikis.global_wiki)  # an older file's missing counts are rebuilt on connect
    assert snapshot() == truth() and snapshot()


def test_fetch_by_tag_needs_every_tag_and_ranks_lower_stages_first(web):
    wikis, ids = web
    both = fetch.tag_selection(["options", "python"], start=wikis.repo)
    assert sorted(i["id"] for i in both) == sorted([ids["r1"], ids["i1"]])
    got = fetch.tag_selection(["options"], start=wikis.repo, k=20)
    by = [i["id"] for i in got]
    assert by.index(ids["m1"]) < by.index(ids["r2"])  # stage 1 before stage 2
    assert {ids["m1"], ids["r2"]} == set(by[:2])  # staged ones lead; the rest are on demand
    assert fetch.tag_selection(["options", "rust"], start=wikis.repo) == []
    assert fetch.tag_selection(["nothing-has-this"], start=wikis.repo) == []
    types = {i["id"]: i["type"] for i in got}
    assert types[ids["t1"]] == "task" and types[ids["s1"]] == "skill" and types[ids["i1"]] == "issue"


def test_fetch_by_tag_with_a_query_ranks_by_relevance_then_the_rest(web):
    wikis, ids = web
    got = fetch.tag_selection(["options"], "walk-forward backtests", start=wikis.repo, k=3)
    assert got[0]["id"] == ids["r2"] and len(got) == 3


def test_the_cli_fetch_tag_flag(web, capsys):
    wikis, ids = web
    with pytest.raises(SystemExit) as e:
        fetch.main(["--cwd", str(wikis.repo), "--tag", "options,python"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and ids["r1"] in out and ids["i1"] in out and ids["m2"] not in out
    assert out.startswith(fetch.WRAPPER_OPEN)


def test_bad_tag_queries_are_refused(web):
    wikis, _ = web
    with pytest.raises(db.LedgerError):
        fetch.tag_selection([], start=wikis.repo)
    with pytest.raises(db.LedgerError):
        fetch.tag_selection([f"t{i}" for i in range(20)], start=wikis.repo)
    assert fetch.tag_selection(["x" * 500], start=wikis.repo) == []


def call(tool, args, cwd):
    r = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": tool, "arguments": args}}, cwd)
    return json.loads(r["result"]["content"][0]["text"])


def test_the_mcp_tools(web):
    wikis, ids = web
    cwd = str(wikis.repo)
    found = call("search", {"query": "", "tags": ["options", "python"]}, cwd)
    assert {r["id"] for r in found["results"]} == {ids["r1"], ids["i1"]} and found["mode"] == "tags"
    assert call("search", {"query": "x", "tags": "options"}, cwd)["error"]
    tags = call("tags", {}, cwd)["tags"]
    assert tags[0]["tag"] == "options" and tags[0]["counts"]["rule"] == 2
    rel = call("related", {"id": ids["r1"], "limit": 3}, cwd)
    assert rel["id"] == ids["r1"] and len(rel["related"]) == 3
    assert call("related", {"id": "R999"}, cwd)["error"]
    names = {t["name"] for t in mcp.TOOLS}
    assert {"tags", "related", "tasks", "project", "projects", "project_new", "project_add"} <= names
    assert set(mcp._HANDLERS) == names


def test_related_orders_linked_then_shared_tags_then_newest(web):
    wikis, ids = web
    g = wikis.conn(wikis.global_wiki)
    db.link(g, ids["m2"], ids["r1"], "related")  # linked, though it shares no tag (and links point in)
    first = relate.related(g, ids["r1"], 10)
    assert first[0]["id"] == ids["m2"] and first[0]["links"] == ["related"] and first[0]["shared"] == []
    shared = {r["id"]: r["shared"] for r in first[1:]}
    assert shared[ids["i1"]] == ["options", "python"]  # two shared tags before one
    assert list(shared)[0] == ids["i1"]
    assert set(shared) == {ids["i1"], ids["r2"], ids["m1"], ids["t1"], ids["s1"]}
    assert len(relate.related(g, ids["r1"], 2)) == 2
    assert len(relate.related(g, ids["r1"], 999)) <= relate.RELATED_MAX
    assert relate.related(g, ids["m2"], 10)[0]["id"] == ids["r1"]  # the link reads both ways
    g.close()


def test_system_tags_do_not_relate_records(web):
    wikis, ids = web
    g = wikis.conn(wikis.global_wiki)
    a, _ = db.add_row(g, "R", "A rule", "", tags=["stage:5"])
    b, _ = db.add_row(g, "R", "Another rule", "", tags=["stage:5"])
    assert relate.related(g, a) == []
    g.close()


def test_a_record_that_links_to_itself_and_a_dangling_link(web):
    wikis, ids = web
    g = wikis.conn(wikis.global_wiki)
    db.link(g, ids["r1"], ids["r1"], "related")
    db.link(g, ids["r1"], "R4040", "related")
    out = relate.related(g, ids["r1"], 50)
    assert all(r["id"] not in (ids["r1"], "R4040") for r in out)
    g.close()


def test_a_tag_on_thousands_of_records_does_not_make_related_slow(wikis):
    import time

    registry.write_format(wikis.global_wiki)
    g = wikis.conn(wikis.global_wiki)
    with db.write(g):
        for i in range(1, 3001):
            g.execute("INSERT INTO mri(id, kind, summary, body, hash, created, updated) VALUES (?,?,?,?,?,?,?)",
                      (f"M{i}", "M", f"m{i}", "", f"h{i}", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            g.execute("INSERT INTO tags(id, tag) VALUES (?, 'everything')", (f"M{i}",))
    rid, _ = db.add_row(g, "R", "Lonely rule", "", tags=["everything", "specific"])
    start = time.perf_counter()
    found = relate.related(g, rid)
    assert len(found) == relate.RELATED_DEFAULT  # a tag on thousands of records still answers ...
    assert all(r["shared"] == ["everything"] for r in found)
    assert time.perf_counter() - start < 0.1  # ... from bounded work
    # and the rarest tag comes first: a record sharing only the rare tag is found before the crowd
    pal, _ = db.add_row(g, "R", "A pal", "", tags=["specific"])
    assert relate.related(g, rid)[0]["id"] == pal
    g.close()


def test_ten_thousand_tags_in_the_index(wikis):
    import time

    registry.write_format(wikis.global_wiki)
    g = wikis.conn(wikis.global_wiki)
    with db.write(g):
        g.execute("INSERT INTO mri(id, kind, summary, body, hash, created, updated) VALUES "
                  "('M1','M','s','','h','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        for i in range(10_000):
            g.execute("INSERT INTO tags(id, tag) VALUES ('M1', ?)", (f"tag-{i:05d}",))
    start = time.perf_counter()
    idx = relate.tag_index(g)
    assert len(idx) == 10_000 and time.perf_counter() - start < 1.0
    start = time.perf_counter()
    assert len(relate.topics(g)) == relate.TOPICS_SHOWN
    assert time.perf_counter() - start < 0.1
    g.close()
