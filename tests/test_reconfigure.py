"""The format-4 restructure directive: which wikis get it, where it shows, how
a session claims it and how the dispatched agent ends it."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from shapa import bootstrap, cli, config, db, reconfigure, registry, upgrade
from shapa.config import WikiRoot


def run_upgrade(*argv) -> tuple[int, str]:
    out = io.StringIO()
    code = 0
    with redirect_stdout(out):
        try:
            upgrade.main(list(argv))
        except SystemExit as e:
            code = e.code or 0
    return code, out.getvalue()


class ReconfigureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wiki = self.tmp / "repo" / ".shapa"
        self.wiki.mkdir(parents=True)
        (self.wiki / "AGENTS.md").write_text("# marker\n", encoding="utf-8")
        pointer = self.tmp / "config.json"
        pointer.write_text(json.dumps({"memory": str(self.tmp / "global")}), encoding="utf-8")
        self.patches = [
            mock.patch.object(config, "CONFIG_FILE", pointer),
            mock.patch.dict(os.environ, {registry.REGISTRY_ENV_VAR: str(self.tmp / "wikis.json")}),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop(config.ENV_VAR, None)
        os.environ.pop(reconfigure.MODEL_ENV, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def old_wiki(self):
        """A format-3 wiki with a rule note and an open checklist item."""
        registry.write_format(self.wiki, 3)
        (self.wiki / "deploy-rule.md").write_text(
            "---\nid: deploy-rule\ntype: rule\nsummary: Deploy from tags only\n---\nTags only.\n",
            encoding="utf-8")
        (self.wiki / "checklist.md").write_text("## Ship\n- [ ] **A1** first item\n", encoding="utf-8")

    def context(self):
        return bootstrap.build_context(roots=[WikiRoot(path=self.wiki, kind="repo", repo="repo")])


class TestWhoNeedsIt(ReconfigureCase):
    def test_an_older_format_needs_it_and_the_directive_leads_the_bootstrap(self):
        self.old_wiki()
        self.assertIn("format 3, shapa is at 4", reconfigure.reasons(self.wiki)[0])
        text = self.context()
        self.assertTrue(text.startswith("<shapa-action>"), text[:80])
        self.assertIn("ACTION FIRST", text)
        self.assertIn('model "opus"', text)
        self.assertIn(f"shapa upgrade --reconfigure-prompt {self.wiki.resolve()}", text)
        self.assertLess(text.index("</shapa-action>"), text.index("<shapa-memory>"))

    def test_a_mechanical_upgrade_leaves_it_pending(self):
        self.old_wiki()
        upgrade.upgrade_wiki(self.wiki)
        self.assertEqual(registry.read_format(self.wiki), 4)
        self.assertEqual(reconfigure.reasons(self.wiki), ["migrated mechanically, not yet restructured"])

    def test_a_wiki_born_in_format_4_never_needs_it(self):
        registry.write_format(self.wiki)
        db.connect(self.wiki, create=True).close()
        self.assertEqual(reconfigure.reasons(self.wiki), [])
        self.assertNotIn("<shapa-action>", self.context())

    def test_the_model_is_configurable(self):
        self.old_wiki()
        with mock.patch.dict(os.environ, {reconfigure.MODEL_ENV: "sonnet"}):
            self.assertIn('model "sonnet"', self.context())


class TestLifecycle(ReconfigureCase):
    def test_prompt_claims_then_mark_ends_the_directive(self):
        self.old_wiki()
        upgrade.upgrade_wiki(self.wiki)
        code, prompt = run_upgrade("--reconfigure-prompt", str(self.wiki))
        self.assertEqual(code, 0)
        self.assertIn(str(self.wiki.resolve()), prompt)
        self.assertIn("--mark-reconfigured", prompt)
        self.assertTrue(reconfigure.claimed(self.wiki))
        text = self.context()
        self.assertIn("in progress", text)
        self.assertNotIn("ACTION FIRST", text)

        code, _ = run_upgrade("--mark-reconfigured", str(self.wiki))
        self.assertEqual(code, 0)
        self.assertEqual(reconfigure.reasons(self.wiki), [])
        self.assertIsNone(reconfigure.claimed(self.wiki))
        self.assertNotIn("<shapa-action>", self.context())
        # A later upgrade pass never re-opens a finished restructure.
        upgrade.upgrade_wiki(self.wiki)
        self.assertEqual(reconfigure.reasons(self.wiki), [])

    def test_mark_is_refused_while_format_3_files_remain(self):
        self.old_wiki()
        code, _ = run_upgrade("--mark-reconfigured", str(self.wiki))
        self.assertEqual(code, 1)

    def test_an_expired_claim_brings_the_directive_back(self):
        self.old_wiki()
        reconfigure.claim(self.wiki, "s1")
        old = (self.wiki / reconfigure.CLAIM_FILENAME)
        stamp = old.stat().st_mtime - (reconfigure.CLAIM_TTL_HOURS + 1) * 3600
        os.utime(old, (stamp, stamp))
        self.assertIn("ACTION FIRST", self.context())

    def test_init_never_installs_the_prompt_into_a_wiki(self):
        target = self.tmp / "fresh"
        target.mkdir()
        installed = cli._install_docs(target)
        self.assertFalse(any(p.startswith("prompts") for p in installed))
        self.assertFalse((target / "prompts").exists())

    def test_the_claim_file_is_gitignored(self):
        self.assertIn(reconfigure.CLAIM_FILENAME, upgrade.CACHE_IGNORES)


if __name__ == "__main__":
    unittest.main()
