"""install.sh / bootstrap.sh side of the upgrade mechanism (spec §11): the
shapa-upgrade skill is installed per harness, and every install/update ends
with ``shapa upgrade --all --check``.

Same hermetic harness as tests/test_installer_mcp.py (isolated $HOME, stub
binaries only); the `shapa` stub here also answers ``upgrade`` and logs
every call so the tests can see exactly what the installer ran.
"""

from __future__ import annotations

import unittest

from tests.test_installer_mcp import BootstrapShTestCase, InstallShTestCase, _write_stub

SHAPA_UPGRADE_STUB = """#!/usr/bin/env bash
echo "shapa $*" >> "$SHAPA_STUB_SHAPA_LOG"
if [ "$1" = "init" ] && [ -n "$2" ]; then
  mkdir -p "$2"
  printf '%s\\n' '---' 'id: AGENTS' 'type: reference' 'created: "2026-01-01T00:00:00Z"' \\
    'consequence: 8' 'locus: output' '---' 'stub' > "$2/AGENTS.md"
fi
if [ "$1" = "upgrade" ] && [ "$2" = "--print-skill" ]; then
  printf '%s\\n' '---' 'name: shapa-upgrade' 'description: stub' '---' 'stub skill body'
  exit 0
fi
if [ "$1" = "upgrade" ]; then
  echo "behind     /stub/wiki  (format 1)"
  exit "${SHAPA_STUB_UPGRADE_RC:-1}"
fi
exit 0
"""


class UpgradeInstallerMixin:
    def setUp(self):
        super().setUp()
        _write_stub(self.bin / "shapa", SHAPA_UPGRADE_STUB)
        self.shapa_log = self.tmp / "shapa.log"

    def _env(self, extra: dict | None = None) -> dict:
        env = super()._env(extra)
        env.setdefault("SHAPA_STUB_SHAPA_LOG", str(self.shapa_log))
        return env

    def shapa_calls(self) -> list[str]:
        if not self.shapa_log.exists():
            return []
        return self.shapa_log.read_text(encoding="utf-8").splitlines()

    @property
    def claude_skill(self):
        return self.settings.parent / "skills" / "shapa-upgrade" / "SKILL.md"

    @property
    def codex_skill(self):
        return self.home / ".agents" / "skills" / "shapa-upgrade" / "SKILL.md"

    @property
    def opencode_skill(self):
        return self.home / ".config" / "opencode" / "skills" / "shapa-upgrade" / "SKILL.md"


class TestInstallSkillAndCheck(UpgradeInstallerMixin, InstallShTestCase):
    def test_default_install_writes_the_claude_skill_and_ends_with_the_check(self):
        result = self.run_install(["--no-embeddings", "--no-mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("name: shapa-upgrade", self.claude_skill.read_text(encoding="utf-8"))
        self.assertFalse(self.codex_skill.exists())
        self.assertEqual(self.shapa_calls().count("shapa upgrade --all --check"), 1)
        self.assertEqual(self.shapa_calls()[-1], "shapa upgrade --all --check")
        self.assertIn("/stub/wiki", result.stdout)
        self.assertIn("need the shapa-upgrade skill", result.stdout)

    def test_check_failure_never_fails_the_install(self):
        result = self.run_install(["--no-embeddings", "--no-mcp"], {"SHAPA_STUB_UPGRADE_RC": "2"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_current_says_so(self):
        result = self.run_install(["--no-embeddings", "--no-mcp"], {"SHAPA_STUB_UPGRADE_RC": "0"})
        self.assertIn("Every known wiki is current.", result.stdout)

    def test_harness_all_installs_where_each_harness_is_present(self):
        self.add_stub_harness("codex")
        result = self.run_install(["--no-embeddings", "--no-mcp", "--harness", "all"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.claude_skill.is_file())
        self.assertTrue(self.codex_skill.is_file())
        self.assertFalse(self.opencode_skill.exists(), "opencode binary absent - never touched")

        self.add_stub_harness("opencode")
        self.run_install(["--no-embeddings", "--no-mcp", "--harness", "opencode"])
        self.assertIn("stub skill body", self.opencode_skill.read_text(encoding="utf-8"))

    def test_dry_run_previews_without_writing_or_checking(self):
        result = self.run_install(["--dry-run", "--no-embeddings", "--no-mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.claude_skill.exists())
        self.assertIn("would install the shapa-upgrade skill", result.stdout)
        self.assertIn("would run: shapa upgrade --all --check", result.stdout)
        self.assertFalse(any(c.startswith("shapa upgrade") for c in self.shapa_calls()))

    def test_uninstall_removes_the_skill_and_runs_no_check(self):
        self.run_install(["--no-embeddings", "--no-mcp"])
        self.assertTrue(self.claude_skill.is_file())
        result = self.run_install(["--uninstall", "--no-embeddings", "--no-mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.claude_skill.exists())
        self.assertFalse(self.claude_skill.parent.exists())
        self.assertEqual(self.shapa_calls().count("shapa upgrade --all --check"), 1)


class TestBootstrapShCheck(UpgradeInstallerMixin, BootstrapShTestCase):
    def test_update_path_checks_once_via_install_sh(self):
        result = self.run_bootstrap(["--no-mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.shapa_calls().count("shapa upgrade --all --check"), 1)

    def test_no_hooks_path_still_checks(self):
        result = self.run_bootstrap([], {"SHAPA_NO_HOOKS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.shapa_calls().count("shapa upgrade --all --check"), 1)
        self.assertIn("need the shapa-upgrade skill", result.stdout)


if __name__ == "__main__":
    unittest.main()
