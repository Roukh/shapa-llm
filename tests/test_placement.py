"""Tests for shapa-backend-spec.md §8 Slice 7: ``capture.py --scope`` /
``shapa save --scope {global,repo,external} [--applies-to]``, the generic
``placement.md`` asset, scope inference, and the "capture never blocks"
contract.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shapa import capture, cli, config, memlog, save
from shapa.frontmatter import parse as parse_frontmatter


def _make_git_repo(path: Path) -> Path:
    """A directory that *looks* like a git checkout - same fixture style as
    tests/test_config_multiroot.py's ``_make_git_repo``."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


def _make_wiki(root: Path) -> Path:
    """A minimal valid wiki (``.shapa/AGENTS.md``)."""
    wiki = root / ".shapa"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / config.WIKI_MARKER).write_text(
        "---\nid: AGENTS\ntype: reference\ncreated: \"2026-01-01T00:00:00Z\"\n"
        "consequence: 8\nlocus: output\nuses: 0\n---\n# rules\n",
        encoding="utf-8",
    )
    return wiki


class IsolatedTestCase(unittest.TestCase):
    """Isolates $SHAPA_MEMORY and the ~/.shapa/config.json pointer, same
    harness as tests/test_config_multiroot.py."""

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


# ---------------------------------------------------------------------------
# placement.md: generic, no hardcoded repo, installed by `shapa init`.
# ---------------------------------------------------------------------------

class TestPlacementAsset(IsolatedTestCase):
    def test_installed_by_init(self):
        wiki = self.tmp / "wiki"
        cli._init([str(wiki)])
        self.assertTrue((wiki / "placement.md").is_file())

    def test_documents_the_decision_rule(self):
        text = cli.ASSETS_DIR.joinpath("placement.md").read_text(encoding="utf-8")
        self.assertIn("global", text)
        self.assertIn("repo", text)
        self.assertIn("Would this still be true", text)

    def test_is_generic_no_hardcoded_repo_allowlist(self):
        # A per-project asset must not bake in *this* workspace's own repo
        # names - it ships to every user of the tool, on any project. The
        # folder this checkout sits in stands for the workspace's own name.
        text = cli.ASSETS_DIR.joinpath("placement.md").read_text(encoding="utf-8")
        workspace = Path(__file__).resolve().parents[2].name
        for name in ("roukh-llm", "roukh-brain", "shapa-llm", "open-trader", workspace):
            self.assertNotIn(name, text)

    def test_valid_as_a_node(self):
        parsed = parse_frontmatter(cli.ASSETS_DIR / "placement.md")
        self.assertEqual(parsed.meta.get("id"), "placement")
        self.assertEqual(parsed.meta.get("type"), "reference")


# ---------------------------------------------------------------------------
# config.find_sibling_repo: generic, no hardcoded list, `--applies-to`'s
# repo-resolution mechanism (spec §10 decision 4).
# ---------------------------------------------------------------------------

class TestFindSiblingRepo(IsolatedTestCase):
    def test_finds_a_sibling_git_checkout_by_name(self):
        workspace = self.tmp / "workspace"
        this_repo = _make_git_repo(workspace / "this-repo")
        other_repo = _make_git_repo(workspace / "other-repo")

        found = config.find_sibling_repo("other-repo", this_repo)
        self.assertEqual(found, other_repo.resolve())

    def test_returns_none_when_not_found(self):
        workspace = self.tmp / "workspace"
        this_repo = _make_git_repo(workspace / "this-repo")
        self.assertIsNone(config.find_sibling_repo("does-not-exist", this_repo))

    def test_returns_none_for_a_non_git_sibling_directory(self):
        workspace = self.tmp / "workspace"
        this_repo = _make_git_repo(workspace / "this-repo")
        (workspace / "just-a-folder").mkdir()
        self.assertIsNone(config.find_sibling_repo("just-a-folder", this_repo))

    def test_returns_none_for_empty_name(self):
        this_repo = _make_git_repo(self.tmp / "this-repo")
        self.assertIsNone(config.find_sibling_repo("", this_repo))
        self.assertIsNone(config.find_sibling_repo(None, this_repo))


# ---------------------------------------------------------------------------
# shapa.save.infer_scope: unambiguous default, never a guess.
# ---------------------------------------------------------------------------

class TestInferScope(IsolatedTestCase):
    def test_infers_repo_when_local_wiki_exists(self):
        repo = _make_git_repo(self.tmp / "repo")
        _make_wiki(repo)
        self.assertEqual(save.infer_scope(repo, None), "repo")

    def test_infers_global_when_no_local_wiki(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(save.infer_scope(plain, None), "global")

    def test_applies_to_implies_external_regardless_of_local_wiki(self):
        repo = _make_git_repo(self.tmp / "repo")
        _make_wiki(repo)
        self.assertEqual(save.infer_scope(repo, "some-other-repo"), "external")


# ---------------------------------------------------------------------------
# shapa.save.save_note: the write path itself.
# ---------------------------------------------------------------------------

class TestSaveNote(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.global_wiki = self.tmp / "global"
        config.set_memory_dir(self.global_wiki)

    def test_global_scope_writes_into_the_global_wiki(self):
        out = save.save_note("global", "A global fact.", "Body text here.")
        self.assertNotIn("error", out)
        self.assertEqual(out["scope"], "global")
        self.assertTrue((self.global_wiki / f"{out['id']}.md").is_file())

    def test_scope_inferred_when_omitted_and_unambiguous(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        out = save.save_note(None, "Inferred global.", "Body.", start=plain)
        self.assertNotIn("error", out)
        self.assertEqual(out["scope"], "global")

        repo = _make_git_repo(self.tmp / "repo")
        _make_wiki(repo)
        out2 = save.save_note(None, "Inferred repo.", "Body.", start=repo)
        self.assertNotIn("error", out2)
        self.assertEqual(out2["scope"], "repo")
        self.assertTrue((repo / ".shapa" / f"{out2['id']}.md").is_file())

    def test_repo_scope_requires_an_existing_repo_wiki(self):
        no_wiki_repo = _make_git_repo(self.tmp / "no-wiki-repo")
        out = save.save_note("repo", "x", "y", start=no_wiki_repo)
        self.assertIn("error", out)
        self.assertIn("shapa init", out["error"])

    def test_repo_scope_writes_into_this_repos_own_wiki(self):
        repo = _make_git_repo(self.tmp / "repo")
        _make_wiki(repo)
        out = save.save_note("repo", "A repo fact.", "Body.", start=repo)
        self.assertNotIn("error", out)
        self.assertEqual(out["scope"], "repo")
        path = Path(out["path"])
        self.assertEqual(path.parent, (repo / ".shapa").resolve())
        meta = parse_frontmatter(path).meta
        self.assertEqual(meta["scope"], "repo")

    def test_external_without_applies_to_errors_clearly(self):
        out = save.save_note("external", "x", "y")
        self.assertIn("error", out)
        self.assertIn("--applies-to", out["error"])
        # And nothing was written anywhere.
        self.assertEqual(list(self.global_wiki.rglob("*.md")), [])

    def test_applies_to_without_external_scope_errors_clearly(self):
        out = save.save_note("global", "x", "y", applies_to="some-repo")
        self.assertIn("error", out)

    def test_external_with_unresolvable_repo_errors_and_never_falls_back_to_global(self):
        this_repo = _make_git_repo(self.tmp / "workspace" / "this-repo")
        out = save.save_note(
            "external", "x", "y", applies_to="nonexistent-repo", start=this_repo,
        )
        self.assertIn("error", out)
        self.assertIn("nonexistent-repo", out["error"])
        # Never falls back to the global wiki (decision 4).
        self.assertEqual(list(self.global_wiki.rglob("*.md")), [])

    def test_external_with_applies_to_writes_into_the_other_repos_own_wiki(self):
        workspace = self.tmp / "workspace"
        this_repo = _make_git_repo(workspace / "this-repo")
        other_repo = _make_git_repo(workspace / "other-repo")
        _make_wiki(other_repo)

        out = save.save_note(
            "external", "About the other repo.", "Body.",
            applies_to="other-repo", start=this_repo,
        )
        self.assertNotIn("error", out)
        # Stored scope is "repo" (matching the physical bucket it landed
        # in) - "external" is never a stored frontmatter value (decision 6).
        self.assertEqual(out["scope"], "repo")
        self.assertEqual(out["applies_to"], "other-repo")
        path = Path(out["path"])
        self.assertEqual(path.parent, (other_repo / ".shapa").resolve())
        # Nothing landed in this repo's own wiki or the global wiki.
        self.assertEqual(list(self.global_wiki.rglob("*.md")), [])

    def test_external_creates_the_target_repos_wiki_folder_if_needed(self):
        workspace = self.tmp / "workspace"
        this_repo = _make_git_repo(workspace / "this-repo")
        other_repo = _make_git_repo(workspace / "other-repo")  # no wiki yet

        out = save.save_note(
            "external", "Needs a fresh wiki.", "Body.",
            applies_to="other-repo", start=this_repo,
        )
        self.assertNotIn("error", out)
        self.assertTrue((other_repo / ".shapa").is_dir())

    def test_rejects_an_id_that_would_collide_across_roots(self):
        repo = _make_git_repo(self.tmp / "repo")
        wiki = _make_wiki(repo)
        (wiki / "dup-id.md").write_text(
            "---\nid: dup-id\ntype: memory\ncreated: \"2026-01-01T00:00:00Z\"\n"
            "consequence: 5\nlocus: output\nuses: 0\n---\nexisting\n",
            encoding="utf-8",
        )
        out = save.save_note("global", "x", "y", id="dup-id", start=repo)
        self.assertIn("error", out)
        self.assertIn("F09", out["error"])

    def test_never_overwrites_an_existing_id(self):
        first = save.save_note("global", "First.", "Body one.", id="dup")
        self.assertNotIn("error", first)
        second = save.save_note("global", "Second.", "Body two.", id="dup")
        self.assertIn("error", second)

    def test_rejects_invalid_type_and_tags(self):
        self.assertIn("error", save.save_note("global", "x", "y", note_type="bogus"))
        self.assertIn("error", save.save_note("global", "x", "y", tags="not-a-list"))

    def test_requires_summary_and_body(self):
        self.assertIn("error", save.save_note("global", "", "y"))
        self.assertIn("error", save.save_note("global", "x", ""))


# ---------------------------------------------------------------------------
# `shapa save` CLI + wiring.
# ---------------------------------------------------------------------------

class TestSaveCLI(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.global_wiki = self.tmp / "global"
        config.set_memory_dir(self.global_wiki)

    def _run(self, argv):
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            with self.assertRaises(SystemExit) as exc:
                save.main(argv)
            return exc.exception.code, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

    def test_cli_writes_a_global_note(self):
        code, out, err = self._run(["--scope", "global", "--summary", "A note.", "--body", "Body."])
        self.assertEqual(code, 0)
        self.assertIn("saved", out)

    def test_cli_external_without_applies_to_errors_clearly_and_nonzero_exit(self):
        code, out, err = self._run(["--scope", "external", "--summary", "x", "--body", "y"])
        self.assertEqual(code, 1)
        self.assertIn("--applies-to", err)

    def test_registered_in_cli_dispatch(self):
        self.assertIn("save", cli._SUBMODULES)
        self.assertIn("save", cli.USAGE)

    def test_cli_main_dispatches_to_save(self):
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as exc:
                cli.main(["save", "--scope", "global", "--summary", "Via cli.main.", "--body", "Body."])
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("saved", out)


# ---------------------------------------------------------------------------
# capture.py --scope: the Stop-hook's own scope-aware write path, which
# must never block regardless of what scope resolves to.
# ---------------------------------------------------------------------------

class TestCaptureScope(IsolatedTestCase):
    """capture.py --root/--scope (manual overrides): bypass per-record
    routing entirely and send every v3 record to one explicit target - the
    Stop-hook's own scope-aware write path, which must never block
    regardless of what scope resolves to."""

    def setUp(self):
        super().setUp()
        self.global_wiki = self.tmp / "global"
        # IsolatedTestCase already gives this test its own patched environ
        # and clears the override var; set it here so every call this class
        # makes resolves the global wiki to a throwaway tmp dir.
        os.environ[config.ENV_VAR] = str(self.global_wiki)
        self.transcript = self.tmp / "t.jsonl"
        self.transcript.write_text(
            json.dumps({"message": {"role": "user", "content": [
                {"type": "text", "text": "the raw task brief - never stored"}]}}) + "\n" +
            json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": (
                "Did something worth remembering in this session today for real.\n\n"
                "Files: a/b.py and c/d.py; architecture is unchanged otherwise here.\n\n"
                "Open decisions: none."
            )}]}}) + "\n",
            encoding="utf-8",
        )

    def _records(self, root) -> list:
        return list(memlog.read_log(root).records.values())

    def test_default_scope_matches_pre_existing_inference(self):
        # No --scope, no --applies-to, no explicit --root: automatic
        # per-record routing - no git repo at all -> everything global.
        plain = self.tmp / "plain"
        plain.mkdir()
        with mock.patch.object(config.Path, "cwd", staticmethod(lambda: plain)):
            written = capture.capture_session(str(self.transcript), "sessAAAAAA")
        self.assertTrue(written)
        self.assertEqual(self._records(self.global_wiki), written)

    def test_explicit_global_scope_overrides_a_discoverable_repo_wiki(self):
        repo = _make_git_repo(self.tmp / "repo")
        wiki = _make_wiki(repo)
        with mock.patch.object(config.Path, "cwd", staticmethod(lambda: repo)):
            written = capture.capture_session(
                str(self.transcript), "sessBBBBBB", scope="global",
            )
        self.assertTrue(written)
        self.assertEqual(self._records(self.global_wiki), written)
        self.assertFalse(memlog.has_log(wiki))

    def test_external_scope_with_resolvable_repo_writes_there(self):
        workspace = self.tmp / "workspace"
        this_repo = _make_git_repo(workspace / "this-repo")
        other_repo = _make_git_repo(workspace / "other-repo")
        _make_wiki(other_repo)
        with mock.patch.object(config.Path, "cwd", staticmethod(lambda: this_repo)):
            written = capture.capture_session(
                str(self.transcript), "sessCCCCCC",
                scope="external", applies_to="other-repo",
            )
        self.assertTrue(written)
        self.assertEqual(self._records(other_repo / ".shapa"), written)
        self.assertTrue(all(r.scope == "repo" for r in written))

    def test_unresolvable_external_scope_never_blocks_falls_back_instead(self):
        this_repo = _make_git_repo(self.tmp / "this-repo")
        # No --applies-to at all, and no matching repo either - must not
        # raise, must still write somewhere sane (never blocks): this_repo
        # has no wiki of its own, so it falls back to the global wiki.
        with mock.patch.object(config.Path, "cwd", staticmethod(lambda: this_repo)):
            written = capture.capture_session(
                str(self.transcript), "sessDDDDDD", scope="external",
            )
        self.assertTrue(written)
        self.assertEqual(self._records(self.global_wiki), written)

    def test_hook_main_never_blocks_on_a_bad_scope_combination(self):
        plain = self.tmp / "plain-cwd"
        plain.mkdir()
        payload = json.dumps({"transcript_path": str(self.transcript), "session_id": "sessEEEEEE"})
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with mock.patch.object(config.Path, "cwd", staticmethod(lambda: plain)):
                with self.assertRaises(SystemExit) as exc:
                    capture.main(["--scope", "external"])  # no --applies-to
        finally:
            sys.stdin = old_stdin
        self.assertEqual(exc.exception.code, 0)
        # Never blocked, and still wrote the record (fell back to global).
        self.assertTrue(self._records(self.global_wiki))

    def test_hook_main_never_blocks_even_if_capture_session_raises(self):
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("{\"transcript_path\": \"nonexistent.jsonl\", \"session_id\": \"x\"}")
        try:
            with mock.patch.object(capture, "capture_session", side_effect=RuntimeError("boom")):
                with self.assertRaises(SystemExit) as exc:
                    capture.main([])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(exc.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
