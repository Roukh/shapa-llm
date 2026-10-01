"""Tests for the wiki upgrade mechanism (shapa-backend-spec.md §11):
the format marker + wiki registry (shapa/registry.py), ``shapa upgrade``
(shapa/upgrade.py), and its hooks into ``init``/``bootstrap``/``fetch``.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from shapa import bootstrap, cli, config, fetch, registry, store, upgrade

REPO_ROOT = Path(__file__).resolve().parent.parent

OLD_AGENTS = (
    "---\nid: AGENTS\ntype: reference\ncreated: \"2026-06-30T20:00:00Z\"\n"
    "consequence: 8\nlocus: output\nuses: 0\n---\n# old rules, pre-v2\n"
)


def _note(d: Path, nid: str, body: str, *, ntype: str = "memory", summary: bool = True,
          scope: str | None = None, uses: int | None = None, last_used: str | None = None,
          locus: str = "output", file_id: str | None = None, extra: tuple = ()) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"id: {file_id or nid}", f"type: {ntype}",
             'created: "2026-09-01T00:00:00Z"', "consequence: 5", f"locus: {locus}"]
    if summary:
        lines.append(f'summary: "{nid} in one line"')
    if scope:
        lines.append(f"scope: {scope}")
    if uses is not None:
        lines.append(f"uses: {uses}")
    if last_used:
        lines.append(f'last_used: "{last_used}"')
    lines += list(extra)
    lines.append("---")
    path = d / f"{nid}.md"
    path.write_text("\n".join(lines) + f"\n{body}\n", encoding="utf-8")
    return path


def _agenda(wiki: Path, items: int = 2, *, uses: int | None = None) -> None:
    fires = "\n".join(f"{i}. fire number {chr(96 + i)} needs attention" for i in range(1, items + 1))
    _note(wiki, "agenda", f"# Agenda\n\n{fires}", locus="meta", uses=uses)


def make_old_wiki(wiki: Path) -> Path:
    """A format-1 wiki as shapa 0.6.0 left it: legacy AGENTS.md, counters in
    frontmatter, no scope, no cache .gitignore, no marker."""
    wiki.mkdir(parents=True)
    (wiki / "AGENTS.md").write_text(OLD_AGENTS, encoding="utf-8")
    _agenda(wiki, uses=3)
    _note(wiki, "git-flow", "Branch off main, rebase before merge, never force-push shared branches.",
          uses=4, last_used="2026-09-20T10:00:00Z")
    _note(wiki, "deploy-rule", "Deploys run from tagged commits only; a hotfix still gets a tag.",
          ntype="rule", uses=0)
    _note(wiki / "arch", "design", "## Design\n\nThe service splits reads from writes.",
          ntype="reference", summary=False)
    return wiki


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class UpgradeTestCase(unittest.TestCase):
    """Isolated pointer (global root), registry, env and cwd."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.global_wiki = self.tmp / "global"
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.tmp / "config.json"),
            mock.patch.dict(os.environ, {registry.REGISTRY_ENV_VAR: str(self.tmp / "wikis.json")}),
            mock.patch.object(config.Path, "cwd", staticmethod(lambda: self.tmp)),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)
        config.set_memory_dir(self.global_wiki)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def repo_wiki(self, name: str = "repo") -> Path:
        repo = self.tmp / name
        (repo / ".git").mkdir(parents=True)
        return repo / ".shapa"

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as exc:
                upgrade.main(argv)
        return exc.exception.code, buf.getvalue()

    def codes(self, wiki: Path) -> dict[str, list[str]]:
        return {item.code: item.files for item in upgrade.work_items(wiki)}


# --- end to end ----------------------------------------------------------------

class TestEndToEnd(UpgradeTestCase):
    def test_old_wiki_upgrades_then_passes_check_and_a_second_upgrade_is_a_noop(self):
        wiki = make_old_wiki(self.repo_wiki())

        before = _snapshot(wiki)
        code, out = self.run_cli([str(wiki), "--check", "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["behind"], [str(wiki.resolve())])
        self.assertEqual(report["wikis"][0]["status"], "behind")
        self.assertEqual(report["wikis"][0]["format"], 1)
        self.assertEqual(set(report["wikis"][0]["mechanical"]),
                         {"docs", "gitignore", "counters", "frontmatter", "format"})
        self.assertEqual(report["wikis"][0]["work"], [])
        self.assertEqual(_snapshot(wiki), before, "--check must change nothing")

        code, out = self.run_cli([str(wiki)])
        self.assertEqual(code, 0, out)
        self.assertIn("current", out)

        self.assertEqual(registry.read_format(wiki), registry.CURRENT_FORMAT)
        self.assertEqual((wiki / "AGENTS.md").read_text(encoding="utf-8"),
                         (upgrade.ASSETS_DIR / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue((wiki / "placement.md").is_file())
        for p in wiki.rglob("*.md"):
            self.assertNotRegex(p.read_text(encoding="utf-8"), r"(?m)^(uses|last_used):", p.name)
        self.assertIn("scope: repo", (wiki / "git-flow.md").read_text(encoding="utf-8"))
        self.assertIn("scope: repo", (wiki / "arch" / "design.md").read_text(encoding="utf-8"))
        ignored = (wiki / ".gitignore").read_text(encoding="utf-8").splitlines()
        for name in upgrade.CACHE_IGNORES:
            self.assertIn(name, ignored)
        # The counters moved into the index store, not dropped.
        self.assertEqual(store.get_use(wiki, "git-flow"), (4, "2026-09-20T10:00:00Z"))
        self.assertEqual(store.get_use(wiki, "agenda")[0], 3)

        code, out = self.run_cli([str(wiki), "--check"])
        self.assertEqual(code, 0, out)

        after_first = _snapshot(wiki)
        code, out = self.run_cli([str(wiki)])
        self.assertEqual(code, 0, out)
        self.assertEqual(_snapshot(wiki), after_first, "a second upgrade must be a no-op")
        self.assertNotIn("mechanical applied", out)

    def test_cli_dispatch_through_python_m_shapa(self):
        wiki = make_old_wiki(self.repo_wiki())
        env = dict(os.environ, SHAPA_MEMORY=str(self.global_wiki))
        proc = subprocess.run(
            [sys.executable, "-m", "shapa", "upgrade", str(wiki), "--check", "--json"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["format"], registry.CURRENT_FORMAT)


# --- mechanical steps ----------------------------------------------------------

class TestMechanical(UpgradeTestCase):
    def test_scope_is_global_in_the_global_wiki(self):
        make_old_wiki(self.global_wiki)
        upgrade.upgrade_wiki(self.global_wiki)
        self.assertIn("scope: global", (self.global_wiki / "git-flow.md").read_text(encoding="utf-8"))

    def test_missing_id_and_empty_scope_are_filled_in_place(self):
        wiki = make_old_wiki(self.repo_wiki())
        p = wiki / "no-id.md"
        p.write_text("---\ntype: memory\ncreated: \"2026-09-01T00:00:00Z\"\nconsequence: 5\n"
                     "locus: output\nsummary: \"x\"\nscope:\n---\nA note with no id at all.\n",
                     encoding="utf-8")
        upgrade.upgrade_wiki(wiki)
        text = p.read_text(encoding="utf-8")
        self.assertIn("id: no-id", text)
        self.assertEqual(len(re.findall(r"(?m)^scope:", text)), 1)
        self.assertIn("scope: repo", text)
        self.assertTrue(text.endswith("A note with no id at all.\n"))

    def test_gitignore_appends_only_missing_entries_and_keeps_own_rules(self):
        wiki = make_old_wiki(self.repo_wiki())
        (wiki / ".gitignore").write_text("my-own-rule\n.shapa-index.db\n", encoding="utf-8")
        upgrade.upgrade_wiki(wiki)
        lines = (wiki / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "my-own-rule")
        self.assertEqual(lines.count(".shapa-index.db"), 1)
        for name in upgrade.CACHE_IGNORES:
            self.assertIn(name, lines)
        self.assertEqual(upgrade.step_gitignore(wiki, dry_run=True), [])

    def test_docs_refresh_never_touches_arch_or_notes(self):
        wiki = make_old_wiki(self.repo_wiki())
        design_before = (wiki / "arch" / "design.md").read_text(encoding="utf-8")
        self.assertEqual(upgrade.step_docs(wiki, dry_run=False), ["AGENTS.md", "placement.md"])
        self.assertEqual((wiki / "arch" / "design.md").read_text(encoding="utf-8"), design_before)
        self.assertEqual(upgrade.step_docs(wiki, dry_run=False), [])

    def test_seed_uses_keeps_the_larger_count_and_the_later_timestamp(self):
        wiki = self.repo_wiki()
        _note(wiki, "n", "body text here")
        store.record_use(wiki, "n")
        store.record_use(wiki, "n")
        uses, newer = store.get_use(wiki, "n")
        self.assertEqual(uses, 2)
        store.seed_uses(wiki, {"n": (1, "2000-01-01T00:00:00Z")})
        self.assertEqual(store.get_use(wiki, "n"), (2, newer))
        store.seed_uses(wiki, {"n": (9, "2999-01-01T00:00:00Z")})
        self.assertEqual(store.get_use(wiki, "n"), (9, "2999-01-01T00:00:00Z"))

    def test_a_wiki_ahead_of_this_shapa_is_never_touched(self):
        wiki = make_old_wiki(self.repo_wiki())
        registry.write_format(wiki, registry.CURRENT_FORMAT + 1)
        before = _snapshot(wiki)
        code, out = self.run_cli([str(wiki)])
        self.assertEqual(code, 0)
        self.assertIn("ahead", out)
        self.assertEqual(_snapshot(wiki), before)


# --- judgment items --------------------------------------------------------------

class TestJudgment(UpgradeTestCase):
    def test_work_left_keeps_the_marker_down_until_resolved(self):
        wiki = make_old_wiki(self.repo_wiki())
        _agenda(wiki, items=4)
        report = upgrade.upgrade_wiki(wiki)
        self.assertEqual(report.status, "behind")
        self.assertIn("F11", [w.code for w in report.work])
        self.assertEqual(registry.read_format(wiki), registry.LEGACY_FORMAT)
        self.assertNotIn("format", report.mechanical)

        _agenda(wiki, items=3)
        report = upgrade.upgrade_wiki(wiki)
        self.assertEqual(report.status, "current")
        self.assertEqual(registry.read_format(wiki), registry.CURRENT_FORMAT)

    def test_missing_agenda_is_judgment_not_a_placeholder(self):
        wiki = make_old_wiki(self.repo_wiki())
        (wiki / "agenda.md").unlink()
        upgrade.upgrade_wiki(wiki)
        self.assertIn("F11", self.codes(wiki))
        self.assertFalse((wiki / "agenda.md").exists())

    def test_over_cap_root_notes(self):
        wiki = make_old_wiki(self.repo_wiki())
        for i in range(41):
            _note(wiki, f"n{i}", f"alpha{i} beta{i} gamma{i} delta{i} epsilon{i}")
        self.assertIn("F10", self.codes(wiki))

    def test_duplicates_by_content_and_by_id(self):
        wiki = make_old_wiki(self.repo_wiki())
        body = "Always run the full suite with and without extras before calling a slice done."
        _note(wiki, "suite-rule", body)
        _note(wiki, "suite-rule-copy", body)
        _note(wiki / "arch", "git-flow-ref", "reference copy", ntype="reference", file_id="git-flow")
        codes = self.codes(wiki)
        self.assertEqual(sorted(codes["DUP"]), ["suite-rule-copy.md", "suite-rule.md"])
        self.assertEqual(sorted(codes["DUP-ID"]), ["arch/git-flow-ref.md", "git-flow.md"])

    def test_summary_required_for_notes_not_references(self):
        wiki = make_old_wiki(self.repo_wiki())
        _note(wiki, "no-summary", "a memory without its one-line summary", summary=False)
        self.assertEqual(self.codes(wiki)["F04"], ["no-summary.md"])

    def test_over_length_note(self):
        wiki = make_old_wiki(self.repo_wiki())
        _note(wiki, "long", " ".join(f"word{i}" for i in range(301)))
        self.assertEqual(self.codes(wiki)["F07"], ["long.md"])

    def test_layout_scope_and_frontmatter_problems(self):
        wiki = make_old_wiki(self.repo_wiki())
        _note(wiki / "notes", "tucked-away", "a live note in a side folder")
        _note(wiki, "misplaced", "claims to be global but lives in a repo wiki", scope="global")
        (wiki / "README.md").write_text("plain markdown, no frontmatter\n", encoding="utf-8")
        _note(wiki / "archive", "old", "archived notes are never judged", summary=False)
        codes = self.codes(wiki)
        self.assertEqual(codes["LAYOUT"], ["notes/tucked-away.md"])
        self.assertEqual(codes["F06"], ["misplaced.md"])
        self.assertEqual(codes["NOFM"], ["README.md"])
        self.assertNotIn("F04", codes)


# --- registry + marker -----------------------------------------------------------

class TestRegistry(UpgradeTestCase):
    def test_register_skips_non_wikis_and_never_rewrites_a_known_path(self):
        wiki = make_old_wiki(self.repo_wiki())
        self.assertEqual(registry.register([self.tmp / "nothing-here"], via="test"), [])
        self.assertFalse(registry.registry_path().exists())

        self.assertEqual(registry.register([wiki], via="test"), [str(wiki.resolve())])
        mtime = registry.registry_path().stat().st_mtime_ns
        self.assertEqual(registry.register([wiki], via="test"), [])
        self.assertEqual(registry.registry_path().stat().st_mtime_ns, mtime)
        self.assertEqual(registry.load()[str(wiki.resolve())]["via"], "test")

    def test_corrupt_registry_is_moved_aside_not_lost(self):
        wiki = make_old_wiki(self.repo_wiki())
        path = registry.registry_path()
        path.write_text("{not json", encoding="utf-8")
        registry.register([wiki], via="test")
        self.assertEqual(path.with_name(path.name + ".corrupt").read_text(encoding="utf-8"), "{not json")
        self.assertIn(str(wiki.resolve()), registry.load())

    def test_concurrent_sessions_never_corrupt_or_drop_entries(self):
        wikis = []
        for i in range(32):
            w = self.tmp / f"w{i}"
            w.mkdir()
            (w / "AGENTS.md").write_text("marker\n", encoding="utf-8")
            wikis.append(str(w))
        script = textwrap.dedent("""
            import sys
            from shapa import registry
            for p in sys.argv[1:]:
                registry.register([p], via="race")
        """)
        env = dict(os.environ)
        procs = [
            subprocess.Popen([sys.executable, "-c", script, *wikis[i::8]],
                             env=env, cwd=str(REPO_ROOT))
            for i in range(8)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)
        data = json.loads(registry.registry_path().read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["wikis"]), sorted(str(Path(w).resolve()) for w in wikis))
        leftovers = [p.name for p in registry.registry_path().parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_marker_and_behind_notice(self):
        wiki = make_old_wiki(self.repo_wiki())
        self.assertEqual(registry.read_format(wiki), registry.LEGACY_FORMAT)
        self.assertEqual(
            registry.behind_notice([wiki]),
            f"shapa: wiki {wiki} is format 1<{registry.CURRENT_FORMAT} - run the shapa-upgrade skill",
        )
        registry.write_format(wiki)
        self.assertEqual(registry.behind_notice([wiki]), "")
        self.assertEqual(registry.behind_notice([self.tmp / "not-a-wiki"]), "")


# --- CLI surface -----------------------------------------------------------------

class TestCli(UpgradeTestCase):
    def test_path_that_is_not_a_wiki_exits_2(self):
        (self.tmp / "plain").mkdir()
        with self.assertRaises(SystemExit) as exc:
            upgrade.main([str(self.tmp / "plain")])
        self.assertEqual(exc.exception.code, 2)

    def test_all_covers_registered_wikis_and_prunes_missing_ones_on_apply(self):
        old = make_old_wiki(self.repo_wiki("one"))
        gone = make_old_wiki(self.repo_wiki("two"))
        registry.register([old, gone], via="test")
        shutil.rmtree(gone)

        code, out = self.run_cli(["--all", "--check", "--json"])
        self.assertEqual(code, 1)
        status = {w["path"]: w["status"] for w in json.loads(out)["wikis"]}
        self.assertEqual(status[str(old.resolve())], "behind")
        self.assertEqual(status[str(gone.resolve())], "missing")
        self.assertIn(str(gone.resolve()), registry.load(), "--check never edits the registry")

        code, out = self.run_cli(["--all"])
        self.assertEqual(code, 0, out)
        self.assertNotIn(str(gone.resolve()), registry.load())
        self.assertIn(str(old.resolve()), registry.load())

    def test_print_skill_emits_the_bundled_skill(self):
        code, out = self.run_cli(["--print-skill"])
        self.assertEqual(code, 0)
        self.assertIn("name: shapa-upgrade", out)


class TestSkillAsset(unittest.TestCase):
    def test_frontmatter_meets_every_harness_contract(self):
        text = upgrade.SKILL_ASSET.read_text(encoding="utf-8")
        head = text.split("---")[1]
        name = re.search(r"(?m)^name: (.+)$", head).group(1).strip()
        desc = re.search(r"(?m)^description: (.+)$", head).group(1).strip()
        self.assertEqual(name, upgrade.SKILL_ASSET.parent.name)
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")  # OpenCode's rule
        self.assertLessEqual(len(desc), 1024)

    def test_skill_carries_the_upgrade_protocol(self):
        text = upgrade.SKILL_ASSET.read_text(encoding="utf-8")
        for needle in ("shapa upgrade --all --check --json", "shapa upgrade <wiki path>",
                       "claude -p", "Archive, never delete", "Never push",
                       "project's own", "shapa upgrade --all --check\n"):
            self.assertIn(needle, text)

    def test_skill_ships_in_the_package_data(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"assets/skills/*/SKILL.md"', pyproject)


# --- integration: init / bootstrap / fetch ------------------------------------------

class TestInitIntegration(UpgradeTestCase):
    def test_fresh_init_is_current_and_registered(self):
        wiki = self.tmp / "fresh"
        with redirect_stdout(io.StringIO()):
            cli._init([str(wiki)])
        self.assertEqual(registry.read_format(wiki), registry.CURRENT_FORMAT)
        self.assertEqual(upgrade.upgrade_wiki(wiki, check=True).status, "current")
        self.assertIn(str(wiki.resolve()), registry.load())
        self.assertFalse((wiki / "skills").exists(), "harness skills are never copied into a wiki")
        self.assertIn("scope: global", (wiki / "agenda.md").read_text(encoding="utf-8"))

    def test_reinit_never_migrates_an_existing_wiki(self):
        wiki = make_old_wiki(self.tmp / "legacy")
        before = (wiki / "git-flow.md").read_text(encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            cli._init([str(wiki)])
        self.assertFalse((wiki / registry.FORMAT_FILENAME).exists())
        self.assertEqual((wiki / "git-flow.md").read_text(encoding="utf-8"), before)


class TestHookIntegration(UpgradeTestCase):
    def _bootstrap(self, cwd: Path) -> str:
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(json.dumps({"cwd": str(cwd)})), io.StringIO()
        try:
            with self.assertRaises(SystemExit):
                bootstrap.main([])
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def test_bootstrap_appends_one_line_when_a_wiki_is_behind(self):
        wiki = make_old_wiki(self.repo_wiki())
        make_old_wiki(self.global_wiki)
        ctx = self._bootstrap(wiki.parent)
        lines = [ln for ln in ctx.splitlines() if "shapa-upgrade" in ln]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("shapa: wikis "))
        self.assertIn(str(wiki), lines[0])
        self.assertIn(str(self.global_wiki), lines[0])
        self.assertEqual(ctx.splitlines()[-1], lines[0])
        self.assertIn(str(wiki.resolve()), registry.load())

    def test_bootstrap_is_silent_once_current(self):
        wiki = make_old_wiki(self.repo_wiki())
        upgrade.upgrade_wiki(wiki)
        ctx = self._bootstrap(wiki.parent)
        self.assertNotIn("shapa-upgrade", ctx)
        self.assertIn("git-flow", ctx)

    def test_fetch_registers_the_wiki_it_read(self):
        wiki = make_old_wiki(self.repo_wiki())
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                fetch.main(["--query", "branch rebase", "--root", str(wiki), "--no-record"])
        self.assertIn(str(wiki.resolve()), registry.load())


if __name__ == "__main__":
    unittest.main()
