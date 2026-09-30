"""Tests for install.sh / bootstrap.sh's MCP harness wiring (installer-
hardening GAP B): ``--mcp``/``--no-mcp``, ``--harness claude|codex|opencode|
all``, idempotency, ``--dry-run`` previews, ``--uninstall`` reversal, and
"nothing is touched unless the harness binary is installed".

These run the real ``install.sh``/``bootstrap.sh`` via subprocess against a
FULLY ISOLATED environment - an explicit ``env`` dict (never
``os.environ.copy()``) with a temp ``HOME`` and a ``PATH`` containing only
stub ``claude``/``codex``/``opencode``/``shapa``/``pipx``/``curl`` binaries
plus the minimal system dirs. ``XDG_CONFIG_HOME``/``CODEX_HOME`` are
deliberately left OUT of that dict (not merely unset) so a leaked ambient
value can never redirect a write to the real ``~/.config/opencode`` or
``~/.codex`` - the real ``~/.claude``/``~/.claude.json`` are likewise never
reachable since ``claude`` only ever resolves to the stub on this PATH.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
BOOTSTRAP_SH = REPO_ROOT / "bootstrap.sh"

# A stub `claude` that implements just enough of `claude mcp get/add/remove`
# to exercise install.sh's idempotency check and add/remove calls, without
# ever touching a real Claude Code config. State is a marker file under the
# fake $HOME (never the real one - see SHAPA_STUB_HOME below).
CLAUDE_STUB = """#!/usr/bin/env bash
echo "claude $*" >> "$SHAPA_STUB_LOG"
mark="$SHAPA_STUB_HOME/claude-mcp-registered"
if [ "$1" = "mcp" ] && [ "$2" = "get" ]; then
  [ -f "$mark" ] && exit 0 || exit 1
fi
if [ "$1" = "mcp" ] && [ "$2" = "add" ]; then
  touch "$mark"; exit 0
fi
if [ "$1" = "mcp" ] && [ "$2" = "remove" ]; then
  rm -f "$mark"; exit 0
fi
exit 0
"""

# codex/opencode are never actually invoked by install.sh (it edits their
# config files directly) - these stubs only need to exist so `command -v`
# finds them, proving "the binary is installed".
NOOP_STUB = "#!/usr/bin/env bash\nexit 0\n"

# A minimal `shapa` so resolve_shapa()'s `command -v shapa` succeeds
# immediately - no repo .venv build, no real package, fully hermetic.
SHAPA_STUB = """#!/usr/bin/env bash
if [ "$1" = "init" ] && [ -n "$2" ]; then
  mkdir -p "$2"
  printf '%s\\n' \
    '---' 'id: AGENTS' 'type: reference' 'created: "2026-01-01T00:00:00Z"' \
    'consequence: 8' 'locus: output' 'uses: 0' '---' 'stub' > "$2/AGENTS.md"
fi
exit 0
"""

PIPX_STUB = "#!/usr/bin/env bash\nexit 0\n"


def _write_stub(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class InstallShTestCase(unittest.TestCase):
    """Fixture: an isolated $HOME + a bin dir seeded with `shapa`/`pipx`
    stubs; each test opts into whichever of claude/codex/opencode it needs
    present via :meth:`add_stub_harness`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shapa-installer-test-"))
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        _write_stub(self.bin / "shapa", SHAPA_STUB)
        _write_stub(self.bin / "pipx", PIPX_STUB)
        self.settings = self.tmp / "settings.json"
        self.stub_log = self.tmp / "claude.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_stub_harness(self, *names: str) -> None:
        for name in names:
            _write_stub(self.bin / name, CLAUDE_STUB if name == "claude" else NOOP_STUB)

    def _env(self, extra: dict | None = None) -> dict:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "SHAPA_STUB_LOG": str(self.stub_log),
            "SHAPA_STUB_HOME": str(self.home),
            "SHAPA_MEMORY": str(self.tmp / "wiki"),
        }
        # Deliberately no XDG_CONFIG_HOME/CODEX_HOME here: unset in the child
        # means install.sh's own `${VAR:-$HOME/...}` fallbacks kick in and
        # resolve under the fake $HOME, never a real ambient value.
        if extra:
            env.update(extra)
        return env

    def run_install(self, args: list[str], extra_env: dict | None = None) -> subprocess.CompletedProcess:
        cmd = ["bash", str(INSTALL_SH), "--settings", str(self.settings), *args]
        return subprocess.run(
            cmd,
            env=self._env(extra_env),
            cwd=str(self.tmp),
            input="",  # never a TTY: deterministic across CI/dev machines
            capture_output=True,
            text=True,
            timeout=30,
        )

    @property
    def claude_marker(self) -> Path:
        return self.home / "claude-mcp-registered"

    @property
    def codex_config(self) -> Path:
        return self.home / ".codex" / "config.toml"

    @property
    def opencode_config(self) -> Path:
        return self.home / ".config" / "opencode" / "opencode.json"

    def settings_hooks(self) -> dict:
        if not self.settings.exists():
            return {}
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        return data.get("hooks", {})


class TestHarnessValidation(InstallShTestCase):
    def test_unknown_harness_value_errors_and_touches_nothing(self):
        result = self.run_install(["--harness", "bogus", "--no-embeddings"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown --harness value", result.stderr)
        self.assertFalse(self.settings.exists())


class TestDryRun(InstallShTestCase):
    def test_dry_run_all_harnesses_previews_without_writing_mcp_config(self):
        self.add_stub_harness("claude", "codex", "opencode")
        result = self.run_install(["--dry-run", "--no-embeddings", "--harness", "all"])
        self.assertEqual(result.returncode, 0, result.stderr)
        # Settings may be created empty by the pre-existing write_settings
        # dry-run path, but must carry no shapa hook command.
        self.assertNotIn("shapa", self.settings_hooks() or {})
        for ev, entries in self.settings_hooks().items():
            self.assertEqual(entries, [], f"{ev} should be empty in a dry run")
        # No MCP config was actually written anywhere.
        self.assertFalse(self.claude_marker.exists())
        self.assertFalse(self.codex_config.exists())
        self.assertFalse(self.opencode_config.exists())
        # The idempotency check (`claude mcp get`, read-only) may run even in
        # a dry run - it never mutates anything - but `add`/`remove` must not.
        if self.stub_log.exists():
            log = self.stub_log.read_text(encoding="utf-8")
            self.assertNotIn("mcp add", log)
            self.assertNotIn("mcp remove", log)
        # But the plan is shown for every in-scope, installed harness.
        self.assertIn("would run: claude mcp add shapa --scope user", result.stdout)
        self.assertIn("would append to", result.stdout)
        self.assertIn("[mcp_servers.shapa]", result.stdout)
        self.assertIn("would set .mcp.shapa", result.stdout)

    def test_dry_run_respects_explicit_no_mcp(self):
        self.add_stub_harness("claude")
        result = self.run_install(["--dry-run", "--no-embeddings", "--no-mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("claude mcp add", result.stdout)


class TestMcpAutoNoTty(InstallShTestCase):
    def test_no_flag_no_tty_skips_mcp_but_still_wires_claude_hooks(self):
        self.add_stub_harness("claude")
        result = self.run_install(["--no-embeddings"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.stub_log.exists(), "claude stub must never run with no TTY and no --mcp")
        self.assertFalse(self.claude_marker.exists())
        combined = result.stdout + result.stderr
        self.assertIn("no TTY", combined)
        self.assertIn("--mcp", combined)  # tells the operator the exact follow-up
        # Hooks still get wired - MCP wiring and hook wiring are independent.
        hooks = self.settings_hooks()
        self.assertTrue(any(hooks.get(ev) for ev in ("SessionStart", "UserPromptSubmit")))


class TestMcpRegistrationAndIdempotency(InstallShTestCase):
    def test_mcp_registers_all_three_harnesses_and_is_idempotent(self):
        self.add_stub_harness("claude", "codex", "opencode")

        first = self.run_install(["--no-embeddings", "--harness", "all", "--mcp"])
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(self.claude_marker.exists())
        self.assertIn("[mcp_servers.shapa]", self.codex_config.read_text(encoding="utf-8"))
        opencode_data = json.loads(self.opencode_config.read_text(encoding="utf-8"))
        self.assertIn("shapa", opencode_data.get("mcp", {}))
        self.assertEqual(opencode_data["mcp"]["shapa"]["type"], "local")
        self.assertEqual(opencode_data["mcp"]["shapa"]["command"][-1], "mcp")

        second = self.run_install(["--no-embeddings", "--harness", "all", "--mcp"])
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already registered", second.stdout)
        self.assertEqual(second.stdout.count("already registered"), 3)

        # Exactly one add call ever reached the claude stub; the rerun only
        # issued the idempotency-check `get` (never a second `add`).
        log_lines = self.stub_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(1 for l in log_lines if l.startswith("claude mcp add")), 1)
        self.assertEqual(sum(1 for l in log_lines if l.startswith("claude mcp get")), 2)

        # The codex/opencode configs are byte-identical after the no-op rerun.
        codex_before = self.codex_config.read_text(encoding="utf-8")
        opencode_before = self.opencode_config.read_text(encoding="utf-8")
        self.run_install(["--no-embeddings", "--harness", "all", "--mcp"])
        self.assertEqual(self.codex_config.read_text(encoding="utf-8"), codex_before)
        self.assertEqual(self.opencode_config.read_text(encoding="utf-8"), opencode_before)

    def test_claude_mcp_add_argv_is_the_shapa_mcp_invocation(self):
        self.add_stub_harness("claude")
        self.run_install(["--no-embeddings", "--mcp"])
        log = self.stub_log.read_text(encoding="utf-8")
        self.assertIn("mcp add shapa --scope user -- shapa mcp", log)


class TestHarnessScoping(InstallShTestCase):
    def test_harness_codex_skips_claude_hooks_and_other_harnesses(self):
        self.add_stub_harness("claude", "codex", "opencode")
        result = self.run_install(["--no-embeddings", "--harness", "codex", "--mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.settings.exists(), "claude hooks must not be wired for --harness codex")
        self.assertFalse(self.stub_log.exists(), "the claude binary must never be invoked for --harness codex")
        self.assertIn("[mcp_servers.shapa]", self.codex_config.read_text(encoding="utf-8"))
        self.assertFalse(self.opencode_config.exists(), "opencode is out of scope for --harness codex")

    def test_harness_default_is_claude_only(self):
        self.add_stub_harness("claude", "codex", "opencode")
        result = self.run_install(["--no-embeddings", "--mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.claude_marker.exists())
        self.assertFalse(self.codex_config.exists())
        self.assertFalse(self.opencode_config.exists())


class TestBinaryAbsence(InstallShTestCase):
    def test_uninstalled_harness_binaries_are_never_touched(self):
        self.add_stub_harness("claude")  # codex/opencode deliberately absent
        result = self.run_install(["--no-embeddings", "--harness", "all", "--mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.claude_marker.exists())
        self.assertFalse(self.codex_config.exists())
        self.assertFalse(self.opencode_config.exists())
        self.assertFalse((self.home / ".codex").exists(), "no directory should be created for an absent harness")
        self.assertFalse((self.home / ".config").exists())
        self.assertIn("codex CLI not found on PATH", result.stdout)
        self.assertIn("opencode CLI not found on PATH", result.stdout)


class TestUninstall(InstallShTestCase):
    def test_uninstall_reverses_hooks_and_every_harnesss_mcp_registration(self):
        self.add_stub_harness("claude", "codex", "opencode")
        self.run_install(["--no-embeddings", "--harness", "all", "--mcp"])
        self.assertTrue(self.claude_marker.exists())

        result = self.run_install(["--uninstall", "--harness", "all"])
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertFalse(self.claude_marker.exists())
        self.assertNotIn("mcp_servers.shapa", self.codex_config.read_text(encoding="utf-8"))
        opencode_data = json.loads(self.opencode_config.read_text(encoding="utf-8"))
        self.assertNotIn("shapa", opencode_data.get("mcp", {}))
        for ev, entries in self.settings_hooks().items():
            self.assertEqual(entries, [])

    def test_uninstall_with_explicit_no_mcp_leaves_mcp_registration_alone(self):
        self.add_stub_harness("claude")
        self.run_install(["--no-embeddings", "--mcp"])
        self.assertTrue(self.claude_marker.exists())

        self.run_install(["--uninstall", "--no-mcp"])
        self.assertTrue(self.claude_marker.exists(), "--no-mcp on --uninstall must skip MCP cleanup")


# --- bootstrap.sh: flag forwarding only (never a real network install) -----

CURL_STUB_TEMPLATE = """#!/usr/bin/env bash
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
if [ -n "$out" ]; then
  cp "%s" "$out"
fi
exit 0
"""


class BootstrapShTestCase(InstallShTestCase):
    """Extends the same fixture with a stub `curl` that copies the real,
    local install.sh instead of downloading anything - bootstrap.sh's own
    flag-forwarding logic then runs against the actual install.sh under
    test, with the same fully-isolated env (no network, ever)."""

    def setUp(self):
        super().setUp()
        _write_stub(self.bin / "curl", CURL_STUB_TEMPLATE % str(INSTALL_SH))

    def run_bootstrap(self, args: list[str], extra_env: dict | None = None) -> subprocess.CompletedProcess:
        cmd = ["sh", str(BOOTSTRAP_SH), *args]
        return subprocess.run(
            cmd,
            env=self._env(extra_env),
            cwd=str(self.tmp),
            input="",
            capture_output=True,
            text=True,
            timeout=30,
        )


class TestBootstrapForwarding(BootstrapShTestCase):
    def test_mcp_and_harness_flags_forward_to_install_sh(self):
        self.add_stub_harness("codex")
        result = self.run_bootstrap(["--mcp", "--harness", "codex"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[mcp_servers.shapa]", self.codex_config.read_text(encoding="utf-8"))
        self.assertFalse(self.claude_marker.exists())

    def test_with_mcp_extra_implies_wiring_by_default(self):
        self.add_stub_harness("claude")
        result = self.run_bootstrap(["--with-mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.claude_marker.exists(), "--with-mcp should also wire MCP by default")

    def test_explicit_no_mcp_overrides_the_with_mcp_default(self):
        self.add_stub_harness("claude")
        result = self.run_bootstrap(["--with-mcp", "--no-mcp"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.claude_marker.exists(), "an explicit --no-mcp must win over --with-mcp's default")

    def test_full_implies_mcp_wiring(self):
        self.add_stub_harness("claude")
        result = self.run_bootstrap(["--full"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.claude_marker.exists())

    def test_unknown_argument_errors(self):
        result = self.run_bootstrap(["--bogus-flag"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown argument", result.stderr)


if __name__ == "__main__":
    unittest.main()
