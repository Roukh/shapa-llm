"""Documents (kind D), ideas, and delete tombstones: database schema 2."""

import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from unittest import mock

import pytest

from shapa import bootstrap, cli, db, docs, fetch, get, mcp, save

SECRET = "sk-ant-api03-Q7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7x"


def run_cli(argv, cwd, stdin=None):
    out = io.StringIO()
    with redirect_stdout(out):
        if stdin is not None:
            with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                code = docs.run_doc(argv + ["--cwd", str(cwd)])
        else:
            code = docs.run_doc(argv + ["--cwd", str(cwd)])
    return code, out.getvalue()


def make_v1_db(path):
    """A schema-1 database exactly as an older shapa wrote it."""
    conn = sqlite3.connect(str(path))
    for stmt in db._SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema', '1')")
    for kind in "FJTMRI":
        conn.execute("INSERT INTO counters(kind, next) VALUES (?, 1)", (kind,))
    conn.execute("INSERT INTO mri(id, kind, summary, body, hash, created, updated) "
                 "VALUES ('M1','M','old memory','b','h','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
    conn.execute("UPDATE counters SET next = 2 WHERE kind = 'M'")
    conn.commit()
    conn.close()


def table_names(path):
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# --- schema 1 -> 2 ------------------------------------------------------------------------

def test_connect_migrates_schema_1_additively(tmp_path):
    wiki = tmp_path / "w"
    wiki.mkdir()
    make_v1_db(wiki / "shapa.db")
    assert "docs" not in table_names(wiki / "shapa.db")
    conn = db.connect(wiki)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "2"
        assert conn.execute("SELECT next FROM counters WHERE kind='D'").fetchone()[0] == 1
        assert [r.id for r in db.rows(conn)] == ["M1"]  # old data intact
        assert db.add_doc(conn, "Spec", "text") == "D1"
    finally:
        conn.close()
    assert {"docs", "tombstones"} <= table_names(wiki / "shapa.db")


def test_schema_1_file_reads_read_only_without_migrating(tmp_path):
    wiki = tmp_path / "w"
    wiki.mkdir()
    make_v1_db(wiki / "shapa.db")
    before = (wiki / "shapa.db").read_bytes()
    assert [r.id for r in db.read_rows(wiki)] == ["M1"]
    assert (wiki / "shapa.db").read_bytes() == before


def test_a_read_only_database_still_opens(tmp_path):
    wiki = tmp_path / "w"
    wiki.mkdir()
    make_v1_db(wiki / "shapa.db")
    (wiki / "shapa.db").chmod(0o444)
    wiki.chmod(0o555)
    try:
        conn = db.connect(wiki)  # cannot migrate; must not raise
        assert conn is not None
        assert [r.id for r in db.rows(conn)] == ["M1"]
        conn.close()
    finally:
        wiki.chmod(0o755)


def test_schema_2_keeps_every_schema_1_table_and_column(tmp_path):
    """An older shapa reading a schema-2 file only touches the v1 tables."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    make_v1_db(a / "shapa.db")
    db.connect(b, create=True).close()
    for table in ("items", "mri", "tags", "links", "counters", "meta"):
        cols = lambda p: [r[1] for r in sqlite3.connect(str(p)).execute(f"PRAGMA table_info({table})")]  # noqa: E731
        assert cols(a / "shapa.db") == cols(b / "shapa.db")


def test_a_fresh_database_is_schema_2(tmp_path):
    conn = db.connect(tmp_path, create=True)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "2"
        assert conn.execute("SELECT next FROM counters WHERE kind='D'").fetchone()[0] == 1
    finally:
        conn.close()


# --- documents ----------------------------------------------------------------------------

def test_add_get_update_documents(wikis):
    conn = wikis.conn()
    try:
        d1 = db.add_doc(conn, "Deploy runbook", "step one", alias="runbook", source="test")
        d2 = db.add_doc(conn, "Second", "x", mime="text/plain")
        assert (d1, d2) == ("D1", "D2")
        doc = db.get_doc(conn, "runbook")
        assert doc.id == "D1" and doc.mime == "text/markdown" and doc.source == "test"
        assert db.get_doc(conn, "D2").mime == "text/plain"
        old_hash = doc.hash
        edited = db.update_doc(conn, "D1", body="step one, then two")
        assert edited.body == "step one, then two" and edited.hash != old_hash
        assert edited.created == doc.created
        with pytest.raises(db.LedgerError):
            db.add_doc(conn, "Dup alias", "x", alias="runbook")
        with pytest.raises(db.LedgerError):
            db.add_doc(conn, "  ", "x")
        with pytest.raises(db.LedgerError):
            db.add_doc(conn, "Bad mime", "x", mime="not a mime")
        assert db.counts(conn)["D"] == 2
    finally:
        conn.close()


def test_documents_take_tags_and_links_and_are_redacted(wikis):
    conn = wikis.conn()
    try:
        d = db.add_doc(conn, f"Notes {SECRET}", f"body {SECRET}", tags=["ops"])
        db.tag(conn, d, ["runbook"])
        m, _ = db.add_row(conn, "M", "a memory")
        db.link(conn, d, m, "related")
        assert db.get_doc(conn, d).tags == ["ops", "runbook"]
        assert (d, m, "related") in db.links_of(conn, d)
        db.update_doc(conn, d, body=f"again {SECRET}")
    finally:
        conn.close()
    assert SECRET.encode() not in db.db_path(wikis.wiki).read_bytes()


# --- tombstones ---------------------------------------------------------------------------

def test_delete_writes_a_tombstone_for_a_row_an_item_and_a_document(wikis):
    conn = wikis.conn()
    try:
        m, _ = db.add_row(conn, "M", "gone soon")
        j = db.add_item(conn, "J", "a job")
        d = db.add_doc(conn, "A doc", "b")
        created = {m: db.get_row(conn, m).created, j: db.get_item(conn, j).created,
                   d: db.get_doc(conn, d).created}
        assert db.delete(conn, [m, j, d]) == 3
        stones = {t["id"]: t for t in db.tombstones(conn)}
        assert set(stones) == {m, j, d}
        assert stones[m]["kind"] == "M" and stones[j]["kind"] == "J" and stones[d]["kind"] == "D"
        for i, t in stones.items():
            assert t["created"] == created[i] and t["deleted_at"]
    finally:
        conn.close()


def test_delete_can_skip_the_tombstone_for_a_remote_originated_delete(wikis):
    conn = wikis.conn()
    try:
        m, _ = db.add_row(conn, "M", "remote said so")
        db.delete(conn, [m], tombstone=False)
        assert db.tombstones(conn) == []
    finally:
        conn.close()


def test_the_sweep_deletes_through_tombstones_and_never_removes_documents(wikis):
    conn = wikis.conn()
    try:
        j = db.add_item(conn, "J", "done work")
        db.close(conn, j, "commit")
        a, _ = db.add_row(conn, "R", "always rebase before merging the branch")
        b, _ = db.add_row(conn, "R", "always rebase before merging the branch", "")
        d1 = db.add_doc(conn, "Same title", "same body", tags=["keep"])
        d2 = db.add_doc(conn, "Same title", "same body")
        report = db.sweep(conn, keep_closed_since="2999-01-01T00:00:00Z")
        assert j in report.deleted_items
        assert {t["id"] for t in db.tombstones(conn)} >= {j}
        assert db.get_doc(conn, d1) is not None and db.get_doc(conn, d2) is not None
        assert db.get_doc(conn, d1).tags == ["keep"]  # tags survive the dangling-tag cleanup
    finally:
        conn.close()


# --- the surface --------------------------------------------------------------------------

def test_doc_cli_add_list_edit_rm(wikis):
    code, out = run_cli(["add", "--scope", "repo", "Deploy runbook", "--body", "step one",
                         "--alias", "runbook", "--tag", "ops"], wikis.repo)
    assert code == 0 and out.strip() == "D1"
    code, out = run_cli(["list"], wikis.repo)
    assert "D1 [runbook] Deploy runbook" in out
    code, out = run_cli(["edit", "runbook", "--title", "Runbook v2", "--body", "step two"], wikis.repo)
    assert code == 0 and "Runbook v2" in out
    result = mcp.tool_get({"id": "runbook"}, str(wikis.repo))
    assert result["body"] == "step two" and result["type"] == "document"
    code, out = run_cli(["rm", "D1"], wikis.repo)
    assert "removed 1" in out
    assert "no note" in mcp.tool_get({"id": "D1"}, str(wikis.repo))["error"]


def test_doc_add_needs_a_scope_and_reads_body_from_file_or_stdin(wikis, tmp_path):
    with pytest.raises(SystemExit):
        docs.run_doc(["add", "No scope", "--body", "x", "--cwd", str(wikis.repo)])
    src = tmp_path / "in.md"
    src.write_text("from a file", encoding="utf-8")
    run_cli(["add", "--scope", "global", "File doc", "--file", str(src)], wikis.repo)
    run_cli(["add", "--scope", "global", "Piped doc"], wikis.repo, stdin="from stdin\n")
    conn = wikis.conn(wikis.global_wiki)
    try:
        bodies = {d.title: d.body for d in db.documents(conn)}
    finally:
        conn.close()
    assert bodies == {"File doc": "from a file", "Piped doc": "from stdin"}


def test_get_renders_a_document_by_id_and_alias(wikis):
    conn = wikis.conn()
    try:
        db.add_doc(conn, "Architecture", "the long text", alias="arch-doc", tags=["design"])
    finally:
        conn.close()
    for key in ("D1", "arch-doc"):
        result = mcp.tool_get({"id": key}, str(wikis.repo))
        assert result["id"] == "D1" and result["type"] == "document"
        text = get.render(result)
        assert "Architecture" in text and "the long text" in text and "design" in text


def test_save_accepts_document_and_idea_on_cli_and_mcp(wikis):
    r = mcp.tool_save({"scope": "repo", "type": "document", "summary": "Plan", "body": "the plan",
                       "tags": ["x"]}, str(wikis.repo))
    assert r["id"] == "D1"
    r = mcp.tool_save({"scope": "repo", "type": "idea", "summary": "Try a cache", "body": "why"},
                      str(wikis.repo))
    assert r["id"] == "M1"
    r2 = save.save_note("repo", "Another idea", "details", note_type="idea", start=wikis.repo)
    assert r2["id"] == "M2"
    r3 = save.save_note("repo", "A doc", "text", note_type="document", start=wikis.repo)
    assert r3["id"] == "D2"
    conn = wikis.conn()
    try:
        assert db.get_row(conn, "M1").kind == "M" and "idea" in db.get_row(conn, "M1").tags
        assert "idea" in db.get_row(conn, "M2").tags
    finally:
        conn.close()
    assert "error" in mcp.tool_save({"scope": "repo", "type": "bogus", "summary": "s", "body": "b"},
                                    str(wikis.repo))


# --- recall -------------------------------------------------------------------------------

def test_documents_reach_fetch_search_and_bootstrap(wikis):
    conn = wikis.conn()
    try:
        db.add_doc(conn, "Quokka migration checklist", "move the quokka database to the new cluster")
    finally:
        conn.close()
    sel = fetch.select_multi("quokka migration", start=wikis.repo)
    assert "Quokka migration checklist" in fetch.render(sel)
    found = mcp.tool_search({"query": "quokka migration"}, str(wikis.repo))
    assert any(r["id"] == "D1" for r in found["results"])
    ctx = bootstrap.build_context(start=wikis.repo)
    assert "D1 (document): Quokka migration checklist" in ctx
    assert "the quokka database" not in ctx  # summary line only, never the body


def test_session_start_stays_lean_with_many_documents(wikis):
    conn = wikis.conn()
    try:
        for i in range(30):
            db.add_doc(conn, f"Reference document number {i}", "body " * 50)
    finally:
        conn.close()
    ctx = bootstrap.build_context(start=wikis.repo)
    assert ctx.count("(document)") <= bootstrap.MEMORY_CAP_PER_ROOT
