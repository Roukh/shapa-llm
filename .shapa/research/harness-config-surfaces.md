---
id: harness-config-surfaces
type: reference
created: "2026-10-06T21:50:00Z"
consequence: 8
locus: output
scope: repo
summary: Config paths, hook events, context injection, MCP shape and packaging for 21 coding-agent harnesses (2026-10), tiered for a `shapa connect` command.
---

# Harness config surfaces (research 2026-10-06)

Official docs, fetched 2026-10. Input for `shapa connect` in [[public-release-plan]]. UNVERIFIED marks facts with no confirmed official page. Paths churn (see gotcha 8): an adapter should re-check them, not trust this table forever.

## Tiers

| Tier | Harnesses | What shapa can do |
|---|---|---|
| Full hooks with context injection | Claude Code, Codex CLI, Qwen Code, Factory Droid, GitHub Copilot CLI, Cursor (IDE and `cursor-agent`), Gemini CLI (experimental, double opt-in) | same as Claude Code today: start overview, per-prompt recall, stop capture, correction capture, ledger hooks. Most share Claude Code's `hookSpecificOutput.additionalContext` envelope |
| Hooks through code plugins | OpenCode (JS/TS plugin), Amp (TS plugin), Goose (plugin `hooks.json`) | generate a small plugin file that shells out to `shapa` |
| Narrow hooks | Cline (8 types, one context string), Kiro (agent hooks; output appended), Crush (PreToolUse only), Windsurf (exit code only, no context injection) | partial: some events, or MCP plus instructions |
| MCP plus instruction file only | Copilot in VS Code, Zed, Continue, Augment/Auggie, Kilo Code, Warp, Roo Code (archived 2026-05-15) | MCP `search/get/save` plus a pointer in the instruction file |
| None | Aider (no hooks, no confirmed MCP) | instruction file via `read:` only |

## Matrix

| Harness | Detect | User config | Project config | Format | Hooks and injection | MCP | Instructions | Packaging |
|---|---|---|---|---|---|---|---|---|
| Claude Code | `claude`, `~/.claude` (`CLAUDE_CONFIG_DIR`) | `settings.json` | `.claude/settings.json`, `.local.json` | JSON | SessionStart, UserPromptSubmit, Pre/PostToolUse, Stop, SubagentStop, PreCompact, SessionEnd; `hookSpecificOutput.additionalContext` | `claude mcp add -s user`; `.mcp.json` | CLAUDE.md (AGENTS.md fallback) | plugins: `.claude-plugin/plugin.json` bundles hooks, MCP, skills, agents; marketplaces |
| Codex CLI | `codex`, `~/.codex` (`CODEX_HOME`) | `config.toml` | `.codex/config.toml` (trusted projects only) | TOML | Claude-style event set and envelope; hooks hash-trust-gated, reviewed in `/hooks` | `[mcp_servers.x]`; `codex mcp add` | AGENTS.md, AGENTS.override.md | plugin-bundled hooks |
| Qwen Code | `qwen`, `~/.qwen` | `settings.json` | `.qwen/settings.json` | JSON | ~20 events, Claude-style envelope | `qwen mcp add -s user` | QWEN.md, AGENTS.md | `contextFileNames` |
| Factory Droid | `droid`, `~/.factory` | `config.json`, `hooks.json` | `.factory/hooks.json`, `.factory/mcp.json` | JSON | 9 Claude-named events, same envelope | `droid mcp add` | AGENTS.md | skills (path UNVERIFIED), custom droids |
| Copilot CLI | `copilot`, `~/.copilot` (`COPILOT_HOME`) | `settings.json`, `mcp-config.json` | `.github/copilot/settings.json` | JSON | sessionStart/End, userPromptSubmitted, pre/postToolUse, preCompact, agentStop, subagent*; `additionalContext` | `mcpServers`; `/mcp add` | `.github/copilot-instructions.md`, AGENTS.md | `~/.copilot/skills`, `hooks/`, plugins |
| Cursor IDE + `cursor-agent` | `cursor-agent`, `~/.cursor` | `~/.cursor/hooks.json`, `~/.cursor/mcp.json` | `.cursor/hooks.json`, `.cursor/mcp.json` | JSON | sessionStart/End, pre/postToolUse, subagent*, stop, preCompact; output `additional_context` | `mcpServers` | `.cursor/rules/*.mdc`, AGENTS.md | none |
| Gemini CLI | `gemini`, `~/.gemini` | `settings.json` | `.gemini/settings.json` | JSON | needs `tools.enableHooks` and `hooks.enabled`; BeforeTool/AfterTool/BeforeAgent/AfterAgent/SessionStart/SessionEnd; stdout must be JSON only | `gemini mcp add --scope user` | GEMINI.md (`context.fileName`) | extensions: `gemini-extension.json` bundles MCP, commands, hooks, skills |
| OpenCode | `opencode`, `~/.config/opencode` (`OPENCODE_CONFIG`) | `opencode.json` | `opencode.json`, `.opencode/` | JSON/JSONC | JS/TS plugin (`session.*`, `chat.message`, `tool.execute.*`); plugin dir name UNVERIFIED | `mcp{}` `type: local` | AGENTS.md | plugin files, commands |
| Amp | `amp`, `~/.config/amp` | `settings.json(c)` | `.amp/settings.json` | JSONC | TS plugin `amp.on(...)` | `amp mcp add` | AGENTS.md (not for subagents) | plugin dirs with skills |
| Goose | `goose`, `~/.config/goose` | `config.yaml` | `.goosehints`, AGENTS.md | YAML | `.agents/plugins/<x>/hooks/hooks.json`; `{"decision","context"}` | `extensions:` map (field names UNVERIFIED) | AGENTS.md, .goosehints | recipes |
| Cline | ext, `~/.cline` (`CLINE_DATA_DIR`) | `cline_mcp_settings.json` | `.clinerules/` | JSON | TaskStart/Resume/Cancel/Complete, Pre/PostToolUse, UserPromptSubmit, PreCompact; `{"contextModification"}`; registration file UNVERIFIED | `mcpServers` | `.clinerules/` | none |
| Kiro | `kiro-cli`, `~/.kiro` | `settings/mcp.json` | `.kiro/hooks/*.json`, `.kiro/steering/` | JSON | PromptSubmit, AgentStop, SessionStart/End, Pre/PostToolUse, file events; stdout appended | `mcpServers` | steering files | specs |
| Crush | `crush`, `~/.config/crush` | `crushrc` (executable shell DSL) or legacy `crush.json` | `.crushrc` | shell/JSON | PreToolUse only | `crush mcp add` | CRUSH.md, AGENTS.md | none |
| Windsurf | `windsurf`, `~/.codeium/windsurf` | `hooks.json`, `mcp_config.json` | `.devin/hooks.json` | JSON | 12 events, exit code only | `mcpServers`, user scope only | `.windsurf/rules/`, AGENTS.md | workflows |
| Copilot in VS Code | extension | - | `.vscode/mcp.json` (key `servers`) | JSON | none | `code --add-mcp` | copilot-instructions, AGENTS.md, CLAUDE.md (additive) | agent files |
| Zed | `zed`, `~/.config/zed` | `settings.json` (allows comments) | `.zed/settings.json` | JSON with comments | none | `context_servers` | AGENTS.md and others (order UNVERIFIED) | extensions |
| Continue | `cn`, `~/.continue` | `config.yaml` | `.continuerc.json`, `.continue/mcpServers/*.yaml` | YAML + JSON | none | drop-in YAML file | `.continue/rules/` | hub blocks |
| Augment/Auggie | `auggie`, `~/.augment` | `settings.json` (JSONC) | `.augment/settings.json` | JSONC | none | `auggie mcp add` | CLAUDE.md, AGENTS.md | rules |
| Kilo Code | ext; new CLI `kilo.jsonc` | `mcp_settings.json` | `.kilocode/mcp.json` | JSON / JSONC (two generations) | none | two incompatible shapes | `.kilocode/rules/` | modes |
| Warp | app, `~/.warp` | `.mcp.json` | `.warp/.mcp.json` | JSON | none | flat map | AGENTS.md / WARP.md (upper case) | none |
| Aider | `aider` | `~/.aider.conf.yml` | `.aider.conf.yml` | YAML | none (lint/test commands only) | not found | `read:` key | none |

## Gotchas for an automatic editor

1. Files with comments (Zed, Augment, Amp, OpenCode `.jsonc`, Kilo CLI): a plain `json` rewrite deletes them. Edit with a comment-preserving pass or print the snippet.
2. Files the app rewrites itself (`~/.claude.json`); Crush's `crushrc` is executable shell, so use `crush mcp add` instead of editing it.
3. Two config generations at once (Kilo Code): detect which before writing.
4. Trust gates: Codex ignores project config in untrusted repos and asks to review new hooks; VS Code needs Workspace Trust for MCP; Claude Code plugins show a review. `connect` should say a review prompt is coming.
5. Precedence differs (Cursor Team > Project > User; Cline workspace wins; Copilot in VS Code additive with no order).
6. Windows paths differ per harness (`%APPDATA%`, `%ProgramData%`, `%USERPROFILE%\.copilot`).
7. Warp needs upper-case `AGENTS.md`.
8. Churn in 2026: Roo Code archived, Windsurf docs moved under Devin, Goose moved foundations, Factory and Kilo changed domains or formats.
9. Continue mixes YAML (global) and JSON (project) in one harness.
10. Copilot CLI and Copilot in VS Code share `~/.copilot` but only the CLI has hooks.

## Design consequence

The Claude-style hook envelope is a de facto standard across seven harnesses, so one hook contract plus per-harness event-name and path mapping covers the full tier. Plugin-tier harnesses need a generated shim file. The rest get MCP plus an instruction pointer.

Related: [[installer]] · [[cli-and-hooks]] · [[install-and-cli-ux]]
