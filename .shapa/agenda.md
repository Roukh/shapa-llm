---
id: agenda
type: memory
created: "2026-09-30T06:00:00Z"
consequence: 8
locus: meta
summary: Top 3 fires for shapa-llm - upgrade every wiki to format 2, go live through roukh-llm's installer, prove the stranger install live.
scope: repo
status: active
---

# Agenda: top 3 fires

1. **Bring every wiki to format 2.** shapa 0.7.0 is installed, and `shapa upgrade --all --check` reports all 7 registered wikis as behind. lern and championmetalglass need only the mechanical pass. roukh-llm, open-trader and ghobz_projects need real archiving and merging: up to 68 arch refs and up to 1.2 MB live, and ghobz has 263 notes outside root/arch. Run the `shapa-upgrade` skill, one agent per wiki. The mechanism is in [[shapa-backend-spec]] §11.
2. **Go live in roukh-llm.** Its installer wires the hooks to 0.7.0: SessionStart runs `shapa bootstrap`, UserPromptSubmit runs `shapa fetch`, and Stop runs capture and maintain. It also installs the `shapa-upgrade` skill and repoints the global wiki. The memri hook that leaks notes across projects is retired at the same time (spec §7).
3. **An installer a stranger can run, verified live.** `bootstrap.sh` asks about the extras and installs the core only when there's no terminal. It wires the hooks and the skill, and ends on the upgrade check. It counts as done only after a live headless Claude Code session shows the merged context, not on unit tests alone.
