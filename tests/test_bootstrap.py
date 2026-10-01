"""Tests for shapa.bootstrap: the SessionStart path (Tier 1, metadata-only).

A session start is not a prompt - there is no query to rank by relevance,
only value (score_meta). Every root's own locus:meta notes surface
unconditionally (capped per root, exactly like fetch.py's anchors), the
rest is filled by value within a character budget, and note bodies are
never read for this - only id/type/summary/consequence/locus. See
shapa-backend-spec.md §4.2 Tier 1 and Slice 3's acceptance criteria (§8).
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shapa import bootstrap, config
from shapa.config import WikiRoot

FIX = Path(__file__).parent / "fixtures" / "fetch"


def _note(d: Path, nid: str, summary: str = "", locus: str = "output",
          consequence: int = 5, body: str = "body text") -> None:
    d.mkdir(parents=True, exist_ok=True)
    summary_line = f"summary: {summary}\n" if summary else ""
    (d / f"{nid}.md").write_text(
        "---\n"
        f"id: {nid}\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
        f"consequence: {consequence}\nlocus: {locus}\nuses: 0\n{summary_line}---\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _make_repo_wiki(repo: Path) -> Path:
    wiki = repo / ".shapa"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / config.WIKI_MARKER).write_text(
        "---\nid: AGENTS\ntype: reference\ncreated: \"2026-01-01T00:00:00Z\"\n"
        "consequence: 8\nlocus: output\nuses: 0\n---\n# rules\n",
        encoding="utf-8",
    )
    return wiki


class BootstrapTestCase(unittest.TestCase):
    """Shared harness: isolated $SHAPA_MEMORY/pointer, exactly like
    tests/test_config_multiroot.py and tests/test_fetch_multiroot.py."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.cfg),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAnchorsUnconditionalPerRoot(BootstrapTestCase):
    def test_each_root_own_meta_note_surfaces(self):
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-meta", "standing deploy-safety rule", locus="meta", consequence=9)
        _note(global_wiki, "g-filler", "lunch note")

        repo = _make_git_repo(self.tmp / "repoA")
        repo_wiki = _make_repo_wiki(repo)
        _note(repo_wiki, "r-meta", "standing test-discipline rule", locus="meta", consequence=9)
        _note(repo_wiki, "r-filler", "weather note")

        selected = bootstrap.select(start=repo)
        ids = [n.id for n, _ in selected]
        self.assertIn("g-meta", ids)
        self.assertIn("r-meta", ids)

    def test_weak_root_anchor_survives_a_strong_root(self):
        # A global anchor cap (sorted by value across roots) would starve a
        # weak root's own anchor behind another root's many strong ones -
        # the per-root cap must reserve each root's own slot regardless.
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-weak-meta", "a weak global rule", locus="meta", consequence=2)

        repo = _make_git_repo(self.tmp / "repoB")
        repo_wiki = _make_repo_wiki(repo)
        for i in range(5):
            _note(repo_wiki, f"r-strong-meta-{i}", f"strong rule {i}", locus="meta", consequence=10)

        # A budget too small for any "rest" item isolates the anchor phase
        # itself (anchors ignore the budget entirely - see the next test) -
        # so what comes back here is exactly each root's own anchor set.
        selected = bootstrap.select(start=repo, budget=1)
        ids = {n.id for n, _ in selected}
        self.assertIn("g-weak-meta", ids)
        # Per-root cap of 2: exactly the top-2 repo metas as anchors, never
        # all 5 crowding out the weak root's own guaranteed slot.
        repo_meta_ids = {i for i in ids if i.startswith("r-strong-meta-")}
        self.assertEqual(len(repo_meta_ids), 2)

    def test_anchors_never_budget_gated(self):
        # Anchors print unconditionally, even when the budget is far too
        # small to hold them - unlike the value-ranked "rest" fill.
        global_wiki = self.tmp / "global"
        config.set_memory_dir(global_wiki)
        _note(global_wiki, "g-meta", "x" * 500, locus="meta", consequence=9)

        selected = bootstrap.select(start=global_wiki, budget=1)
        ids = [n.id for n, _ in selected]
        self.assertIn("g-meta", ids)


class TestValueRankedRest(BootstrapTestCase):
    def test_higher_score_ranks_first(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "low", "low value note", consequence=1)
        _note(wiki, "high", "high value note", consequence=10)

        selected = bootstrap.select(start=wiki)
        ids = [n.id for n, _ in selected]
        self.assertEqual(ids.index("high"), 0)
        self.assertLess(ids.index("high"), ids.index("low"))

    def test_budget_truncates_the_rest(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        for i in range(30):
            _note(wiki, f"n{i}", "y" * 100, consequence=5)

        selected = bootstrap.select(start=wiki, budget=200)
        self.assertLess(len(selected), 30)

    def test_first_item_always_fits_even_if_oversized(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "big", "z" * 5000, consequence=9)

        selected = bootstrap.select(start=wiki, budget=10)
        self.assertEqual([n.id for n, _ in selected], ["big"])

    def test_selected_lines_stay_within_budget_with_long_realistic_ids(self):
        # Regression: budgeting must count the REAL rendered line
        # (`- [{kind}] {id} ({type}): {summary}`), not a fixed per-line
        # overhead constant plus the summary alone. Short uniform ids like
        # "n0".."n29" (test_budget_truncates_the_rest, above) make that
        # undercount negligible; long, varied ids - as real wikis actually
        # have - do not, and a fixed constant overshoots the stated budget.
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        for i in range(40):
            _note(
                wiki,
                f"long-descriptive-memory-note-identifier-{i:02d}-detail",
                "a fairly typical one-line summary sentence for this note",
                consequence=5,
            )

        budget = 2000
        selected = bootstrap.select(start=wiki, budget=budget)
        rendered_total = sum(
            len(bootstrap._render_line(node, root)) for node, root in selected
        )
        self.assertLessEqual(rendered_total, budget)


class TestSummaryOnlyNeverBody(BootstrapTestCase):
    def test_summary_used_when_present(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "s1", summary="one-line summary text", body="a very different long body")

        block = bootstrap.build_context(start=wiki)
        self.assertIn("one-line summary text", block)
        self.assertNotIn("a very different long body", block)

    def test_falls_back_to_id_when_summary_missing(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "no-summary-note", body="body content")

        block = bootstrap.build_context(start=wiki)
        self.assertIn("no summary note", block)  # id with dashes -> spaces
        self.assertNotIn("body content", block)

    def test_summary_capped_to_max_chars(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "long-summary", summary="s" * 400)

        block = bootstrap.build_context(start=wiki)
        self.assertNotIn("s" * 400, block)
        self.assertIn("s" * bootstrap.MAX_SUMMARY_CHARS, block)


class TestEmptyAndUnreachable(BootstrapTestCase):
    def test_no_notes_anywhere_returns_empty_string(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)  # directory does not even exist yet
        self.assertEqual(bootstrap.build_context(start=wiki), "")
        self.assertEqual(bootstrap.select(start=wiki), [])

    def test_never_raises_on_a_broken_root(self):
        # A root whose path exists but is unreadable-as-notes must not
        # blow up bootstrap - it just contributes nothing.
        roots = [WikiRoot(path=Path("/this/does/not/exist/at/all"), kind="global")]
        self.assertEqual(bootstrap.build_context(roots=roots), "")
        self.assertEqual(bootstrap.select(roots=roots), [])


class TestContextBlockShape(BootstrapTestCase):
    def test_block_wrapped_and_lists_wiki_roots(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "n1", "a note")

        block = bootstrap.build_context(start=wiki)
        self.assertTrue(block.startswith("<shapa-memory>"))
        self.assertTrue(block.rstrip().endswith("</shapa-memory>"))
        self.assertIn(str(wiki.resolve()), block)
        self.assertIn("[global]", block)


class TestAgentsMdFormat(BootstrapTestCase):
    """``--format agents-md`` (shapa-backend-spec.md §7.1): a plain markdown
    block, not the SessionStart JSON envelope - for harnesses like Codex
    that natively ingest an AGENTS.md but have no per-turn hook."""

    def test_render_agents_md_wraps_the_same_content_no_json(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "n1", "a standing note", locus="meta", consequence=9)

        text = bootstrap.render_agents_md(start=wiki)
        self.assertNotIn('"hookSpecificOutput"', text)
        self.assertTrue(text.startswith("<!-- Generated by `shapa bootstrap"))
        self.assertIn("<shapa-memory>", text)
        self.assertIn("n1", text)
        self.assertIn("a standing note", text)

    def test_render_agents_md_empty_when_nothing_to_show(self):
        wiki = self.tmp / "wiki"  # never created - no notes anywhere
        config.set_memory_dir(wiki)
        self.assertEqual(bootstrap.render_agents_md(start=wiki), "")

    def test_cli_format_agents_md_prints_to_stdout_ignoring_stdin(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "n1", "cli agents-md note")

        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO("garbage, never read in this mode")
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as exc:
                bootstrap.main(["--format", "agents-md", "--cwd", str(wiki)])
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("cli agents-md note", out)
        self.assertNotIn('"hookSpecificOutput"', out)

    def test_cli_format_agents_md_with_out_writes_a_file(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "n1", "file-written note")
        out_path = self.tmp / "shapa-context.md"

        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit):
                bootstrap.main([
                    "--format", "agents-md", "--cwd", str(wiki), "--out", str(out_path),
                ])
        finally:
            sys.stdout = old_stdout
        self.assertTrue(out_path.is_file())
        self.assertIn("file-written note", out_path.read_text(encoding="utf-8"))


class TestHookMain(BootstrapTestCase):
    """The SessionStart hook contract: JSON payload in on stdin, a
    hookSpecificOutput/additionalContext JSON envelope out, exit 0 always."""

    def _run_main(self, stdin_text: str, argv=None):
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as exc:
                bootstrap.main(argv or [])
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        self.assertEqual(exc.exception.code, 0)
        return out

    def test_hook_emits_valid_json_with_additional_context(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "n1", "a standing note", locus="meta", consequence=9)

        out = self._run_main(json.dumps({"cwd": str(wiki)}))
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("n1", ctx)
        self.assertIn("a standing note", ctx)

    def test_hook_never_blocks_on_garbage_stdin(self):
        # Garbage stdin falls back to the process cwd: pin it (and the global
        # pointer) to empty dirs, so a wiki above the real cwd - this repo's
        # own .shapa/ - can't leak into the context.
        config.set_memory_dir(self.tmp / "empty-global")
        with mock.patch.object(config.Path, "cwd", staticmethod(lambda: self.tmp)):
            out = self._run_main("not valid json at all { { {")
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "")

    def test_hook_respects_explicit_cwd_flag_over_stdin(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "n1", "explicit cwd note")

        out = self._run_main("", argv=["--cwd", str(wiki)])
        payload = json.loads(out)
        self.assertIn("explicit cwd note", payload["hookSpecificOutput"]["additionalContext"])

    def test_hook_never_raises_even_on_internal_error(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        with mock.patch.object(bootstrap, "build_context", side_effect=RuntimeError("boom")):
            out = self._run_main(json.dumps({"cwd": str(wiki)}))
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "")


class TestAutostartGating(BootstrapTestCase):
    """GAP D (shapa-backend-spec.md §5): SessionStart may autostart each
    in-scope wiki's `shapa serve` daemon, but ONLY when
    SHAPA_SERVE_AUTOSTART is set - unset (the default, and every OTHER test
    in this file) must never touch `serve.ensure_running` at all, so the
    rest of this suite's many direct/subprocess `bootstrap.main()` calls
    never spawn a real background process."""

    def _run_main(self, stdin_text: str, argv=None):
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit):
                bootstrap.main(argv or [])
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout

    def test_autostart_skipped_when_env_var_unset(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "n1", "a note")
        os.environ.pop(bootstrap.AUTOSTART_ENV_VAR, None)

        with mock.patch("shapa.serve.ensure_running") as mock_ensure:
            self._run_main(json.dumps({"cwd": str(wiki)}))
        mock_ensure.assert_not_called()

    def test_autostart_called_per_root_when_env_var_set(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "n1", "a note")
        os.environ[bootstrap.AUTOSTART_ENV_VAR] = "1"

        with mock.patch("shapa.serve.ensure_running") as mock_ensure:
            self._run_main(json.dumps({"cwd": str(wiki)}))
        mock_ensure.assert_called_once_with(wiki.resolve())

    def test_a_failed_autostart_never_breaks_session_start(self):
        wiki = self.tmp / "wiki"
        config.set_memory_dir(wiki)
        _note(wiki, "n1", "a note")
        os.environ[bootstrap.AUTOSTART_ENV_VAR] = "1"

        with mock.patch("shapa.serve.ensure_running", side_effect=RuntimeError("boom")):
            old_stdout = sys.stdout
            sys.stdin, sys.stdout = io.StringIO(json.dumps({"cwd": str(wiki)})), io.StringIO()
            try:
                with self.assertRaises(SystemExit) as exc:
                    bootstrap.main([])
                out = sys.stdout.getvalue()
            finally:
                sys.stdout = old_stdout
        self.assertEqual(exc.exception.code, 0)
        payload = json.loads(out)
        self.assertIn("n1", payload["hookSpecificOutput"]["additionalContext"])


class TestBootstrapSubprocess(unittest.TestCase):
    """One real end-to-end pass through `python -m shapa.bootstrap`, not just
    calling main() in-process - catches argv/stdin wiring bugs the in-process
    tests can't see."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_roundtrip(self):
        wiki = self.tmp / "wiki"
        _note(wiki, "sub-note", "a subprocess-visible note", locus="meta", consequence=9)

        env = dict(os.environ)
        env["SHAPA_MEMORY"] = str(wiki)
        proc = subprocess.run(
            [sys.executable, "-m", "shapa.bootstrap"],
            input=json.dumps({"cwd": str(wiki)}),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertIn("sub-note", payload["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
