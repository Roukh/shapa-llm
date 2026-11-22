"""The configurable hierarchy: the global tree (levels, types, groups, limits), each repo's own settings,
the TOML round trip, the commands, the session-start line and `global_access`."""

import json

import pytest

from shapa import bootstrap, config, db, fetch, hierarchy
from tests.learnkit import at, lexical_only  # noqa: F401


def run(*argv):
    return hierarchy.run(list(argv))


@pytest.fixture
def tree(wikis, monkeypatch):
    """The global wiki and one repo wiki, cwd inside the repo."""
    monkeypatch.chdir(wikis.repo)
    return wikis


def gcfg(wikis):
    return hierarchy.load_global(wikis.global_wiki)


# --- the TOML file ---------------------------------------------------------------------------------

def test_the_global_config_round_trips_what_the_writer_wrote(tmp_path):
    cfg = hierarchy.empty_global()
    cfg.update(global_store="hosted", levels=["project", "subproject", "repo"], types=["main", "sub"],
               max_depth=4, max_children=6)
    hierarchy.add_group(cfg, ["acme"])
    hierarchy.add_group(cfg, ["acme", "core"])
    hierarchy.add_group(cfg, ["acme", "edge"])
    hierarchy.link(cfg, ["acme", "core"], "api")
    hierarchy.link(cfg, ["acme"], "web")
    path = tmp_path / "shapa.toml"
    hierarchy.save_global(path.parent, cfg)
    assert hierarchy.load_global(path.parent) == cfg


def test_an_absent_config_reads_as_the_defaults_and_nothing_is_written(tmp_path):
    cfg = hierarchy.load_global(tmp_path)
    assert cfg == hierarchy.empty_global() and cfg["global_store"] == "local"
    assert not (tmp_path / "shapa.toml").exists()


def test_a_malformed_or_foreign_config_is_refused_with_its_reason(tmp_path):
    (tmp_path / "shapa.toml").write_text('global_store = "cloud"\n', encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError, match="global_store"):
        hierarchy.load_global(tmp_path)
    (tmp_path / "shapa.toml").write_text("levels = [", encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError, match="not valid TOML"):
        hierarchy.load_global(tmp_path)
    (tmp_path / "shapa.toml").write_text('[tree.a]\nrepos = "x"\n', encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError, match="repos"):
        hierarchy.load_global(tmp_path)


def test_the_repo_config_round_trips_and_only_known_keys_count(tmp_path):
    hierarchy.save_repo(tmp_path, {"type": "sub", "global_access": 2, "read_stage": 3})
    assert hierarchy.load_repo(tmp_path) == {"type": "sub", "global_access": 2, "read_stage": 3}
    (tmp_path / "shapa.toml").write_text("read_stage = 12\n", encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError, match="read_stage"):
        hierarchy.load_repo(tmp_path)


# --- the tree and its limits -------------------------------------------------------------------------

def test_a_tree_nests_and_branches_without_a_built_in_limit():
    cfg = hierarchy.empty_global()
    path = []
    for n in range(12):
        path.append(f"g{n}")
        hierarchy.add_group(cfg, list(path))
    hierarchy.link(cfg, path, "deep")
    assert hierarchy.path_of(cfg, "deep") == path
    for n in range(30):
        hierarchy.add_group(cfg, ["g0", f"branch{n}"])


def test_the_limits_are_enforced_on_write():
    cfg = hierarchy.empty_global()
    cfg.update(max_depth=3, max_children=2)
    hierarchy.add_group(cfg, ["a"])
    hierarchy.add_group(cfg, ["a", "b"])
    with pytest.raises(hierarchy.HierarchyError, match="max_depth"):
        hierarchy.add_group(cfg, ["a", "b", "c"])        # a repo below it would be a 4th level
    hierarchy.link(cfg, ["a", "b"], "r1")
    hierarchy.link(cfg, ["a", "b"], "r2")
    with pytest.raises(hierarchy.HierarchyError, match="max_children"):
        hierarchy.link(cfg, ["a", "b"], "r3")
    hierarchy.add_group(cfg, ["z"])
    with pytest.raises(hierarchy.HierarchyError, match="max_children"):
        hierarchy.add_group(cfg, ["q"])                  # a third group at the top


def test_a_repo_is_linked_once_and_unlink_frees_it():
    cfg = hierarchy.empty_global()
    hierarchy.add_group(cfg, ["a"])
    hierarchy.add_group(cfg, ["b"])
    hierarchy.link(cfg, ["a"], "r")
    with pytest.raises(hierarchy.HierarchyError, match="already linked under a"):
        hierarchy.link(cfg, ["b"], "r")
    assert hierarchy.unlink(cfg, "r") == ["a"]
    hierarchy.link(cfg, ["b"], "r")
    assert hierarchy.path_of(cfg, "r") == ["b"] and hierarchy.siblings(cfg, "r") == []


def test_names_that_would_break_the_file_are_refused():
    cfg = hierarchy.empty_global()
    for bad in ("", "a b", 'a"b', "repos", "../x", "x" * 60):
        with pytest.raises(hierarchy.HierarchyError):
            hierarchy.add_group(cfg, [bad])
    with pytest.raises(hierarchy.HierarchyError):
        hierarchy.add_level(cfg, "no spaces allowed")


def test_check_reports_every_problem_it_finds():
    cfg = hierarchy.empty_global()
    cfg.update(types=["main"], max_children=1)
    hierarchy.add_group(cfg, ["a"])
    cfg["tree"]["groups"]["a"]["repos"] = ["x", "y"]               # written by hand, over the limit
    cfg["tree"]["groups"]["b"] = {"repos": ["x"], "groups": {}}    # and a repo linked twice
    problems = hierarchy.check(cfg, repo_type="odd")
    text = " ".join(problems)
    assert "max_children" in text and "linked twice" in text and "odd" in text
    assert hierarchy.check(hierarchy.empty_global()) == []


# --- the commands --------------------------------------------------------------------------------------

def test_the_commands_build_the_tree_and_show_it(tree, capsys):
    assert run("level", "add", "project") == 0 and run("level", "add", "repo") == 0
    assert run("type", "add", "main") == 0
    assert run("group", "acme/core") == 0
    assert run("link", "acme/core") == 0                 # this repo, by its own key
    assert run("show", "--json") == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["levels"] == ["project", "repo"] and out["types"] == ["main"]
    assert out["tree"]["groups"]["acme"]["groups"]["core"]["repos"] == ["my-repo"]
    assert hierarchy.load_global(tree.global_wiki)["tree"]["groups"]["acme"]
    assert run("unlink") == 0 and hierarchy.path_of(gcfg(tree), "my-repo") is None


def test_a_refused_command_exits_2_and_changes_nothing(tree, capsys):
    run("group", "a")
    before = (tree.global_wiki / "shapa.toml").read_text(encoding="utf-8")
    assert run("link", "missing/group") == 2
    assert "no group" in capsys.readouterr().err
    assert (tree.global_wiki / "shapa.toml").read_text(encoding="utf-8") == before


def test_repo_commands_set_the_type_the_stage_and_the_access(tree, capsys):
    run("type", "add", "main")
    assert hierarchy.run_repo(["type", "main"]) == 0
    assert hierarchy.run_repo(["stage", "2"]) == 0
    assert hierarchy.run_repo(["access", "4"]) == 0
    assert hierarchy.load_repo(tree.wiki) == {"type": "main", "global_access": 4, "read_stage": 2}
    assert hierarchy.run_repo(["type", "weird"]) == 2           # not one of the configured types
    assert hierarchy.run_repo(["access", "unset"]) == 0
    assert hierarchy.load_repo(tree.wiki)["global_access"] is None
    assert hierarchy.run_repo(["stage", "12"]) == 2
    capsys.readouterr()
    assert hierarchy.run_repo(["stage"]) == 0 and capsys.readouterr().out.strip() == "2"


def test_store_is_local_unless_the_operator_sets_it(tree):
    assert gcfg(tree)["global_store"] == "local"
    assert run("store", "hosted") == 0 and gcfg(tree)["global_store"] == "hosted"
    assert run("store", "cloud") == 2


def test_init_writes_the_repo_config_with_the_first_type(tmp_path, monkeypatch, wikis):
    from shapa import cli

    run("type", "add", "main")
    run("type", "add", "sub")
    target = tmp_path / "fresh" / ".shapa"
    monkeypatch.chdir(tmp_path)
    cli.main(["init", str(target)])
    assert hierarchy.load_repo(target)["type"] == "main"
    cli.main(["init", str(target)])                      # a second init leaves the file alone
    assert hierarchy.load_repo(target)["type"] == "main"


# --- the session-start line ---------------------------------------------------------------------------

def line_for(wikis):
    return hierarchy.session_line(wikis.wiki)


def test_the_line_names_the_path_the_type_and_the_siblings(tree):
    run("type", "add", "main")
    run("group", "acme/core")
    run("link", "acme/core")
    run("link", "acme/core", "other-repo")
    hierarchy.run_repo(["type", "main"])
    assert line_for(tree) == "HIERARCHY: acme > core > my-repo (type main); siblings: other-repo"


def test_the_line_is_empty_when_nothing_is_configured_and_never_raises(tree):
    assert line_for(tree) == ""
    (tree.global_wiki / "shapa.toml").write_text("not = [valid", encoding="utf-8")
    assert line_for(tree) == ""


def test_the_line_caps_its_siblings(tree):
    run("group", "g")
    run("link", "g")
    for n in range(12):
        run("link", "g", f"sib{n:02d}")
    got = line_for(tree)
    names = got.split("siblings: ")[1].split(" (+")[0].split(", ")
    assert len(names) == hierarchy.MAX_SIBLINGS and got.endswith("(+4 more)")


def test_session_start_prints_the_line_after_the_fires_and_before_the_mempm(tree, monkeypatch):
    from shapa import focus

    monkeypatch.setattr(bootstrap, "_learning_off", lambda: True)
    c = db.connect(tree.wiki)
    focus.set_northstar(c, "ship it")
    c.close()
    c = db.connect(tree.global_wiki)
    db.add_row(c, "R", "A baseline rule.", "", tags=["stage:0"], privileged=True)
    c.close()
    run("group", "acme")
    run("link", "acme")
    text = bootstrap.build_focused(config.wiki_roots(tree.repo))
    lines = text.splitlines()
    at_line = next(i for i, ln in enumerate(lines) if ln.startswith(hierarchy.PREFIX))
    assert lines[at_line] == "HIERARCHY: acme > my-repo"
    north = next(i for i, ln in enumerate(lines) if ln.startswith("northstar [my-repo]"))
    assert north < at_line and lines[at_line + 1].startswith("MEMPM")


# --- global_access -------------------------------------------------------------------------------------

def fill_global(wikis):
    c = db.connect(wikis.global_wiki)
    ids = {}
    ids["base"], _ = db.add_row(c, "R", "Baseline rule zero.", "", tags=["stage:0"], privileged=True)
    ids["two"], _ = db.add_row(c, "R", "Rule at stage two.", "", tags=["stage:2"], privileged=True)
    ids["five"], _ = db.add_row(c, "M", "Memory at stage five.", "", tags=["stage:5"], privileged=True)
    ids["plain"], _ = db.add_row(c, "M", "An untagged memory.", "")
    ids["issue"], _ = db.add_row(c, "I", "A mistake with a lesson.", "", source="correction")
    c.close()
    return ids


def served(text):
    return "\n".join(ln for ln in text.splitlines() if ln.startswith("{"))


def test_unset_global_access_keeps_session_start_exactly_as_it_is(tree, monkeypatch):
    fill_global(tree)
    monkeypatch.setattr(bootstrap, "_learning_off", lambda: True)
    before = bootstrap.build_focused(config.wiki_roots(tree.repo), stage=9)
    hierarchy.save_repo(tree.wiki, {"type": None, "global_access": None, "read_stage": 0})
    assert bootstrap.build_focused(config.wiki_roots(tree.repo), stage=9) == before
    assert "Memory at stage five." in before and "A mistake with a lesson." in before


def test_global_access_leaves_out_global_records_above_it_and_untagged_ones(tree, monkeypatch):
    ids = fill_global(tree)
    monkeypatch.setattr(bootstrap, "_learning_off", lambda: True)
    hierarchy.run_repo(["access", "2"])
    got = served(bootstrap.build_focused(config.wiki_roots(tree.repo), stage=9))
    assert "Baseline rule zero." in got and "Rule at stage two." in got
    assert "Memory at stage five." not in got and "An untagged memory." not in got
    assert "A mistake with a lesson." not in got                    # an issue is untagged: stage 9
    hierarchy.run_repo(["access", "9"])
    got = served(bootstrap.build_focused(config.wiki_roots(tree.repo), stage=9))
    assert "Memory at stage five." in got and "A mistake with a lesson." in got
    assert ids["base"] in got


def test_global_access_caps_the_requested_stage_too(tree, monkeypatch):
    fill_global(tree)
    monkeypatch.setattr(bootstrap, "_learning_off", lambda: True)
    hierarchy.run_repo(["access", "0"])
    got = served(bootstrap.build_focused(config.wiki_roots(tree.repo), stage=9))
    assert "Baseline rule zero." in got and "Rule at stage two." not in got


def test_global_access_filters_recall(tree, lexical_only):
    fill_global(tree)
    query = "memory stage five untagged rule two"

    def recalled():
        sel = fetch.select_multi(query, roots=config.wiki_roots(tree.repo), read_only=True)
        return " ".join(summary for _, summary in sel.items)

    before = recalled()
    assert "Memory at stage five" in before and "untagged memory" in before and "Rule at stage two" in before
    hierarchy.run_repo(["access", "2"])
    after = recalled()
    assert "Rule at stage two" in after
    assert "Memory at stage five" not in after and "untagged memory" not in after
    assert fetch.tag_selection(["stage:5"], roots=config.wiki_roots(tree.repo)) == []
    hierarchy.run_repo(["access", "unset"])
    assert fetch.tag_selection(["stage:5"], roots=config.wiki_roots(tree.repo))


# --- the MCP tools -------------------------------------------------------------------------------------

def call(name, args, cwd):
    from shapa import mcp

    resp = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": args}}, str(cwd))
    return json.loads(resp["result"]["content"][0]["text"]), resp["result"]["isError"]


def test_an_agent_links_its_repo_through_the_mcp_tool(tree):
    got, err = call("hierarchy", {"action": "group", "path": "acme/core"}, tree.repo)
    assert not err
    got, err = call("hierarchy", {"action": "link", "path": "acme/core"}, tree.repo)
    assert not err and got["tree"]["groups"]["acme"]["groups"]["core"]["repos"] == ["my-repo"]
    got, err = call("hierarchy", {"action": "check"}, tree.repo)
    assert got == {"ok": True, "problems": []} and not err
    got, err = call("hierarchy", {"action": "unlink"}, tree.repo)
    assert not err and hierarchy.path_of(gcfg(tree), "my-repo") is None


def test_a_refused_mcp_edit_is_an_error_result_and_changes_nothing(tree):
    got, err = call("hierarchy", {"action": "link", "path": "nowhere"}, tree.repo)
    assert err and "no group" in got["error"]
    assert not (tree.global_wiki / "shapa.toml").exists()


def test_the_mcp_tools_cannot_widen_access_or_move_the_global_store(tree):
    for args in ({"action": "store", "value": "hosted"},):
        got, err = call("hierarchy", args, tree.repo)
        assert err and "operator's CLI" in got["error"]
    for action in ("stage", "access"):
        got, err = call("repo", {"action": action, "value": "0"}, tree.repo)
        assert err and "operator's CLI" in got["error"]
    assert hierarchy.load_repo(tree.wiki) == {"type": None, "global_access": None, "read_stage": 0}
    assert gcfg(tree)["global_store"] == "local"


def test_the_repo_tool_sets_and_shows_the_type(tree):
    call("hierarchy", {"action": "type", "name": "main", "op": "add"}, tree.repo)
    got, err = call("repo", {"action": "type", "value": "main"}, tree.repo)
    assert not err and got["type"] == "main"
    got, err = call("repo", {"action": "show"}, tree.repo)
    assert got == {"type": "main", "global_access": None, "read_stage": 0, "key": "my-repo", "path": []}
    got, err = call("repo", {"action": "type", "value": "odd"}, tree.repo)
    assert err


# --- global_store = hosted -------------------------------------------------------------------------------

def test_the_hosted_mcp_is_named_only_when_logged_in_and_the_store_is_hosted(tree, capsys, monkeypatch):
    from shapa import remote, syncstate

    def mcp_url():
        capsys.readouterr()
        code = remote.run(["mcp"])
        return code, capsys.readouterr().out.strip()

    assert mcp_url() == (1, "")                                     # logged out, local store
    syncstate.save_credentials(syncstate.Credentials("https://hosted.example", "laptop", "d1", "shp_d1_" + "s" * 40))
    assert mcp_url() == (1, "")                                     # logged in, the store is local: nothing changes
    run("store", "hosted")
    assert mcp_url() == (0, "https://hosted.example/mcp")
    syncstate.clear_credentials()
    assert mcp_url() == (1, "")                                     # hosted but logged out


def test_remote_status_says_the_global_wiki_is_a_cache_when_the_store_is_hosted(tree, capsys):
    from shapa import remote, syncstate

    syncstate.save_credentials(syncstate.Credentials("https://hosted.example", "laptop", "d1", "shp_d1_" + "s" * 40))
    remote.run(["status", "--cwd", str(tree.repo)])
    assert "cache" not in capsys.readouterr().out
    run("store", "hosted")
    remote.run(["status", "--cwd", str(tree.repo)])
    assert "global wiki: a cache of the hosted store" in capsys.readouterr().out


def test_a_hostile_hand_edited_tree_is_refused_not_crashed_on(tmp_path):
    deep = "[tree" + "".join(f'."g{i}"' for i in range(1500)) + "]\nrepos = []\n"
    (tmp_path / "shapa.toml").write_text(deep, encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError):
        hierarchy.load_global(tmp_path)
    (tmp_path / "shapa.toml").write_text('[tree."a b"]\nrepos = []\n', encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError, match="not a group name"):
        hierarchy.load_global(tmp_path)
    (tmp_path / "shapa.toml").write_text('levels = ["ok", "ok"]\n', encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError, match="levels"):
        hierarchy.load_global(tmp_path)
    assert hierarchy.session_line(tmp_path) == ""


# --- break-it regressions ---------------------------------------------------------------------------------

def test_get_does_not_open_a_global_record_above_the_repos_global_access(tree):
    ids = fill_global(tree)
    for key in ("five", "plain", "two"):
        got, err = call("get", {"id": ids[key]}, tree.repo)
        assert not err, key
    hierarchy.run_repo(["access", "2"])
    got, err = call("get", {"id": ids["two"]}, tree.repo)
    assert not err and got["meta"]["summary"] == "Rule at stage two."
    for key in ("five", "plain", "issue"):
        got, err = call("get", {"id": ids[key]}, tree.repo)
        assert err and "no note" in got["error"], key
    hierarchy.run_repo(["access", "unset"])
    got, err = call("get", {"id": ids["five"]}, tree.repo)
    assert not err


def test_a_name_with_a_trailing_newline_is_refused_everywhere(tmp_path):
    cfg = hierarchy.empty_global()
    with pytest.raises(hierarchy.HierarchyError):
        hierarchy.add_group(cfg, ["acme\n"])
    with pytest.raises(hierarchy.HierarchyError):
        hierarchy.add_type(cfg, "main\n")
    hierarchy.add_group(cfg, ["acme"])
    with pytest.raises(hierarchy.HierarchyError):
        hierarchy.link(cfg, ["acme"], "repo\n")
    (tmp_path / "shapa.toml").write_text('type = "main\\n"\n', encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError):
        hierarchy.load_repo(tmp_path)           # a cloned repo's file must not put a newline into the session text
    (tmp_path / "shapa.toml").write_text('[tree."a\\n"]\nrepos = []\n', encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError):
        hierarchy.load_global(tmp_path)


def test_an_oversized_config_file_is_refused_and_session_start_still_works(tree, monkeypatch):
    monkeypatch.setattr(bootstrap, "_learning_off", lambda: True)
    (tree.wiki / "shapa.toml").write_text("# x\n" * (hierarchy.MAX_FILE_BYTES // 4 + 10), encoding="utf-8")
    with pytest.raises(hierarchy.HierarchyError, match="larger than"):
        hierarchy.load_repo(tree.wiki)
    assert hierarchy.global_access(tree.wiki) is None
    assert hierarchy.session_line(tree.wiki) == ""
    bootstrap.build_focused(config.wiki_roots(tree.repo))       # does not raise


def test_a_rewritten_remote_url_is_not_wired_into_a_harness(tree, capsys):
    from shapa import remote, syncstate

    run("store", "hosted")
    for url in ("http://evil.example", "https://user:pw@hosted.example", "ftp://hosted.example", "https://"):
        syncstate.save_credentials(syncstate.Credentials(url, "laptop", "d1", "shp_d1_" + "s" * 40))
        capsys.readouterr()
        assert remote.run(["mcp"]) == 1 and capsys.readouterr().out == "", url


def test_a_symlink_at_the_temp_name_is_not_written_through(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "shapa.toml.tmp").symlink_to(victim)
    hierarchy.save_repo(wiki, {"type": "main", "global_access": None, "read_stage": 0})
    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert hierarchy.load_repo(wiki)["type"] == "main"
    cfg = hierarchy.empty_global()
    hierarchy.save_global(wiki, cfg)
    assert victim.read_text(encoding="utf-8") == "precious\n"
