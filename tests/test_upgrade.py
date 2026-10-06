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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from shapa import db, bootstrap, cli, config, fetch, memlog, registry, store, upgrade

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


def _session_note(wiki: Path, sid: str, requests: str, *, related: tuple = (),
                  uses: int = 0, created: str = "2026-09-01T00:00:00Z") -> Path:
    """A v2 capture-hook note (``shapa.capture.capture_session``'s exact
    on-disk shape), for exercising ``shapa upgrade``'s session-note
    conversion against real pre-v3 fixtures."""
    note_id = f"memory-session-{sid}"
    related_str = " ".join(f"[[{r}]]" for r in related)
    body = (
        f"Captured at the end of session {sid}. The operator's requests this "
        f"session: {requests}\n\n"
        "Type: [[memory]]." + (f" Related: {related_str}." if related_str else "")
    )
    text = (
        "---\n"
        f"id: {note_id}\n"
        "type: memory\n"
        f'created: "{created}"\n'
        "consequence: 4\n"
        "locus: output-meta\n"
        f"uses: {uses}\n"
        "---\n"
        f"{body}\n"
    )
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / f"{note_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


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
                         {"docs", "database", "gitignore", "gitattributes", "counters",
                          "frontmatter", "format"})
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
        self.assertIn("scope: repo", (wiki / "arch" / "design.md").read_text(encoding="utf-8"))
        ignored = (wiki / ".gitignore").read_text(encoding="utf-8").splitlines()
        for name in upgrade.CACHE_IGNORES:
            self.assertIn(name, ignored)
        # Format 4: memory/rule notes became database rows (old id kept as
        # alias), agenda.md is retired, the database is marked binary.
        for gone in ("git-flow.md", "deploy-rule.md", "agenda.md"):
            self.assertFalse((wiki / gone).exists(), gone)
        conn = db.connect(wiki)
        try:
            self.assertEqual(db.get_row(conn, "git-flow").kind, "M")
            self.assertIn("Branch off main", db.get_row(conn, "git-flow").body)
            self.assertEqual(db.get_row(conn, "deploy-rule").kind, "R")
        finally:
            conn.close()
        self.assertIn(upgrade.DB_GITATTRIBUTES_LINE,
                      (wiki / ".gitattributes").read_text(encoding="utf-8"))

        code, out = self.run_cli([str(wiki), "--check"])
        self.assertEqual(code, 0, out)

        after_first = _snapshot(wiki)
        code, out = self.run_cli([str(wiki)])
        self.assertEqual(code, 0, out)
        self.assertEqual(_snapshot(wiki), after_first, "a second upgrade must be a no-op")
        self.assertNotIn("mechanical applied", out)

    def test_check_spells_out_the_counter_diff_and_that_it_is_committed(self):
        wiki = make_old_wiki(self.repo_wiki())
        expected = {
            "agenda.md": ["uses: 3"],
            "git-flow.md": ["uses: 4", 'last_used: "2026-09-20T10:00:00Z"'],
            "deploy-rule.md": ["uses: 0"],
        }
        before = _snapshot(wiki)

        code, out = self.run_cli([str(wiki), "--check", "--json"])
        report = json.loads(out)
        self.assertEqual(report["wikis"][0]["counters"], expected)
        self.assertEqual(sorted(report["wikis"][0]["mechanical"]["counters"]), sorted(expected))
        self.assertIn("never restore", report["steps"]["counters"])

        code, out = self.run_cli([str(wiki), "--check"])
        self.assertEqual(code, 1)
        self.assertIn("[counters] 3 note(s)", out)
        self.assertIn('git-flow.md: -uses: 4  -last_used: "2026-09-20T10:00:00Z"', out)
        self.assertIn("commit it with the upgrade, never restore it", out)
        self.assertEqual(_snapshot(wiki), before, "--check must change nothing")

        # Apply reports what it deleted; once stripped, nothing is left to report.
        self.assertEqual(upgrade.upgrade_wiki(wiki).counters, expected)
        self.assertEqual(upgrade.upgrade_wiki(wiki, check=True).counters, {})
        code, out = self.run_cli([str(wiki), "--check"])
        self.assertNotIn("[counters]", out)

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
        self.assertIn("scope: global",
                      (self.global_wiki / "arch" / "design.md").read_text(encoding="utf-8"))

    def test_missing_id_and_empty_scope_are_filled_in_place(self):
        wiki = make_old_wiki(self.repo_wiki())
        p = wiki / "no-id.md"
        p.write_text("---\ntype: reference\ncreated: \"2026-09-01T00:00:00Z\"\nconsequence: 5\n"
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

    def test_gitignore_header_is_written_once_across_upgrades(self):
        wiki = make_old_wiki(self.repo_wiki())
        (wiki / ".gitignore").write_text("my-own-rule\n", encoding="utf-8")
        upgrade.step_gitignore(wiki, dry_run=False)
        text = (wiki / ".gitignore").read_text(encoding="utf-8")
        (wiki / ".gitignore").write_text(text.replace("temp/\n", ""), encoding="utf-8")
        upgrade.step_gitignore(wiki, dry_run=False)
        lines = (wiki / ".gitignore").read_text(encoding="utf-8").splitlines()
        header = upgrade.CACHE_GITIGNORE.split("\n", 1)[0]
        self.assertEqual(lines.count(header), 1)
        self.assertIn("temp/", lines)

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
        broken = wiki / "arch" / "broken.md"
        broken.write_text("---\nid: [unclosed\n---\nbody\n", encoding="utf-8")
        report = upgrade.upgrade_wiki(wiki)
        self.assertEqual(report.status, "behind")
        self.assertIn("F02", [w.code for w in report.work])
        self.assertEqual(registry.read_format(wiki), registry.LEGACY_FORMAT)
        self.assertNotIn("format", report.mechanical)

        broken.unlink()
        report = upgrade.upgrade_wiki(wiki)
        self.assertEqual(report.status, "current")
        self.assertEqual(registry.read_format(wiki), registry.CURRENT_FORMAT)

    def test_format_4_retires_the_agenda_file(self):
        wiki = make_old_wiki(self.repo_wiki())
        upgrade.upgrade_wiki(wiki)
        self.assertNotIn("F11", self.codes(wiki))
        self.assertFalse((wiki / "agenda.md").exists())

    def test_missing_agenda_is_still_f11_before_the_database_exists(self):
        wiki = make_old_wiki(self.repo_wiki())
        (wiki / "agenda.md").unlink()
        self.assertIn("F11", self.codes(wiki))

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

    def test_checklist_over_length_is_exempt_from_f07(self):
        # checklist.md (like ideas.md) is an append-only/ever-growing
        # convention file by design - `shapa upgrade --check` must never
        # list it under F07, or every wiki with a real checklist would
        # report "behind" forever.
        wiki = make_old_wiki(self.repo_wiki())
        _note(wiki, "checklist", " ".join(f"word{i}" for i in range(400)), locus="meta")
        _note(wiki, "ideas", " ".join(f"word{i}" for i in range(400)), locus="meta")
        codes = self.codes(wiki)
        self.assertNotIn("F07", codes)

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
                       "project's own", "shapa upgrade --all --check\n",
                       "The counter removal is part of this commit", "Never restore them"):
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
        # Not the global wiki (init DIR leaves the pointer alone) -> repo scope.
        self.assertIn("scope: repo", (wiki / "arch" / "index.md").read_text(encoding="utf-8"))
        self.assertTrue(db.exists(wiki))
        self.assertEqual(config.global_root(), self.global_wiki)

    def test_fresh_init_global_is_current_with_global_scope(self):
        wiki = self.tmp / "new-global"
        with redirect_stdout(io.StringIO()):
            cli._init(["--global", str(wiki)])
        self.assertEqual(config.global_root(), wiki.resolve())
        self.assertEqual(upgrade.upgrade_wiki(wiki, check=True).status, "current")
        self.assertIn("scope: global", (wiki / "arch" / "index.md").read_text(encoding="utf-8"))

    def test_reinit_never_migrates_an_existing_wiki(self):
        wiki = make_old_wiki(self.tmp / "legacy")
        before = (wiki / "git-flow.md").read_text(encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            cli._init([str(wiki)])
        self.assertFalse((wiki / registry.FORMAT_FILENAME).exists())
        self.assertEqual((wiki / "git-flow.md").read_text(encoding="utf-8"), before)

    def test_adopted_folder_is_behind_until_upgrade_then_current(self):
        wiki = make_old_wiki(self.tmp / "marker-less")
        (wiki / "AGENTS.md").unlink()  # a folder of notes that never had the marker
        before = {p: (wiki / p).read_text(encoding="utf-8") for p in ("git-flow.md", "agenda.md")}
        with redirect_stdout(io.StringIO()):
            cli._init([str(wiki)])
        for rel, text in before.items():
            self.assertEqual((wiki / rel).read_text(encoding="utf-8"), text, rel)
        self.assertEqual(upgrade.upgrade_wiki(wiki, check=True).status, "behind")
        upgrade.upgrade_wiki(wiki)
        self.assertEqual(upgrade.upgrade_wiki(wiki, check=True).status, "current")


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


# --- format 3: memory-session conversion ----------------------------------------

class TestMemoryConversion(UpgradeTestCase):
    """``shapa upgrade``'s format-2 -> 3 session-note conversion: every live
    ``memory-session-*.md`` note becomes one v3 memory-log record
    (shapa.memlog), carries its use counter over, and is archived - never
    deleted."""

    def _wiki_with_sessions(self) -> Path:
        wiki = make_old_wiki(self.repo_wiki())
        _session_note(wiki, "abc12345", "Wire the new memlog store into capture.",
                      related=("git-flow",), uses=3)
        _session_note(
            wiki, "def67890",
            "Rotate the deploy key sk-ant-api03-abcdefghijklmnopqrstuvwx before shipping.",
            uses=0,
        )
        return wiki

    def test_check_lists_memory_and_gitattributes_without_changing_a_byte(self):
        wiki = self._wiki_with_sessions()
        before = _snapshot(wiki)

        code, out = self.run_cli([str(wiki), "--check", "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)["wikis"][0]
        self.assertIn("memory", report["mechanical"])
        self.assertIn("gitattributes", report["mechanical"])
        self.assertEqual(
            sorted(report["mechanical"]["memory"]),
            ["memory-session-abc12345.md", "memory-session-def67890.md"],
        )
        self.assertEqual(_snapshot(wiki), before, "--check must change nothing")

    def test_upgrade_converts_archives_redacts_and_carries_uses(self):
        wiki = self._wiki_with_sessions()

        code, out = self.run_cli([str(wiki)])
        self.assertEqual(code, 0, out)

        # The session notes are gone from the root, archived (never deleted).
        self.assertFalse((wiki / "memory-session-abc12345.md").exists())
        self.assertFalse((wiki / "memory-session-def67890.md").exists())
        self.assertTrue((wiki / "archive" / "memory-session-abc12345.md").exists())
        self.assertTrue((wiki / "archive" / "memory-session-def67890.md").exists())

        # Each session note (the operator's requests) became a memory row on
        # the way through the format-3 log, which format 4 then retires.
        self.assertFalse((wiki / "memory").exists())
        conn = db.connect(wiki)
        try:
            by_source = {r.source: r for r in db.rows(conn, "M")}
        finally:
            conn.close()
        rec1 = by_source["upgrade:memory-session-abc12345.md"]
        rec2 = by_source["upgrade:memory-session-def67890.md"]
        self.assertEqual(rec1.session, "abc12345")
        self.assertIn("git-flow", rec1.tags)
        self.assertNotIn("Captured at the end of session", rec1.summary)
        self.assertIn("Wire the new memlog store", rec1.summary)

        # The planted secret never reaches the database.
        self.assertNotIn("sk-ant", rec2.summary)
        self.assertNotIn("sk-ant", rec2.body)
        self.assertNotIn(b"sk-ant", db.db_path(wiki).read_bytes())

        # .gitattributes marks the database binary, not a union-merged log.
        attrs = (wiki / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn(upgrade.DB_GITATTRIBUTES_LINE, attrs)
        self.assertNotIn(memlog.GITATTRIBUTES_LINE, attrs)
        self.assertEqual(registry.read_format(wiki), registry.CURRENT_FORMAT)

    def test_second_upgrade_is_a_noop_and_check_then_passes(self):
        wiki = self._wiki_with_sessions()
        self.run_cli([str(wiki)])
        after_first = _snapshot(wiki)

        code, out = self.run_cli([str(wiki)])
        self.assertEqual(code, 0, out)
        self.assertEqual(_snapshot(wiki), after_first, "a second upgrade must be a no-op")

        code, out = self.run_cli([str(wiki), "--check"])
        self.assertEqual(code, 0, out)

    def test_unparseable_session_note_is_still_archived_and_reported(self):
        wiki = make_old_wiki(self.repo_wiki())
        p = wiki / "memory-session-weird001.md"
        p.write_text(
            "---\nid: memory-session-weird001\ntype: memory\n"
            'created: "2026-09-01T00:00:00Z"\nconsequence: 4\nlocus: output-meta\n---\n'
            "not the capture-hook shape at all\n",
            encoding="utf-8",
        )
        report = upgrade.upgrade_wiki(wiki)
        self.assertIn("memory-session-weird001.md", report.mechanical.get("memory", []))
        self.assertFalse(p.exists())
        self.assertTrue((wiki / "archive" / "memory-session-weird001.md").exists())
        conn = db.connect(wiki)
        try:
            self.assertFalse(any("weird001" in r.source for r in db.rows(conn)))
        finally:
            conn.close()


class TestStrayLogAfterFormat4(UpgradeTestCase):
    def test_a_log_written_by_an_older_capture_is_folded_into_rows(self):
        wiki = make_old_wiki(self.repo_wiki())
        upgrade.upgrade_wiki(wiki)
        keep = memlog.make_record(kind="preference", summary="Always pin the model in trials.",
                                  scope="repo", source="capture:request#abc")
        drop = memlog.make_record(kind="outcome", summary="Shipped the hook.", scope="repo",
                                  source="capture:report#abc")
        memlog.append(wiki, [keep, drop])
        self.assertEqual(upgrade.upgrade_wiki(wiki, check=True).status, "behind")
        upgrade.upgrade_wiki(wiki)
        self.assertFalse((wiki / "memory").exists())
        conn = db.connect(wiki)
        try:
            summaries = [r.summary for r in db.rows(conn, "M")]
        finally:
            conn.close()
        self.assertIn("Always pin the model in trials.", summaries)
        self.assertNotIn("Shipped the hook.", summaries)
        self.assertEqual(upgrade.upgrade_wiki(wiki, check=True).status, "current")


class TestMemlogJudgment(UpgradeTestCase):
    def test_malformed_memory_log_line_is_a_work_item(self):
        wiki = make_old_wiki(self.repo_wiki())
        rec = memlog.make_record(kind="fact", summary="A seed memory.", scope="repo")
        memlog.append(wiki, [rec])
        log = memlog.current_log(wiki)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        codes = self.codes(wiki)
        self.assertIn("MEMLOG", codes)
        self.assertIn(log.name, "".join(codes["MEMLOG"]))


class TestMemoryConversionGit(unittest.TestCase):
    """A real git checkout: the archive move is a ``git mv``, and two
    branches that each append to the same month file merge cleanly via the
    ``merge=union`` attribute (shapa-backend-spec.md)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.wiki = self.repo / ".shapa"
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", self.tmp / "config.json"),
            mock.patch.dict(os.environ, {registry.REGISTRY_ENV_VAR: str(self.tmp / "wikis.json")}),
        ]
        for p in self.patches:
            p.start()
        self.repo.mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(self.repo), capture_output=True,
                              text=True, timeout=30)

    def _commit(self, message: str) -> None:
        # --no-verify: this is a disposable fixture repo created purely to
        # exercise `git mv`/`git merge` semantics in isolation - not a real
        # commit to this (or any) project history, so the host's commit
        # hooks (e.g. an identity check meant for real commits) don't apply.
        self._git("add", "-A")
        r = self._git("commit", "-q", "--no-verify", "-m", message)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_archive_is_a_git_mv(self):
        make_old_wiki(self.wiki)
        _session_note(self.wiki, "abc12345", "A session worth remembering.")
        self._commit("initial wiki + session note")

        upgrade.upgrade_wiki(self.wiki)

        status = self._git("status", "--porcelain").stdout
        self.assertIn(".shapa/archive/memory-session-abc12345.md", status)
        # `git mv` stages the move directly as a rename (R), not an
        # untracked new file plus a deletion - proving the plain-move
        # fallback never ran inside this checkout.
        self.assertRegex(
            status,
            r"R\s+\.shapa/memory-session-abc12345\.md -> \.shapa/archive/memory-session-abc12345\.md",
        )

    def test_two_branches_appending_the_same_month_merge_cleanly(self):
        make_old_wiki(self.wiki)
        upgrade.upgrade_wiki(self.wiki)  # format 3: .gitattributes union line in place
        self._commit("wiki at format 3")
        main_branch = self._git("symbolic-ref", "--short", "HEAD").stdout.strip()

        now = datetime(2026, 9, 10, tzinfo=timezone.utc)

        self._git("checkout", "-q", "-b", "branch-a")
        rec_a = memlog.make_record(kind="decision", summary="Branch A's own memory.",
                                   source="test:a", scope="repo", created=memlog.now_iso(now))
        memlog.append(self.wiki, [rec_a], now=now)
        self._commit("branch a appends")

        self._git("checkout", "-q", main_branch)
        self._git("checkout", "-q", "-b", "branch-b")
        rec_b = memlog.make_record(kind="decision", summary="Branch B's own memory.",
                                   source="test:b", scope="repo", created=memlog.now_iso(now))
        memlog.append(self.wiki, [rec_b], now=now)
        self._commit("branch b appends")

        self._git("checkout", "-q", main_branch)
        r1 = self._git("merge", "--no-edit", "branch-a")
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        r2 = self._git("merge", "--no-edit", "branch-b")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

        merged = (self.wiki / "memory" / "2026-09.jsonl").read_text(encoding="utf-8")
        self.assertIn("Branch A's own memory.", merged)
        self.assertIn("Branch B's own memory.", merged)


# --- CLI: --import-memri (opt-in, never part of a plain upgrade) ----------------

class TestImportMemriCli(UpgradeTestCase):
    def _export(self, wiki: Path, rows) -> Path:
        path = wiki.parent / "export.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    def test_plain_upgrade_never_imports_memri(self):
        wiki = make_old_wiki(self.repo_wiki())
        self.run_cli([str(wiki)])
        self.assertFalse(memlog.has_log(wiki) and any(
            r.source.startswith("memri:") for r in memlog.read_log(wiki).records.values()
        ))

    def test_import_memri_writes_into_the_given_path(self):
        wiki = make_old_wiki(self.repo_wiki())
        export = self._export(wiki, [
            {"id": "r1", "type": "memory", "title": "Imported fact",
             "body": "A fact worth importing from the old memri store for good.",
             "created_at": "2026-01-01T00:00:00Z"},
        ])
        code, out = self.run_cli([str(wiki), "--import-memri", str(export)])
        self.assertEqual(code, 0, out)
        self.assertIn("records wrote: 1", out)
        view = memlog.read_log(wiki)
        self.assertEqual(len(view.records), 1)

    def test_import_memri_dry_run_writes_nothing(self):
        wiki = make_old_wiki(self.repo_wiki())
        export = self._export(wiki, [
            {"id": "r1", "type": "memory", "title": "Would be imported",
             "body": "This body would become a record without --dry-run.",
             "created_at": "2026-01-01T00:00:00Z"},
        ])
        code, out = self.run_cli([str(wiki), "--import-memri", str(export), "--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertIn("[dry-run]", out)
        self.assertFalse(memlog.has_log(wiki))

    def test_import_memri_rejects_all_with_a_clean_usage_error(self):
        with self.assertRaises(SystemExit) as exc:
            upgrade.main(["--all", "--import-memri", "whatever.json"])
        self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
