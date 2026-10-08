# Security policy

## Reporting a vulnerability

Report a security issue privately through GitHub Security Advisories:

<https://github.com/Roukh/shapa-llm/security/advisories/new>

Do not open a public issue for a suspected vulnerability. Include steps to
reproduce it, the shapa version, and your Python version and OS. Expect an
initial response within a few days.

## Scope

shapa runs locally, writes to one SQLite file per wiki, and calls no hosted
service of its own. The areas most worth a security report are:

- **Hook and MCP commands.** Anything shapa's installed hooks or its MCP
  server (`shapa mcp`) run, construct, or pass to a shell or to a harness
  (Claude Code, Codex, OpenCode) on your behalf.
- **Config file edits.** `install.sh` and the bootstrap script edit a
  harness's own config (for example Claude Code's `settings.json` or
  Codex's `config.toml`) to register hooks and the MCP server. A report
  that one of these edits is unsafe, overwrites unrelated settings, or
  escapes its own marked block is in scope.
- **Secret redaction.** `shapa/redact.py` runs before any memory is
  written. A pattern it should catch and does not is a valid report.
- **Git hook installation.** `shapa init` writes into a repo's own git
  hooks, and is meant to detect and defer to husky, lefthook, the
  pre-commit framework, and a shared `core.hooksPath` rather than writing
  into them. A case where it writes somewhere it should not is in scope.

Out of scope: the content of your own wiki (`shapa.db`) once it is on disk;
that file has the same protection as any other file in your repo or home
directory.

## Supported versions

Only the latest released version gets security fixes. There is no separate
long-term-support branch.
