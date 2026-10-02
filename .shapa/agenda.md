---
id: agenda
type: memory
created: "2026-09-30T06:00:00Z"
consequence: 8
locus: meta
summary: Agenda centers on checklist.md - SL3 ship the capture fix to the live CLI, SL4 every registered wiki current, SL5 prove the stranger install live.
scope: repo
status: active
---

# Agenda: top 3 fires

[[checklist]] is this repo's work queue; this names its top 3 by item ID. Going live through
roukh-llm's installer is done (hooks and the `shapa-upgrade` skill verified 2026-10-02).

1. **SL3 Ship the 2026-10-02 fixes to the live CLI.** Capture-junk filtering and the
   `checklist.md` convention are in this checkout but not the installed tool; reinstall, then
   `shapa upgrade --all` refreshes every wiki's `AGENTS.md`.
2. **SL4 Every registered wiki current.** `shapa upgrade --all --check` still lists wikis behind,
   some with real archiving and merging work. Run the `shapa-upgrade` skill, one agent per wiki;
   the mechanism is in [[shapa-backend-spec]] §11.
3. **SL5 An installer a stranger can run, verified live.** `bootstrap.sh` asks about the extras
   and installs the core only when there's no terminal. It counts as done only after a live
   headless Claude Code session shows the merged context, not on unit tests alone.
