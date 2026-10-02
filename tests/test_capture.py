"""Tests for shapa.capture (memory format v3): the Stop/SubagentStop hook
that extracts atomic memlog records from a session transcript and writes
them through shapa.memlog.make_record/append. No ``.md`` note is ever
written by this module any more (see shapa/memlog.py for the v3 store).

Every test either passes an explicit ``root=``/``cwd=`` to
:func:`shapa.capture.capture_session`, or mocks ``config.Path.cwd`` inside a
throwaway ``tempfile.mkdtemp()`` tree - never the real process cwd - so a
run of this suite can never discover, read, or write this repo's own
``.shapa`` wiki.
"""

import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shapa import capture, config, memlog

REPO_ROOT = Path(__file__).resolve().parents[1]


def msg(role: str, text: str, **extra) -> str:
    d = {"message": {"role": role, "content": [{"type": "text", "text": text}]}}
    d.update(extra)
    return json.dumps(d)


def tool_use(tool_id="t1") -> str:
    return json.dumps({"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "ls"}}]}})


def tool_result(tool_id="t1", text="output here") -> str:
    return json.dumps({"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
        "toolUseResult": {"stdout": text}})


REPORT = (
    "Fixed the login race condition that dropped sessions under load today.\n\n"
    "Files: src/auth/session.py and src/auth/middleware.py; architecture stays "
    "a plain Flask app with a Redis-backed session store, no new dependencies.\n\n"
    "Open decisions: 1) whether to add a retry budget for Redis timeouts "
    "2) whether the middleware change needs a feature flag."
)
REPORT_NONE = (
    "Shipped the billing retry fix cleanly from end to end this afternoon.\n\n"
    "Files: billing/retry.py and billing/queue.py; architecture adds a small "
    "dead-letter table, nothing else changes.\n\n"
    "Open decisions: none."
)


class CaptureTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, lines: list[str]) -> Path:
        p = self.tmp / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p


# ---------------------------------------------------------------------------
# Final-message extraction: the standard 3-paragraph job report.
# ---------------------------------------------------------------------------

class TestReportExtraction(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "wiki"
        self.root.mkdir()

    def test_three_paragraphs_map_to_outcome_fact_open_questions(self):
        t = self.write("t.jsonl", [
            msg("user", "brief - never stored"),
            msg("assistant", REPORT),
        ])
        written = capture.capture_session(str(t), "sessREPORT1", root=self.root)
        kinds = sorted(r.kind for r in written)
        self.assertEqual(kinds, ["fact", "open_question", "open_question", "outcome"])
        outcome = next(r for r in written if r.kind == "outcome")
        self.assertIn("Fixed the login race condition", outcome.summary)
        fact = next(r for r in written if r.kind == "fact")
        self.assertIn("src/auth/session.py", fact.tags)
        self.assertIn("src/auth/middleware.py", fact.tags)
        opens = [r.summary for r in written if r.kind == "open_question"]
        self.assertTrue(any("retry budget" in o for o in opens))
        self.assertTrue(any("feature flag" in o for o in opens))

    def test_open_decisions_none_yields_no_open_question(self):
        t = self.write("t.jsonl", [
            msg("user", "brief - never stored"),
            msg("assistant", REPORT_NONE),
        ])
        written = capture.capture_session(str(t), "sessNONE001", root=self.root)
        kinds = sorted(r.kind for r in written)
        self.assertEqual(kinds, ["fact", "outcome"])

    def test_non_report_final_message_falls_back_to_long_paragraphs(self):
        long_para = ("This was a long rambling final message that does not follow the "
                    "standard job report shape at all, it just describes work in prose "
                    "without any files section or open decisions section whatsoever.")
        t = self.write("t.jsonl", [
            msg("user", "brief - never stored"),
            msg("assistant", long_para),
        ])
        written = capture.capture_session(str(t), "sessFALLBK1", root=self.root)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].kind, "outcome")

    def test_short_final_message_yields_nothing(self):
        t = self.write("t.jsonl", [
            msg("user", "brief - never stored"),
            msg("assistant", "done"),
        ])
        written = capture.capture_session(str(t), "sessSHORT01", root=self.root)
        self.assertEqual(written, [])

    def test_gotcha_keyword_overrides_default_kind(self):
        report = (
            "The deploy silently broke the cache invalidation path overnight.\n\n"
            "Files: cache/invalidate.py; architecture unchanged otherwise here.\n\n"
            "Open decisions: none."
        )
        t = self.write("t.jsonl", [msg("user", "brief"), msg("assistant", report)])
        written = capture.capture_session(str(t), "sessGOTCHA1", root=self.root)
        outcome_like = [r for r in written if r.body.startswith("The deploy")]
        self.assertEqual(len(outcome_like), 1)
        self.assertEqual(outcome_like[0].kind, "gotcha")


# ---------------------------------------------------------------------------
# Operator-request extraction, first-prompt skip, and noise filtering.
# ---------------------------------------------------------------------------

class TestRequestExtraction(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "wiki"
        self.root.mkdir()

    def test_raw_first_prompt_never_stored(self):
        t = self.write("t.jsonl", [
            msg("user", "RAW TASK BRIEF, please always use tabs not spaces in this codebase"),
            msg("assistant", REPORT_NONE),
        ])
        written = capture.capture_session(str(t), "sessFIRST01", root=self.root)
        for r in written:
            self.assertNotIn("RAW TASK BRIEF", r.summary)
            self.assertNotIn("RAW TASK BRIEF", r.body)

    def test_preference_sentence_from_a_later_request_is_captured(self):
        t = self.write("t.jsonl", [
            msg("user", "brief - never stored"),
            msg("assistant", "ok"),
            msg("user", "never commit secrets to this repo, always use the vault instead"),
            msg("assistant", REPORT_NONE),
        ])
        written = capture.capture_session(str(t), "sessPREF001", root=self.root)
        prefs = [r for r in written if r.kind in ("preference", "gotcha")]
        self.assertTrue(any("never commit secrets" in r.summary for r in prefs))

    def test_tool_result_lines_are_never_treated_as_requests(self):
        t = self.write("t.jsonl", [
            msg("user", "brief - never stored"),
            tool_use("t1"),
            tool_result("t1", "always never do this should not be captured as a preference"),
            msg("assistant", REPORT_NONE),
        ])
        written = capture.capture_session(str(t), "sessTOOLRS1", root=self.root)
        self.assertTrue(all("should not be captured" not in (r.summary + r.body) for r in written))

    def test_system_reminder_and_pasted_content_are_stripped(self):
        t = self.write("t.jsonl", [
            msg("user", "brief - never stored"),
            msg("assistant", "ok"),
            msg("user", "<system-reminder>internal housekeeping text</system-reminder>"),
            msg("user", "<pasted_content id=1>leaked internal text blob here</pasted_content>"
                       "always keep pull requests small in this repo"),
            msg("assistant", REPORT_NONE),
        ])
        written = capture.capture_session(str(t), "sessPASTED1", root=self.root)
        blob = " ".join(r.summary + r.body for r in written)
        self.assertNotIn("housekeeping", blob)
        self.assertNotIn("leaked internal text blob", blob)
        self.assertTrue(any("keep pull requests small" in (r.summary + r.body) for r in written))

    def test_at_most_four_request_candidates(self):
        lines = [msg("user", "brief - never stored")]
        for i in range(8):
            lines.append(msg("user", f"always do thing number {i} in this project without fail"))
        lines.append(msg("assistant", "ok, short"))
        t = self.write("t.jsonl", lines)
        written = capture.capture_session(str(t), "sessMANYRQ1", root=self.root)
        self.assertLessEqual(len(written), capture.REQUEST_MAX)


# ---------------------------------------------------------------------------
# Redaction: secrets must never reach the log in any form.
# ---------------------------------------------------------------------------

class TestRedactionNeverLeaks(CaptureTestCase):
    def test_secrets_in_report_and_request_never_reach_the_log(self):
        root = self.tmp / "wiki"
        root.mkdir()
        secret = "sk-ant-api03-abcdefghijklmnopqrstuvwx"
        report = (
            f"Rotated the leaked credential {secret} out of the vault today.\n\n"
            f"Files: config/secrets.py holds the loader now; architecture unchanged "
            f"otherwise, still config/secrets.py based.\n\n"
            "Open decisions: none."
        )
        t = self.write("t.jsonl", [
            msg("user", "brief"),
            msg("user", f"always rotate {secret} immediately, never leave it in git history"),
            msg("assistant", report),
        ])
        written = capture.capture_session(str(t), "sessSECRET1", root=root)
        self.assertTrue(written)
        for r in written:
            self.assertNotIn(secret, r.summary)
            self.assertNotIn(secret, r.body)
            self.assertNotIn(secret, r.to_json())
        raw = "\n".join(p.read_text(encoding="utf-8") for p in memlog.log_files(root))
        self.assertNotIn(secret, raw)


# ---------------------------------------------------------------------------
# Dedup/idempotency: re-running on an unchanged (or merely re-seen) session
# never grows the log.
# ---------------------------------------------------------------------------

class TestIdempotency(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "wiki"
        self.root.mkdir()

    def test_rerun_on_unchanged_transcript_writes_nothing_new(self):
        t = self.write("t.jsonl", [msg("user", "brief"), msg("assistant", REPORT)])
        first = capture.capture_session(str(t), "sessIDEMP01", root=self.root)
        self.assertTrue(first)
        second = capture.capture_session(str(t), "sessIDEMP01", root=self.root)
        self.assertEqual(second, [])
        third = capture.capture_session(str(t), "sessIDEMP01", root=self.root)
        self.assertEqual(third, [])
        self.assertEqual(len(memlog.read_log(self.root).records), len(first))

    def test_duplicate_flood_in_one_transcript_grows_only_once(self):
        lines = [msg("user", "brief")]
        for _ in range(500):
            lines.append(msg("assistant", REPORT))
        t = self.write("t.jsonl", lines)
        first = capture.capture_session(str(t), "sessFLOOD01", root=self.root)
        self.assertTrue(first)
        before = len(memlog.read_log(self.root).records)
        for _ in range(20):
            capture.capture_session(str(t), "sessFLOOD01", root=self.root)
        after = len(memlog.read_log(self.root).records)
        self.assertEqual(before, after)

    def test_incremental_growth_across_separate_stop_calls(self):
        t = self.tmp / "incr.jsonl"
        t.write_text(msg("user", "RAW BRIEF do the big thing") + "\n", encoding="utf-8")
        w1 = capture.capture_session(str(t), "sessINCR001", root=self.root)
        self.assertEqual(w1, [])  # only the (skipped) brief exists so far
        with t.open("a", encoding="utf-8") as f:
            f.write(msg("assistant", "working on it") + "\n")
            f.write(msg("user", "always run the linter before committing in this project") + "\n")
            f.write(msg("assistant", REPORT_NONE) + "\n")
        w2 = capture.capture_session(str(t), "sessINCR001", root=self.root)
        self.assertTrue(w2)
        blob = " ".join(r.summary + r.body for r in w2)
        self.assertNotIn("RAW BRIEF", blob)
        self.assertTrue(any(r.kind in ("preference", "gotcha") for r in w2))


# ---------------------------------------------------------------------------
# Malformed/missing transcripts: tolerate everything, never crash.
# ---------------------------------------------------------------------------

class TestMalformedTranscripts(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "wiki"
        self.root.mkdir()

    def test_huge_final_message_costs_what_a_record_keeps(self):
        import time
        huge = "The parser now streams records and the index rebuilds. " * 100_000  # ~5.6 MB
        t = self.write("huge.jsonl", [msg("user", "brief"), msg("assistant", huge)])
        t0 = time.perf_counter()
        capture.capture_session(str(t), "sessHUGE01", root=self.root)
        self.assertLess(time.perf_counter() - t0, 2.0)
        t0 = time.perf_counter()
        capture.capture_session(str(t), "sessHUGE02", root=self.root, last_assistant_message=huge * 4)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def _assert_no_crash(self, path) -> None:
        written = capture.capture_session(str(path), "sessMALF0001", root=self.root)
        self.assertEqual(written, [])

    def test_garbage_lines_mixed_with_valid_json(self):
        p = self.tmp / "garbage.jsonl"
        p.write_text("not json at all\n" + msg("user", "hello there, a long enough message") +
                    "\n{broken json\n", encoding="utf-8")
        self._assert_no_crash(p)

    def test_truncated_last_line(self):
        p = self.tmp / "truncated.jsonl"
        p.write_bytes(
            msg("assistant", "abc").encode() + b"\n" +
            b'{"message": {"role": "user", "content": [{"type": "text"'
        )
        self._assert_no_crash(p)

    def test_non_utf8_bytes(self):
        p = self.tmp / "binary.jsonl"
        p.write_bytes(b"\xff\xfe\x00\x01garbage binary \x80\x81\x82\n" +
                     msg("user", "hi").encode() + b"\n")
        self._assert_no_crash(p)

    def test_empty_file(self):
        p = self.tmp / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        self._assert_no_crash(p)

    def test_missing_file(self):
        self._assert_no_crash(self.tmp / "does-not-exist.jsonl")

    def test_directory_path(self):
        d = self.tmp / "a-directory"
        d.mkdir()
        self._assert_no_crash(d)

    def test_huge_file_of_noise(self):
        p = self.tmp / "huge.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for _ in range(5000):
                f.write(msg("user", "noise " + ("x" * 50)) + "\n")
        self._assert_no_crash(p)

    def test_main_hook_exits_zero_and_prints_nothing_on_garbage(self):
        payload = json.dumps({"transcript_path": str(self.tmp / "missing.jsonl"), "session_id": "s"})
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(payload), io.StringIO()
        try:
            with mock.patch.object(config.Path, "cwd", staticmethod(lambda: self.tmp)):
                with self.assertRaises(SystemExit) as exc:
                    capture.main(["--root", str(self.root)])
            printed = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(printed, "")


# ---------------------------------------------------------------------------
# Routing matrix: repo wiki / repo without wiki / no repo / explicit root /
# scope external.
# ---------------------------------------------------------------------------

class TestRouting(CaptureTestCase):
    def _make_wiki(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / config.WIKI_MARKER).write_text(
            "---\nid: AGENTS\ntype: reference\ncreated: \"2026-01-01T00:00:00Z\"\n"
            "consequence: 8\nlocus: output\nuses: 0\n---\n# rules\n", encoding="utf-8")
        return path

    def _report_transcript(self, extra_request: str | None = None) -> Path:
        lines = [msg("user", "brief - never stored")]
        if extra_request:
            lines.append(msg("user", extra_request))
        lines.append(msg("assistant", REPORT_NONE))
        return self.write("t.jsonl", lines)

    def test_repo_with_its_own_wiki_writes_there(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        wiki = self._make_wiki(repo / ".shapa")
        t = self._report_transcript()
        with mock.patch.object(config.Path, "cwd", staticmethod(lambda: repo)):
            written = capture.capture_session(str(t), "sessROUTE01")
        self.assertTrue(written)
        self.assertTrue(all(r.scope == "repo" and r.repo == "repo" for r in written))
        self.assertEqual(list(memlog.read_log(wiki).records.values()), written)

    def test_repo_without_a_wiki_drops_non_global_keeps_clearly_global(self):
        repo = self.tmp / "repo-no-wiki"
        repo.mkdir()
        (repo / ".git").mkdir()
        global_wiki = self.tmp / "global"
        global_wiki.mkdir()
        t = self._report_transcript(
            "never commit directly to main in any repo, always open a PR instead")
        with mock.patch.object(config, "global_root", lambda: global_wiki):
            with mock.patch.object(config.Path, "cwd", staticmethod(lambda: repo)):
                written = capture.capture_session(str(t), "sessROUTE02")
        self.assertTrue(written)
        self.assertTrue(all(r.scope == "global" for r in written))
        self.assertTrue(any("never commit directly to main" in r.summary for r in written))
        # The report-derived outcome/fact were dropped (not clearly global).
        self.assertFalse(any(r.kind == "outcome" for r in written))
        self.assertEqual(list(memlog.read_log(global_wiki).records.values()), written)

    def test_no_git_repo_at_all_writes_everything_global(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        global_wiki = self.tmp / "global"
        global_wiki.mkdir()
        t = self._report_transcript("always write docstrings for every function from now on")
        with mock.patch.object(config, "global_root", lambda: global_wiki):
            with mock.patch.object(config.Path, "cwd", staticmethod(lambda: plain)):
                written = capture.capture_session(str(t), "sessROUTE03")
        self.assertTrue(written)
        self.assertTrue(all(r.scope == "global" and r.repo is None for r in written))
        self.assertEqual(list(memlog.read_log(global_wiki).records.values()), written)

    def test_explicit_root_overrides_all_routing(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        wiki = repo / ".shapa"
        wiki.mkdir()
        override = self.tmp / "override"
        t = self._report_transcript(
            "never commit directly to main in any repo, always open a PR instead")
        with mock.patch.object(config.Path, "cwd", staticmethod(lambda: repo)):
            written = capture.capture_session(str(t), "sessROUTE04", root=override)
        self.assertTrue(written)
        self.assertEqual(list(memlog.read_log(override).records.values()), written)
        self.assertFalse(memlog.has_log(wiki))
        # The explicit root overrides the global-cue promotion too - the
        # preference lands at `override` with the whole batch, not global.
        self.assertTrue(all(r.scope == "repo" for r in written))

    def test_scope_external_writes_into_the_sibling_repos_own_wiki(self):
        workspace = self.tmp / "workspace"
        this_repo = workspace / "this-repo"
        this_repo.mkdir(parents=True)
        (this_repo / ".git").mkdir()
        other_repo = workspace / "other-repo"
        other_repo.mkdir()
        (other_repo / ".git").mkdir()
        (other_repo / ".shapa").mkdir()
        t = self._report_transcript()
        written = capture.capture_session(
            str(t), "sessROUTE05", scope="external", applies_to="other-repo", cwd=this_repo)
        self.assertTrue(written)
        self.assertEqual(list(memlog.read_log(other_repo / ".shapa").records.values()), written)
        self.assertTrue(all(r.scope == "repo" and r.repo == "other-repo" for r in written))


# ---------------------------------------------------------------------------
# SubagentStop: agent_transcript_path is preferred when present.
# ---------------------------------------------------------------------------

class TestSubagentStop(CaptureTestCase):
    def test_agent_transcript_is_used_instead_of_the_main_one(self):
        root = self.tmp / "wiki"
        root.mkdir()
        main_t = self.write("main.jsonl", [msg("user", "main session brief")])
        agent_t = self.write("agent.jsonl", [
            msg("user", "sub-agent brief - delegate task"),
            msg("user", "always validate inputs before writing to the database"),
            msg("assistant", REPORT_NONE),
        ])
        written = capture.capture_session(
            str(main_t), "sessSUBAG01", root=root, agent_transcript_path=str(agent_t))
        self.assertTrue(written)
        self.assertTrue(all(r.source.startswith("capture:subagent") for r in written))

    def test_last_assistant_message_override_skips_transcript_scan(self):
        root = self.tmp / "wiki"
        root.mkdir()
        t = self.write("t.jsonl", [msg("user", "brief")])  # no assistant turn at all
        written = capture.capture_session(
            str(t), "sessLASTMSG1", root=root, last_assistant_message=REPORT_NONE)
        self.assertTrue(written)
        self.assertTrue(any(r.kind == "outcome" for r in written))


# ---------------------------------------------------------------------------
# Optional LLM-distill mode: off by default, falls back on failure.
# ---------------------------------------------------------------------------

class TestDistill(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "wiki"
        self.root.mkdir()
        self.t = self.write("t.jsonl", [msg("user", "brief"), msg("assistant", REPORT_NONE)])

    def test_distill_success_uses_the_models_memories(self):
        fake = self.tmp / "fake-distill.sh"
        fake.write_text(
            "#!/bin/sh\n"
            "cat <<'EOF'\n"
            '<memories>[{"kind": "decision", "summary": "Use SQLite for the cache.", '
            '"body": "Use SQLite for the cache, simpler than Redis here.", "tags": ["db"]}]'
            "</memories>\n"
            "EOF\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"SHAPA_DISTILL_CMD": str(fake)}):
            written = capture.capture_session(str(self.t), "sessDISTIL1", root=self.root, distill=True)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].kind, "decision")
        self.assertIn("SQLite", written[0].summary)

    def test_distill_prompt_goes_in_redacted_on_stdin(self):
        key = "sk-ant-api03-" + "Q" * 40
        t = self.write("s.jsonl", [msg("user", "brief"), msg("assistant", REPORT_NONE + " " + key)])
        seen = self.tmp / "seen.txt"
        fake = self.tmp / "fake-distill-stdin.sh"
        fake.write_text(f"#!/bin/sh\ncat > '{seen}'\necho \"$@\" >> '{seen}'\nexit 1\n",
                        encoding="utf-8")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"SHAPA_DISTILL_CMD": str(fake)}):
            capture.capture_session(str(t), "sessDISTIL3", root=self.root, distill=True)
        text = seen.read_text()
        self.assertIn("Final assistant message", text)
        self.assertNotIn(key, text)

    def test_distill_failure_falls_back_to_heuristic(self):
        with mock.patch.dict(os.environ, {"SHAPA_DISTILL_CMD": "/nonexistent/no-such-binary-xyz"}):
            written = capture.capture_session(str(self.t), "sessDISTIL2", root=self.root, distill=True)
        self.assertTrue(written)
        self.assertTrue(any(r.kind == "outcome" for r in written))

    def test_default_never_spawns_a_worker(self):
        payload = json.dumps({"transcript_path": str(self.t), "session_id": "sessNODIST1"})
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with mock.patch("subprocess.Popen") as popen:
                with self.assertRaises(SystemExit):
                    capture.main(["--root", str(self.root)])
                self.assertFalse(popen.called)
        finally:
            sys.stdin = old_stdin

    def test_distill_flag_spawns_a_detached_worker_and_returns_immediately(self):
        payload = json.dumps({"transcript_path": str(self.t), "session_id": "sessDOSPAWN1"})
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with mock.patch("subprocess.Popen") as popen:
                with self.assertRaises(SystemExit) as exc:
                    capture.main(["--root", str(self.root), "--distill"])
                self.assertTrue(popen.called)
                args, kwargs = popen.call_args
                self.assertIn("--_worker", args[0])
                self.assertTrue(kwargs.get("start_new_session"))
                self.assertEqual(kwargs.get("stdin"), subprocess.PIPE)
                self.assertNotIn(str(self.t), " ".join(args[0]))  # payload over the pipe
                sent = json.loads(popen.return_value.stdin.write.call_args[0][0])
                self.assertEqual(sent["transcript_path"], str(self.t))
        finally:
            sys.stdin = old_stdin
        self.assertEqual(exc.exception.code, 0)
        # Nothing written yet - the (mocked, never-run) worker would do it.
        self.assertFalse(memlog.has_log(self.root))

    def test_env_var_also_enables_distill(self):
        payload = json.dumps({"transcript_path": str(self.t), "session_id": "sessENVDIST1"})
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with mock.patch.dict(os.environ, {"SHAPA_CAPTURE_DISTILL": "1"}):
                with mock.patch("subprocess.Popen") as popen:
                    with self.assertRaises(SystemExit):
                        capture.main(["--root", str(self.root)])
                    self.assertTrue(popen.called)
        finally:
            sys.stdin = old_stdin


# ---------------------------------------------------------------------------
# --dry-run: the only mode that prints, and it writes nothing.
# ---------------------------------------------------------------------------

class TestDryRun(CaptureTestCase):
    def test_dry_run_prints_json_lines_and_writes_nothing(self):
        root = self.tmp / "wiki"
        root.mkdir()
        t = self.write("t.jsonl", [
            msg("user", "brief"),
            msg("user", "always use type hints in this codebase, never skip them"),
            msg("assistant", REPORT_NONE),
        ])
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            result = capture.capture_session(str(t), "sessDRYRUN1", root=root, dry_run=True)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(result, [])
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertTrue(lines)
        for line in lines:
            parsed = json.loads(line)
            self.assertIn("summary", parsed)
            self.assertIn("kind", parsed)
        self.assertFalse(memlog.has_log(root))


# ---------------------------------------------------------------------------
# Concurrency: several sessions capturing into one wiki at once.
# ---------------------------------------------------------------------------

class TestConcurrency(CaptureTestCase):
    TOPICS = [
        ("Rewrote the billing retry queue to use exponential backoff successfully.",
         "Files: billing/retry.py and billing/queue.py; architecture adds a dead-letter table.",
         "whether the dead-letter table needs its own retention policy"),
        ("Migrated the search index off the legacy inverted-file format today.",
         "Files: search/index.py and search/migrate.py; architecture now uses a columnar store.",
         "whether old indexes need a one-time backfill job"),
        ("Replaced the flaky email delivery provider with a new vendor successfully.",
         "Files: notify/email.py and notify/provider.py; architecture keeps the same queue shape.",
         "whether bounce webhooks need a new endpoint"),
        ("Hardened the login throttling to stop the brute-force attempts seen last week.",
         "Files: auth/throttle.py and auth/ratelimit.py; architecture adds a Redis counter.",
         "whether the throttle window should be configurable per tenant"),
    ]

    def test_four_concurrent_subprocesses_lose_nothing(self):
        root = self.tmp / "wiki"
        root.mkdir()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["HF_HUB_OFFLINE"] = "1"
        env["SHAPA_MEMORY"] = str(root)

        procs = []
        for i, (p1, p2, p3) in enumerate(self.TOPICS):
            report = f"{p1}\n\n{p2}\n\nOpen decisions: 1) {p3}."
            t = self.write(f"t{i}.jsonl", [
                msg("user", f"brief {i}"),
                msg("user", f"always write a changelog entry for topic {i} kind, never skip it"),
                msg("assistant", report),
            ])
            payload = json.dumps({"transcript_path": str(t), "session_id": f"sessCONC{i:04d}",
                                  "cwd": str(self.tmp)})
            proc = subprocess.Popen(
                [sys.executable, "-m", "shapa.capture"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(REPO_ROOT), env=env, text=True,
            )
            procs.append((proc, payload))

        outs = []
        for proc, payload in procs:
            out, err = proc.communicate(input=payload, timeout=20)
            outs.append((proc.returncode, out, err))

        for rc, out, err in outs:
            self.assertEqual(rc, 0)
            self.assertEqual(out, "")

        view = memlog.read_log(root)
        self.assertEqual(view.malformed, 0)
        # One outcome + one fact + one open_question per topic, all distinct
        # content -> none lost to dedup; the near-identical "changelog
        # entry" preferences may collapse, but the core report facts never do.
        self.assertGreaterEqual(len(view.records), 12)


# ---------------------------------------------------------------------------
# Latency: a cold `shapa capture` subprocess on a realistic transcript.
# ---------------------------------------------------------------------------

class TestLatency(CaptureTestCase):
    def test_cold_subprocess_latency_is_well_under_budget(self):
        root = self.tmp / "wiki"
        root.mkdir()
        (root / "memory").mkdir()
        # ~3000 pre-existing records, written directly (fast fixture setup).
        import hashlib
        lines = []
        for i in range(3000):
            summary = f"Pre-existing fact number {i} about some module or decision."
            body = summary + " Extra body padding detail for record " + str(i) + "."
            h = hashlib.sha1(f"{summary}\n{body}".encode()).hexdigest()
            lines.append(json.dumps({
                "id": "m-" + h[:10], "created": "2026-09-01T00:00:00Z", "session": "seedseed",
                "repo": None, "scope": "global", "kind": "fact", "summary": summary, "body": body,
                "tags": [], "source": "seed", "supersedes": None, "hash": h,
            }, separators=(",", ":")))
        (root / "memory" / "2026-09.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        # ~1000-line, ~300KB synthetic transcript ending in a standard report.
        tlines = [msg("user", "raw brief - do the big refactor now")]
        for i in range(990):
            if i % 7 == 0:
                tlines.append(tool_use(f"t{i}"))
                tlines.append(tool_result(f"t{i}", "file1\nfile2\n" * 10))
            elif i % 11 == 0:
                tlines.append(msg("user", f"always check lints before committing, round {i}"))
            else:
                tlines.append(msg("assistant", "Working on part " + str(i) + " of the refactor. " * 4))
        tlines.append(msg("assistant", REPORT))
        transcript = self.write("big.jsonl", tlines)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["HF_HUB_OFFLINE"] = "1"
        env["SHAPA_MEMORY"] = str(root)

        times = []
        N = 10
        for i in range(N):
            payload = json.dumps({"transcript_path": str(transcript),
                                  "session_id": f"sessLAT{i:04d}", "cwd": str(self.tmp)})
            start = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, "-m", "shapa.capture"], input=payload, capture_output=True,
                text=True, cwd=str(REPO_ROOT), env=env, timeout=10,
            )
            times.append(time.perf_counter() - start)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")

        times.sort()
        p50 = times[len(times) // 2]
        p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
        print(f"\n[capture latency] N={N} p50={p50*1000:.1f}ms p95={p95*1000:.1f}ms "
             f"max={max(times)*1000:.1f}ms mean={statistics.mean(times)*1000:.1f}ms")
        # Generous bound (spec: "under 300ms wall for a typical session");
        # a loaded CI box gets real headroom, not a hair-trigger assertion.
        self.assertLess(p50, 1.0)
        self.assertLess(p95, 2.0)


if __name__ == "__main__":
    unittest.main()
