"""Tests for shapa.mcp: the stdio MCP server (shapa-backend-spec.md §7.1).

All dispatched in-process (no subprocess, no network) against a stub wiki -
the acceptance criteria for Slice 6's MCP surface is exactly this: a
tools/list + tools/call round-trip with no network needed. One test drives
the full stdio framing (serve_stdio over in-memory text streams) to prove
the line-delimited JSON-RPC transport itself works end to end.
"""

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from shapa import mcp
from shapa.frontmatter import parse as parse_frontmatter


def _note(d: Path, nid: str, body: str, **meta) -> None:
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"id: {nid}", f"type: {meta.pop('type', 'memory')}",
             'created: "2026-09-30T00:00:00Z"', "consequence: 5", "locus: output", "uses: 0"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    (d / f"{nid}.md").write_text("\n".join(lines) + f"\n{body}\n", encoding="utf-8")


class _StubWiki(unittest.TestCase):
    """A repo-local wiki with no global wiki reachable (an isolated
    $SHAPA_MEMORY pointed elsewhere) - keeps tools/search/get deterministic
    regardless of the machine's own ~/.shapa/memory."""

    def setUp(self):
        import os
        from shapa import config

        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.wiki = self.repo / ".shapa"
        self.wiki.mkdir(parents=True)
        (self.wiki / "AGENTS.md").write_text("marker\n", encoding="utf-8")
        (self.repo / ".git").mkdir()  # config.wiki_roots' repo-name detection
        _note(self.wiki, "git-workflow", "Always branch off main before committing.",
              locus="meta")
        _note(self.wiki, "unrelated-fact", "The office coffee machine is broken.")

        # An unreachable-by-default global root: a fresh empty dir, never
        # populated with notes - wiki_roots() always appends it, but it
        # contributes no ids unless a test adds one. Routed via the `shapa
        # init` pointer (not $SHAPA_MEMORY, which wiki_roots() treats as a
        # single-root override that bypasses repo discovery entirely - not
        # what these tests want), and via a scratch CONFIG_FILE so this
        # never touches the real machine's ~/.shapa/config.json.
        self.empty_global = self.tmp / "empty-global"
        self._config = config
        self._old_config_file = config.CONFIG_FILE
        self._old_env = os.environ.pop("SHAPA_MEMORY", None)
        config.CONFIG_FILE = self.tmp / "pointer-home" / "config.json"
        config.set_memory_dir(self.empty_global)

    def tearDown(self):
        import os
        self._config.CONFIG_FILE = self._old_config_file
        if self._old_env is not None:
            os.environ["SHAPA_MEMORY"] = self._old_env
        shutil.rmtree(self.tmp)


class TestTools(_StubWiki):
    def test_search_finds_the_meta_anchor_unconditionally(self):
        out = mcp.tool_search({"query": "totally unrelated gibberish"}, str(self.repo))
        ids = [r["id"] for r in out["results"]]
        self.assertIn("git-workflow", ids)  # locus:meta anchor, always surfaced

    def test_search_ranks_relevant_note_above_filler(self):
        out = mcp.tool_search({"query": "branching and commits"}, str(self.repo))
        ids = [r["id"] for r in out["results"]]
        self.assertIn("git-workflow", ids)
        # The off-topic filler note either ranks below it, or clears the
        # confidence floor so poorly it's dropped from the fill entirely
        # (fetch.py's percentile-relative floor, §4.1/§10 decision 2) -
        # either way it must never outrank the genuinely relevant note.
        if "unrelated-fact" in ids:
            self.assertLess(ids.index("git-workflow"), ids.index("unrelated-fact"))

    def test_get_returns_full_body_for_a_known_id(self):
        out = mcp.tool_get({"id": "git-workflow"}, str(self.repo))
        self.assertNotIn("error", out)
        self.assertEqual(out["id"], "git-workflow")
        self.assertIn("branch off main", out["body"])

    def test_get_errors_clearly_for_an_unknown_id(self):
        out = mcp.tool_get({"id": "does-not-exist"}, str(self.repo))
        self.assertIn("error", out)

    def test_get_reports_ambiguous_when_id_collides_across_roots(self):
        # Same id, same content shape, in both the repo wiki and the "global"
        # root - a real F09 collision.
        _note(self.empty_global, "git-workflow", "A different, colliding note.")
        out = mcp.tool_get({"id": "git-workflow"}, str(self.repo))
        self.assertIn("error", out)
        self.assertIn("matches", out)
        self.assertEqual(len(out["matches"]), 2)

    def test_placement_returns_the_bundled_decision_rule(self):
        out = mcp.tool_placement({}, str(self.repo))
        self.assertNotIn("error", out)
        self.assertIn("global", out["text"])
        self.assertIn("repo", out["text"])


class TestSaveScopeGating(_StubWiki):
    def test_save_requires_a_valid_scope(self):
        out = mcp.tool_save(
            {"scope": "external", "summary": "x", "body": "y"}, str(self.repo)
        )
        self.assertIn("error", out)
        out2 = mcp.tool_save({"summary": "x", "body": "y"}, str(self.repo))
        self.assertIn("error", out2)

    def test_save_requires_summary_and_body(self):
        self.assertIn("error", mcp.tool_save({"scope": "repo", "body": "y"}, str(self.repo)))
        self.assertIn("error", mcp.tool_save({"scope": "repo", "summary": "x"}, str(self.repo)))

    def test_save_rejects_an_oversized_or_multiline_summary(self):
        out = mcp.tool_save(
            {"scope": "repo", "summary": "line one\nline two", "body": "y"}, str(self.repo)
        )
        self.assertIn("error", out)
        out2 = mcp.tool_save(
            {"scope": "repo", "summary": "x" * 200, "body": "y"}, str(self.repo)
        )
        self.assertIn("error", out2)

    def test_save_repo_scope_writes_into_this_repos_own_wiki(self):
        out = mcp.tool_save(
            {"scope": "repo", "summary": "A test note.", "body": "Some body text here."},
            str(self.repo),
        )
        self.assertNotIn("error", out)
        path = Path(out["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.resolve(), self.wiki.resolve())
        meta = parse_frontmatter(path).meta
        self.assertEqual(meta["scope"], "repo")

    def test_save_global_scope_writes_into_the_global_wiki_not_the_repo(self):
        out = mcp.tool_save(
            {"scope": "global", "summary": "A global fact.", "body": "Some body text here."},
            str(self.repo),
        )
        self.assertNotIn("error", out)
        path = Path(out["path"])
        self.assertEqual(path.parent.resolve(), self.empty_global.resolve())

    def test_save_repo_scope_errors_when_no_repo_wiki_exists(self):
        no_wiki_repo = self.tmp / "no-wiki-repo"
        (no_wiki_repo / ".git").mkdir(parents=True)
        out = mcp.tool_save(
            {"scope": "repo", "summary": "x", "body": "y"}, str(no_wiki_repo)
        )
        self.assertIn("error", out)
        self.assertIn("shapa init", out["error"])

    def test_save_never_silently_overwrites_an_existing_id(self):
        first = mcp.tool_save(
            {"scope": "repo", "summary": "First.", "body": "Body one.", "id": "dup-id"},
            str(self.repo),
        )
        self.assertNotIn("error", first)
        second = mcp.tool_save(
            {"scope": "repo", "summary": "Second.", "body": "Body two.", "id": "dup-id"},
            str(self.repo),
        )
        self.assertIn("error", second)

    def test_save_refuses_an_id_that_would_collide_across_roots(self):
        out = mcp.tool_save(
            {"scope": "repo", "summary": "Colliding.", "body": "x",
             "id": "unrelated-fact"},  # already exists in the repo wiki itself
            str(self.repo),
        )
        self.assertIn("error", out)

    def test_saved_note_is_findable_by_get_immediately_after(self):
        saved = mcp.tool_save(
            {"scope": "repo", "summary": "Findable note.", "body": "Body content.",
             "id": "findable-note"},
            str(self.repo),
        )
        self.assertNotIn("error", saved)
        fetched = mcp.tool_get({"id": "findable-note"}, str(self.repo))
        self.assertNotIn("error", fetched)
        self.assertIn("Body content.", fetched["body"])

    def test_save_rejects_a_path_traversal_id_before_writing_anywhere(self):
        outside = self.tmp / "tmp" / "pwned-shapa-test.md"
        out = mcp.tool_save(
            {"scope": "repo", "summary": "x", "body": "y",
             "id": "../../../../tmp/pwned-shapa-test"},
            str(self.repo),
        )
        self.assertIn("error", out)
        self.assertFalse(outside.exists())
        # A bare '..'/'.' id, or one carrying a NUL byte, must be rejected
        # too - not just multi-segment traversal.
        for bad_id in ("..", ".", "a/b", "a\\b", "a\x00b"):
            out = mcp.tool_save(
                {"scope": "repo", "summary": "x", "body": "y", "id": bad_id}, str(self.repo)
            )
            self.assertIn("error", out, f"id {bad_id!r} should have been rejected")

    def test_save_defaults_type_to_memory_and_slugifies_a_missing_id(self):
        out = mcp.tool_save(
            {"scope": "repo", "summary": "Some Summary, With Punctuation!", "body": "x"},
            str(self.repo),
        )
        self.assertNotIn("error", out)
        self.assertTrue(Path(out["path"]).stem.startswith("some-summary"))


class TestDispatch(unittest.TestCase):
    """The JSON-RPC 2.0 layer, independent of any particular tool."""

    def test_initialize(self):
        resp = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, None)
        self.assertEqual(resp["result"]["serverInfo"]["name"], "shapa")
        self.assertEqual(resp["id"], 1)

    def test_notification_gets_no_reply(self):
        resp = mcp.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)
        self.assertIsNone(resp)

    def test_tools_list_matches_the_four_documented_tools(self):
        resp = mcp.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None)
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {"search", "get", "save", "placement"})
        for tool in resp["result"]["tools"]:
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)

    def test_unknown_method_errors(self):
        resp = mcp.dispatch({"jsonrpc": "2.0", "id": 3, "method": "nonsense"}, None)
        self.assertIn("error", resp)

    def test_unknown_tool_errors(self):
        resp = mcp.dispatch(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}},
            None,
        )
        self.assertIn("error", resp)

    def test_a_raising_tool_becomes_an_error_content_reply_not_a_crash(self):
        boom = lambda args, cwd: (_ for _ in ()).throw(RuntimeError("boom"))
        old = mcp._HANDLERS.get("placement")
        mcp._HANDLERS["placement"] = boom
        try:
            resp = mcp.dispatch(
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "placement", "arguments": {}}},
                None,
            )
        finally:
            mcp._HANDLERS["placement"] = old
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("boom", resp["result"]["content"][0]["text"])

    def test_tools_call_round_trip_for_placement(self):
        resp = mcp.dispatch(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "placement", "arguments": {}}},
            None,
        )
        self.assertFalse(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("text", payload)


class TestStdioTransport(_StubWiki):
    """Drives serve_stdio() over in-memory text streams - the real wire
    framing (line-delimited JSON-RPC), no subprocess and no network."""

    def test_initialize_then_tools_call_over_the_stdio_loop(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "search", "arguments": {"query": "branching"}}},
        ]
        stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
        stdout = io.StringIO()
        mcp.serve_stdio(stdin=stdin, stdout=stdout, cwd=str(self.repo))

        lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
        # The notification got no reply, so exactly 3 responses for 3
        # id-bearing requests, in order, correctly correlated by id.
        self.assertEqual([r["id"] for r in lines], [1, 2, 3])
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "shapa")
        tool_names = {t["name"] for t in lines[1]["result"]["tools"]}
        self.assertEqual(tool_names, {"search", "get", "save", "placement"})
        search_payload = json.loads(lines[2]["result"]["content"][0]["text"])
        self.assertIn("git-workflow", [r["id"] for r in search_payload["results"]])

    def test_malformed_line_is_skipped_not_fatal(self):
        stdin = io.StringIO(
            "not json at all\n"
            + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        )
        stdout = io.StringIO()
        mcp.serve_stdio(stdin=stdin, stdout=stdout, cwd=str(self.repo))
        lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["id"], 1)


if __name__ == "__main__":
    unittest.main()
