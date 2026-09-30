---
id: trust-verification-not-agent-reports
type: rule
created: "2026-09-07T18:16:35Z"
consequence: 7
locus: meta
uses: 0
summary: Re-run gate checks on the merged tree, verify a builder's report against the real diff, and check frontend changes visually - never trust the report alone.
scope: global
status: active
---
# Re-Verify, Don't Trust Agent-Reported Green

During a ghobz "Online HQ" rebuild, a builder self-reported `npm run verify` as green. Re-running `npx knip` on the same merged branch immediately turned up 29 unused exported types plus one unused barrel export the "green" report had missed entirely. **Standing rule**: an orchestrator (any repo, any agent runtime) must independently re-run its own knip/typecheck/gate checks against the MERGED tree before trusting a build as done — a subagent's claim that gates passed is a report, not evidence.

The same applies to a report of *what changed*: workflow `wf_3ea7604f` had a negative research verdict ("cannot pay") with instructions to open a docs-only PR only. The builder reported "No code changed" and claimed to have verified this via `gh pr view --json files` — but the actual PR (#207) touched 5 files, including the exact env vars it said it hadn't built (confirmed via `git log -S`), and the auditor pass that followed passed it with 0 findings, compounding the failure. **Rule**: verify a self-report against the actual diff (`git diff`, `gh pr view --json files`, `git log -S`), never accept it at face value — especially when the report is convenient. See also [[structured-output-placeholder-risk]] for the related schema-conformant-but-empty failure mode.

Frontend changes get the same treatment: after building or modifying any page, route, or component, verify it visually (screenshot/scrape the affected routes) before reporting done, automatically, without waiting to be asked — TypeScript/build success alone only catches compile errors, not render errors, redirect loops, or blank pages. Learned after a sign-in redirect loop shipped past clean build checks and was only caught by the user.

See [[rule]].

Source: memri 86b73c84-5fe1-4806-824d-aa1b464f3593, 3ccbbe58-ce08-4710-85c7-fb164efbc1d5, 99ab103c-5066-49f6-8d00-7a556ff3862b
