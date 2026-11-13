"""Two-way sync of a wiki with the hosted store, against the in-process double."""

import json

import pytest

from shapa import db, remotehttp, sync, syncstate
from tests.hosted_double import HostedDouble

KEY = "my-repo"
URL = "https://hosted.example"


class Device:
    """One machine: its own wiki, its own remote dir, a client onto the double."""

    def __init__(self, tmp_path, name, double, *, wiki=None):
        from tests.conftest import Wikis

        self.name = name
        self.wikis = Wikis(tmp_path / name)
        self.wiki = wiki or self.wikis.wiki
        self.rdir = tmp_path / f"{name}-remote"
        self.double = double
        _, token = double.add_device(name)
        self.client = remotehttp.Client(URL, token, transport=double.transport)

    def sync(self, **kw):
        kw.setdefault("key", KEY)
        kw.setdefault("name", KEY)
        kw.setdefault("kind", "repo")
        return sync.sync_wiki(self.wiki, client=self.client, rdir=self.rdir, **kw)

    def conn(self):
        return db.connect(self.wiki)

    def row(self, rid):
        c = self.conn()
        try:
            return db.get_row(c, rid)
        finally:
            c.close()

    def add_row(self, kind, summary, body="", **kw):
        c = self.conn()
        try:
            return db.add_row(c, kind, summary, body, **kw)[0]
        finally:
            c.close()


@pytest.fixture
def double():
    return HostedDouble()


@pytest.fixture
def a(tmp_path, double):
    return Device(tmp_path, "a", double)


@pytest.fixture
def b(tmp_path, double):
    return Device(tmp_path, "b", double)


def test_a_new_row_goes_up_and_comes_down_on_the_other_device(a, b, double):
    rid = a.add_row("R", "always run the tests", "before merging", tags=["ci"])
    res = a.sync()
    assert res.status == "ok" and res.sent_put == 1
    assert double.live(KEY)[rid]["data"]["summary"] == "always run the tests"
    res = b.sync()
    assert res.received >= 1
    got = b.row(rid)
    assert got.summary == "always run the tests" and got.tags == ["ci"]
    assert got.created == a.row(rid).created


# --- helpers -------------------------------------------------------------------------------

def set_cols(device, table, rid, **cols):
    c = device.conn()
    try:
        sets = ", ".join(f"{k} = ?" for k in cols)
        c.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*cols.values(), rid))
    finally:
        c.close()


def rows_of(device):
    c = device.conn()
    try:
        return {r.id: r for r in db.rows(c)}
    finally:
        c.close()


def items_of(device):
    c = device.conn()
    try:
        return {i.id: i for i in db.items(c)}
    finally:
        c.close()


# --- classification -------------------------------------------------------------------------

def test_a_modified_row_is_sent_with_its_base_rev(a, double):
    rid = a.add_row("R", "rule one")
    a.sync()
    c = a.conn()
    db.update_row(c, rid, body="now with a body")
    c.close()
    res = a.sync()
    assert res.sent_put == 1
    req = double.sync_requests[-1][1]
    assert req["ops"][0]["op"] == "put" and req["ops"][0]["base_rev"] == 1
    assert double.live(KEY)[rid]["data"]["body"] == "now with a body"
    assert a.sync().sent_put == 0  # nothing left to send


def test_a_local_delete_becomes_a_del_op_and_reaches_the_other_device(a, b, double):
    rid = a.add_row("M", "to be deleted")
    a.sync()
    b.sync()
    assert rid in rows_of(b)
    c = a.conn()
    db.delete(c, [rid])
    c.close()
    res = a.sync()
    assert res.sent_del == 1
    assert double.wiki(KEY).records[rid]["deleted"] is True
    c = a.conn()
    assert db.tombstones(c) == []  # acknowledged tombstones are dropped
    c.close()
    b.sync()
    assert rid not in rows_of(b)
    c = b.conn()
    assert db.tombstones(c) == []  # a remote-originated delete leaves no tombstone
    c.close()


def test_a_row_missing_with_no_tombstone_is_fetched_not_deleted(a, double):
    rid = a.add_row("M", "swapped away")
    a.sync()
    c = a.conn()
    c.execute("DELETE FROM mri WHERE id = ?", (rid,))  # as if git swapped the database file
    c.close()
    res = a.sync()
    assert res.sent_del == 0 and res.wanted == 1
    assert double.live(KEY)[rid]  # the server still has it
    assert rid in rows_of(a)      # and the local copy healed


def test_a_database_swapped_to_an_older_copy_heals_from_the_server(a, double):
    r1 = a.add_row("R", "first rule", "old body")
    a.sync()
    older = db.db_path(a.wiki).read_bytes()
    c = a.conn()
    db.update_row(c, r1, body="new body")
    c.close()
    set_cols(a, "mri", r1, updated="2999-01-01T00:00:00Z")
    r2 = a.add_row("R", "second rule")
    a.sync()
    assert double.live(KEY)[r1]["data"]["body"] == "new body"
    db.db_path(a.wiki).write_bytes(older)  # a checkout swapped the tracked database
    res = a.sync()
    assert res.sent_del == 0
    assert set(double.live(KEY)) >= {r1, r2}
    assert rows_of(a)[r1].body == "new body" and r2 in rows_of(a)


def test_stale_local_edit_does_not_overwrite_a_newer_server_record(a, double):
    rid = a.add_row("R", "stale rule", "v1")
    a.sync()
    # the server record moves on (another device), and ours is an older copy
    rec = dict(double.wiki(KEY).records[rid])
    rec["data"] = {**rec["data"], "body": "v2 from elsewhere"}
    rec["updated"] = "2999-01-01T00:00:00Z"
    double.remote_put(KEY, rec)
    c = a.conn()
    db.update_row(c, rid, body="v1 edited offline")
    c.close()
    set_cols(a, "mri", rid, updated="2000-01-01T00:00:00Z")
    a.sync()
    assert rows_of(a)[rid].body == "v2 from elsewhere"
    assert double.live(KEY)[rid]["data"]["body"] == "v2 from elsewhere"


# --- server results ------------------------------------------------------------------------------

def test_noop_when_the_server_already_has_the_same_content(a, double):
    rid = a.add_row("M", "same everywhere")
    a.sync()
    (a.rdir / "sync").joinpath(*[]).mkdir(parents=True, exist_ok=True)
    for f in (a.rdir / "sync").glob("*.json"):
        f.unlink()  # lost state: everything looks new again
    res = a.sync()
    assert res.status == "ok" and res.sent_put == 1
    assert list(double.live(KEY)) == [rid] and double.wiki(KEY).seq == 1  # no new rev


def test_rejected_put_takes_the_servers_newer_version(a, b, double):
    rid = a.add_row("R", "contested", "base")
    a.sync()
    b.sync()
    c = b.conn()
    db.update_row(c, rid, body="b wins")
    c.close()
    set_cols(b, "mri", rid, updated="2999-01-01T00:00:00Z")
    b.sync()
    c = a.conn()
    db.update_row(c, rid, body="a loses")
    c.close()
    a.sync()
    assert rows_of(a)[rid].body == "b wins"
    assert double.live(KEY)[rid]["data"]["body"] == "b wins"


def test_a_label_collision_on_rows_renames_the_later_record_and_rewrites_references(a, b, double):
    ra = a.add_row("R", "rule from a", tags=["x"])
    a.sync()
    rb = b.add_row("R", "rule from b", tags=["mine"])
    follower = b.add_row("M", "mentions the rule", about=[rb])
    assert rb == ra == "R1"
    set_cols(b, "mri", rb, created="2020-01-01T00:00:00Z")
    res = b.sync()
    assert res.renamed == 1
    rows = rows_of(b)
    assert rows["R1"].summary == "rule from a"           # the server's R1 came down
    assert rows["R2"].summary == "rule from b" and rows["R2"].tags == ["mine"]
    c = b.conn()
    assert (follower, "R2", "about") in db.links_of(c, follower)
    c.close()
    # M sorts before R in the batch, so the server saw the follower's old link first;
    # the next sync pushes the relabeled link and the store converges.
    b.sync()
    a.sync()
    assert rows_of(a)["R2"].summary == "rule from b"
    assert {i: r["data"]["summary"] for i, r in double.live(KEY).items() if i[0] == "R"} == {
        "R1": "rule from a", "R2": "rule from b"}
    assert [["R2", "about"]] == double.live(KEY)[follower]["links"]


def test_a_label_collision_on_ledger_items_renames_children_with_their_parent(a, b, double):
    c = a.conn()
    fa = db.add_item(c, "F", "feature a")
    db.add_item(c, "J", "job a", parent=fa)
    c.close()
    a.sync()
    c = b.conn()
    fb = db.add_item(c, "F", "feature b")
    jb = db.add_item(c, "J", "job b", parent=fb)
    db.tag(c, jb, ["urgent"])
    c.close()
    set_cols(b, "items", fb, created="2020-01-01T00:00:00Z")
    set_cols(b, "items", jb, created="2020-01-01T00:00:01Z")
    b.sync()
    items = items_of(b)
    assert items["F1"].title == "feature a" and items["J1"].parent == "F1"
    assert items["F2"].title == "feature b" and items["J2"].title == "job b"
    assert items["J2"].parent == "F2"
    c = b.conn()
    assert db.get_row(c, "J2") is None
    assert db._tags_of(c, "J2") == ["urgent"]
    c.close()
    assert double.live(KEY)["J2"]["data"]["parent"] == "F2"
    a.sync()
    assert items_of(a)["J2"].parent == "F2"


def test_two_devices_converge(a, b, double):
    a.add_row("R", "shared rule")
    b.add_row("M", "b's memory")
    for _ in range(3):
        a.sync()
        b.sync()
    ra, rb = rows_of(a), rows_of(b)
    assert {i: r.summary for i, r in ra.items()} == {i: r.summary for i, r in rb.items()}
    assert len(ra) == 2
    assert {r.summary for r in ra.values()} == {"shared rule", "b's memory"}


def test_counters_rise_to_the_servers_so_new_labels_do_not_collide(a, b):
    for i in range(3):
        a.add_row("M", f"memory {i}")
    a.sync()
    b.sync()
    nxt = b.add_row("M", "b's next")
    assert nxt == "M4"


# --- a write during the network call -------------------------------------------------------------

def test_a_local_write_during_the_network_call_is_not_clobbered(a, b, double):
    rid = a.add_row("R", "race rule", "v1")
    a.sync()
    b.sync()
    c = b.conn()
    db.update_row(c, rid, body="from b")
    c.close()
    set_cols(b, "mri", rid, updated="2999-01-01T00:00:00Z")
    b.sync()

    def local_edit(key, req):
        c = a.conn()
        db.update_row(c, rid, body="typed during the sync")
        c.close()

    double.on_sync = local_edit
    res = a.sync()
    double.on_sync = None
    assert rows_of(a)[rid].body == "typed during the sync"
    assert res.skipped == 1


# --- notes ----------------------------------------------------------------------------------------

def test_note_files_sync_both_ways_and_root_rules_never_do(a, b, double):
    (a.wiki / "arch").mkdir()
    (a.wiki / "arch" / "design.md").write_text("# design\n", encoding="utf-8")
    (a.wiki / "research").mkdir()
    (a.wiki / "research" / "topic.md").write_text("findings\n", encoding="utf-8")
    (a.wiki / "loose.md").write_text("a root note\n", encoding="utf-8")
    a.sync()
    assert set(double.live(KEY)) == {"arch/design.md", "research/topic.md", "loose.md"}
    b.sync()
    assert (b.wiki / "arch" / "design.md").read_text() == "# design\n"
    assert (b.wiki / "loose.md").read_text() == "a root note\n"
    assert (b.wiki / "AGENTS.md").read_text() == "# marker\n"  # shipped rules untouched
    (b.wiki / "arch" / "design.md").write_text("# design v2\n", encoding="utf-8")
    b.sync()
    a.sync()
    assert (a.wiki / "arch" / "design.md").read_text() == "# design v2\n"


def test_a_remote_note_delete_removes_an_unchanged_file_and_keeps_an_edited_one(a, double):
    (a.wiki / "gone.md").write_text("bye\n", encoding="utf-8")
    (a.wiki / "kept.md").write_text("original\n", encoding="utf-8")
    a.sync()
    double.remote_delete(KEY, "gone.md")
    double.remote_delete(KEY, "kept.md")
    (a.wiki / "kept.md").write_text("edited locally\n", encoding="utf-8")
    a.sync()
    assert not (a.wiki / "gone.md").exists()
    assert (a.wiki / "kept.md").read_text() == "edited locally\n"


# --- pagination, dry run, if-changed ---------------------------------------------------------------

def test_a_long_change_feed_is_paged(tmp_path):
    double = HostedDouble(page_size=2)
    a, b = Device(tmp_path, "a", double), Device(tmp_path, "b", double)
    for i in range(5):
        a.add_row("M", f"memory number {i}")
    a.sync()
    before = len(double.sync_requests)
    b.sync()
    assert len(double.sync_requests) - before >= 3
    assert len(rows_of(b)) == 5


def test_dry_run_changes_nothing_locally_or_remotely(a, double):
    a.add_row("M", "first")
    before = db.db_path(a.wiki).read_bytes()
    res = a.sync(dry_run=True)
    assert res.sent_put == 1
    assert db.db_path(a.wiki).read_bytes() == before
    assert not list((a.rdir / "sync").glob("*.json")) if (a.rdir / "sync").exists() else True
    assert not [c for c in double.calls if c[0] in ("PUT",)]
    a.sync()
    a.add_row("M", "second")
    snapshot = json.dumps(double.wiki(KEY).records, sort_keys=True)
    state = [p.read_bytes() for p in (a.rdir / "sync").glob("*.json")]
    before = db.db_path(a.wiki).read_bytes()
    res = a.sync(dry_run=True)
    assert res.dry_run and res.sent_put == 1
    assert json.dumps(double.wiki(KEY).records, sort_keys=True) == snapshot
    assert [p.read_bytes() for p in (a.rdir / "sync").glob("*.json")] == state
    assert db.db_path(a.wiki).read_bytes() == before


def test_if_changed_makes_no_network_call_when_nothing_changed(a, double):
    a.add_row("M", "x")
    a.sync()
    calls = len(double.calls)
    res = a.sync(if_changed=True)
    assert res.status == "unchanged" and len(double.calls) == calls
    a.add_row("M", "y")
    assert a.sync(if_changed=True).status == "ok"
    assert len(double.calls) > calls


def test_a_wiki_in_a_linked_worktree_syncs_the_primary_checkouts_database(tmp_path, double):
    primary = tmp_path / "primary"
    (primary / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (primary / ".git" / "worktrees" / "wt" / "commondir").write_text("../..", encoding="utf-8")
    (primary / ".shapa").mkdir()
    (primary / ".shapa" / "AGENTS.md").write_text("m\n", encoding="utf-8")
    db.connect(primary / ".shapa", create=True).close()
    wt = tmp_path / "wt"
    (wt / ".shapa").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {primary}/.git/worktrees/wt\n", encoding="utf-8")
    (wt / ".shapa" / "AGENTS.md").write_text("m\n", encoding="utf-8")
    c = db.connect(primary / ".shapa")
    db.add_row(c, "M", "lives in the primary")
    c.close()
    _, token = double.add_device("wt")
    client = remotehttp.Client(URL, token, transport=double.transport)
    sync.sync_wiki(wt / ".shapa", client=client, rdir=tmp_path / "r", key=KEY)
    assert "M1" in double.live(KEY)
    assert len(list((tmp_path / "r" / "sync").glob("*.json"))) == 1
    sync.sync_wiki(primary / ".shapa", client=client, rdir=tmp_path / "r", key=KEY)
    assert len(list((tmp_path / "r" / "sync").glob("*.json"))) == 1  # one state for both


# --- break-it: a hostile or broken server -----------------------------------------------------------

def note_rec(note_id, body="x\n", kind="N", **over):
    rec = {"id": note_id, "kind": kind, "created": "", "updated": "2026-01-01T00:00:00Z",
           "rev": 0, "deleted": False, "device": "evil", "data": {"body": body}, "tags": [],
           "links": []}
    rec.update(over)
    return rec


def row_rec(rid="R9", summary="from the server", **over):
    rec = {"id": rid, "kind": rid[0], "created": "2026-01-01T00:00:00Z",
           "updated": "2026-01-01T00:00:00Z", "rev": 0, "deleted": False, "device": "x",
           "data": {"summary": summary, "body": "", "alias": None, "source": "", "session": ""},
           "tags": [], "links": []}
    rec.update(over)
    return rec


def tree_files(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file())


@pytest.mark.parametrize("bad_id", [
    "../evil.md", "/etc/evil.md", "arch/../../evil.md", "arch/sub/deep.md", "AGENTS.md",
    "placement.md", "a.md\x00", "no-extension", ".hidden.md", "arch\\x.md", "x" * 120 + ".md",
    "other/x.md", "arch/..md/x.md"])
def test_hostile_note_ids_never_write_a_file(a, double, bad_id, tmp_path):
    a.sync()  # registers the wiki
    before = tree_files(tmp_path)
    double.remote_put(KEY, note_rec(bad_id))
    res = a.sync()
    assert res.status == "ok"
    assert tree_files(tmp_path) == before
    assert (a.wiki / "AGENTS.md").read_text() == "# marker\n"


def test_a_note_cannot_be_written_through_a_symlink_or_a_symlinked_folder(a, double, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.md").write_text("precious\n", encoding="utf-8")
    (a.wiki / "arch").symlink_to(outside, target_is_directory=True)
    (a.wiki / "loose.md").symlink_to(outside / "target.md")
    a.sync()
    double.remote_put(KEY, note_rec("arch/pwn.md", "owned\n"))
    double.remote_put(KEY, note_rec("loose.md", "owned\n"))
    a.sync()
    assert tree_files(outside) == ["target.md"]
    assert (outside / "target.md").read_text() == "precious\n"


@pytest.mark.parametrize("mutate", [
    lambda r: {**r, "kind": "R"},                       # id M9 but kind R
    lambda r: {**r, "id": "M9; DROP TABLE mri"},
    lambda r: {**r, "kind": "X"},
    lambda r: {**r, "created": "yesterday"},
    lambda r: {**r, "rev": "7"},
    lambda r: {**r, "rev": True},
    lambda r: {**r, "deleted": "no"},
    lambda r: {**r, "data": "a string"},
    lambda r: {**r, "data": {"summary": 5}},
    lambda r: {**r, "data": {"summary": "x", "body": ["list"]}},
    lambda r: {**r, "tags": "notalist"},
    lambda r: {**r, "data": {"summary": "x", "body": "y" * 1_100_000}},
    lambda r: None, lambda r: "garbage", lambda r: [1, 2],
])
def test_malformed_records_are_dropped_not_applied(a, double, mutate):
    a.sync()
    good = row_rec("M9", "valid one")
    bad = mutate(dict(row_rec("M9", "bad")))
    double.mutate_response = lambda path, p: (
        {**p, "changes": [bad, good]} if path.endswith("/sync") else p)
    res = a.sync()
    assert res.status == "ok" and res.invalid >= 1
    assert [r.summary for r in rows_of(a).values()] == ["valid one"]


def test_an_item_whose_parent_kind_is_wrong_is_dropped(a, double):
    a.sync()
    bad = {"id": "J5", "kind": "J", "created": "2026-01-01T00:00:00Z",
           "updated": "2026-01-01T00:00:00Z", "rev": 0, "deleted": False, "device": "x",
           "data": {"title": "job", "body": "", "parent": "T1", "status": "open"},
           "tags": [], "links": []}
    double.remote_put(KEY, bad)
    assert a.sync().invalid == 1
    assert items_of(a) == {}


@pytest.mark.parametrize("mutate", [
    lambda p: {**p, "results": "nope"},
    lambda p: {**p, "results": []},
    lambda p: {**p, "results": [{"status": "exploded", "rev": 1}]},
    lambda p: {**p, "results": [{"status": "accepted", "rev": "x"}]},
    lambda p: {**p, "changes": {"not": "a list"}},
    lambda p: {**p, "cursor": "later"},
    lambda p: {**p, "cursor": -4},
    lambda p: {**p, "results": [{"status": "renamed", "rev": 1, "new_id": "M7"}]},   # R renamed to M
    lambda p: {**p, "results": [{"status": "renamed", "rev": 1, "new_id": "../x"}]},
    lambda p: {**p, "results": [{"status": "renamed", "rev": 1}]},
])
def test_a_malformed_sync_response_aborts_and_leaves_local_data_alone(a, double, mutate):
    rid = a.add_row("R", "mine")
    a.sync()
    a.add_row("R", "second")  # something to send, so results are checked
    before = db.db_path(a.wiki).read_bytes()
    double.mutate_response = lambda path, p: mutate(p) if path.endswith("/sync") else p
    res = a.sync()
    assert res.status == "error"
    assert db.db_path(a.wiki).read_bytes() == before
    assert rid in rows_of(a)


def test_a_rename_to_an_id_that_exists_locally_is_refused(a, b, double):
    a.add_row("R", "r one")
    a.sync()
    b.add_row("R", "b's r1")
    set_cols(b, "mri", "R1", created="2020-01-01T00:00:00Z")
    b.add_row("R", "b's r2")

    def hostile(path, p):
        if path.endswith("/sync") and p.get("results"):
            return {**p, "results": [{**r, "new_id": "R2"} if r["status"] == "renamed" else r
                                     for r in p["results"]]}
        return p

    double.mutate_response = hostile
    res = b.sync()
    rows = rows_of(b)
    assert rows["R1"].summary == "b's r1" and rows["R2"].summary == "b's r2"  # nothing relabeled
    assert res.skipped >= 1


def test_html_and_control_characters_become_inert_text(a, double):
    a.sync()
    evil = "<img src=x onerror=alert(1)>\x1b[31m\x07red\nsecond line"
    double.remote_put(KEY, row_rec("M9", evil, data={"summary": evil, "body": "b\x00ody\x1b[0m",
                                                     "alias": None, "source": "", "session": ""}))
    a.sync()
    row = rows_of(a)["M9"]
    assert "\x1b" not in row.summary and "\n" not in row.summary and "\x07" not in row.summary
    assert "\x00" not in row.body and "\x1b" not in row.body
    assert "<img" in row.summary  # inert text, never interpreted


def test_incoming_text_is_redacted_before_it_is_written(a, double):
    a.sync()
    secret = "sk-ant-api03-Q7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7x"
    double.remote_put(KEY, row_rec("M9", f"leaked {secret}", data={
        "summary": f"leaked {secret}", "body": f"body {secret}", "alias": None, "source": "",
        "session": ""}))
    double.remote_put(KEY, note_rec("leaky.md", f"note {secret}\n"))
    a.sync()
    assert secret.encode() not in db.db_path(a.wiki).read_bytes()
    assert secret not in (a.wiki / "leaky.md").read_text()


def test_an_unreachable_server_is_an_error_not_a_crash(a):
    a.add_row("M", "mine")

    def down(*args):
        raise remotehttp.RemoteError("server unreachable (URLError)")

    a.client.transport = down
    res = a.sync()
    assert res.status == "error" and "unreachable" in res.message
    assert "M1" in rows_of(a)


def test_a_redirect_is_refused(a, double):
    a.add_row("M", "x")
    double.redirect_to = "https://elsewhere.example/steal"
    res = a.sync()
    assert res.status == "error" and "redirect" in res.message


# --- break-it: local damage and concurrency ----------------------------------------------------------

@pytest.mark.parametrize("junk", [b"", b"{", b'{"version": 1, "cursor": "x"}', b"\xff\xfe\x00",
                                  b'{"version":1,"key":"my-repo","url":"https://hosted.example",'
                                  b'"cursor":3,"base":{"M1":{"rev":"1"}}}', b"[]", b"null"])
def test_a_corrupt_or_truncated_state_file_resyncs_without_duplicates(a, double, junk):
    a.add_row("M", "one")
    a.sync()
    for f in (a.rdir / "sync").glob("*.json"):
        f.write_bytes(junk)
    res = a.sync()
    assert res.status == "ok"
    assert len(double.live(KEY)) == 1 and list(rows_of(a)) == ["M1"]
    assert a.sync().sent_put == 0


def test_state_for_another_key_or_server_is_not_trusted(a, double):
    a.add_row("M", "one")
    a.sync()
    res = a.sync(key="other-key")
    assert res.status == "ok" and res.sent_put == 1  # fresh state: nothing assumed about the new key


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root ignores file modes")
def test_a_read_only_database_is_an_error_not_a_crash(a, double):
    a.add_row("M", "mine")
    a.sync()
    a.add_row("M", "second")
    db.db_path(a.wiki).chmod(0o444)
    a.wiki.chmod(0o555)
    try:
        res = a.sync()
        assert res.status == "error"
    finally:
        a.wiki.chmod(0o755)
        db.db_path(a.wiki).chmod(0o644)


def test_a_busy_wiki_is_reported_not_interleaved(a):
    a.add_row("M", "x")
    path = syncstate.state_path(a.rdir, db.db_path(a.wiki))
    with syncstate.wiki_lock(path, 1) as got:
        assert got
        res = a.sync(wait=0)
    assert res.status == "busy"


def test_two_syncs_racing_on_one_wiki_do_not_duplicate_anything(a, double):
    import threading

    for i in range(5):
        a.add_row("M", f"racer {i}")
    results = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(a.sync(wait=20))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert [r.status for r in results] == ["ok", "ok"]
    assert sorted(r.sent_put for r in results) == [0, 5]  # the second saw the first's work
    assert len(double.live(KEY)) == 5 and len(rows_of(a)) == 5


# --- nothing bounces, nothing is lost -------------------------------------------------------------------

def test_pulled_records_of_every_kind_do_not_bounce_back(a, b, double):
    c = a.conn()
    f = db.add_item(c, "F", "a feature", body="why", alias="the-feature")
    j = db.add_item(c, "J", "a job", parent=f, verify="true")
    t = db.add_item(c, "T", "a task", parent=j)
    db.close(c, j, "commit", git_ref="abc123")
    r, _ = db.add_row(c, "R", "a rule", "details", tags=["t1", "t2"], alias="rule-alias",
                      about=[f], source="agent")
    d = db.add_doc(c, "A document", "long text", alias="doc-alias", tags=["docs"], mime="text/plain")
    db.link(c, d, r, "related")
    c.close()
    a.sync()
    b.sync()
    assert b.sync().sent_put == 0 and a.sync().sent_put == 0
    ci = items_of(b)
    assert ci[j].status == "closed" and ci[j].closed_reason == "commit" and ci[t].parent == j
    assert ci[f].alias == "the-feature"
    c = b.conn()
    doc = db.get_doc(c, "doc-alias")
    assert doc.mime == "text/plain" and doc.tags == ["docs"]
    assert (d, r, "related") in db.links_of(c, d)
    assert db.get_row(c, "rule-alias").tags == ["t1", "t2"]
    c.close()


def test_a_change_skipped_for_a_label_clash_arrives_on_the_next_sync(a, b, double):
    a.sync()
    b.add_row("M", "b's first")
    b.sync()

    def local_create(key, req):
        a.add_row("M", "a's first, written during the call")
        set_cols(a, "mri", "M1", created="2021-01-01T00:00:00Z")

    double.on_sync = local_create
    a.sync()
    double.on_sync = None
    assert rows_of(a)["M1"].summary == "a's first, written during the call"
    a.sync()
    summaries = {r.summary for r in rows_of(a).values()}
    assert summaries == {"a's first, written during the call", "b's first"}
    b.sync()
    assert {r.summary for r in rows_of(b).values()} == summaries


def test_crlf_note_files_are_preserved_exactly(a, b, double):
    (a.wiki / "windows.md").write_bytes(b"line one\r\nline two\r\n")
    a.sync()
    b.sync()
    assert (b.wiki / "windows.md").read_bytes() == b"line one\r\nline two\r\n"
    assert b.sync().sent_put == 0


def test_a_server_that_never_stops_paging_is_cut_off(a, double):
    a.add_row("M", "x")
    double.mutate_response = lambda path, p: ({**p, "more": True} if path.endswith("/sync") else p)
    res = a.sync()
    assert res.status == "ok"
    assert len(double.sync_requests) <= sync.MAX_PAGES + 3
