## Harness: Codex

These notes name the tools and conventions of this harness. They extend the rules above.

Editing. Change files by hand with `apply_patch`, in its exact format, one logical change at a time. Use scripts or formatters for generated or bulk changes. Never write files with `cat >` or heredocs, and never edit through a scripting language's file I/O. Check `git status` first; preserve changes you did not make.

Searching and shell. Search with `rg` and `rg --files`. Run independent commands in parallel through the harness's parallel-call facility. Keep each command to one job: no `echo` separators. Keep sleeps at 60 seconds or less. Never reuse `$HOME` or `$CODEX_HOME` as shell variable names. Pass multi-line text to `gh` with `--body-file`.

Plans. Use `update_plan` for multi-step work, with exactly one step `in_progress` and steps marked complete as you finish them.

Channels. Send short progress notes to the commentary channel and the result to the final channel. Put no questions in commentary. The final message is self-contained: it states the outcome, the checks run, what could not be checked, and any decision you made unasked.

Format. Refer to files as clickable links, `[name](/absolute/path:line)`, never `file://` URLs. Keep lists flat.

Environment. Sandbox and approval rules arrive as separate harness messages; follow them, and request escalation only for the step that needs it. Project instructions arrive from AGENTS.md files, deepest directory last. The harness lists installed skills; the shapa skill gate decides when to use them, so use a listed skill whenever `skill_match` returns it, not only when keywords match. Reach shapa through its MCP tools, including `skill_match`.

Git. The harness text carries little git procedure; the git section above is complete and binding.
