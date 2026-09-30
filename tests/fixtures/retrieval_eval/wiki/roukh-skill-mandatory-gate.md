---
id: roukh-skill-mandatory-gate
type: rule
created: "2026-05-25T23:19:35.046164+00:00"
consequence: 8
locus: output-meta
uses: 0
summary: Auto-invoke /roukh-skill before any code work in any project; check the existing quality-skill landscape before building a new one.
scope: global
status: active
---
# roukh-skill Is Mandatory Before Code Work

Auto-invoke `/roukh-skill` on the first substantive prompt of every session, and again before any code change, refactor, build, schema change, route change, or architectural decision — regardless of folder/project, without waiting to be asked. Do not write code until it returns clean. Exceptions: pure documentation typo/copy fixes, read-only research/analysis with no code output, casual one-liner questions.

roukh-skill's own protocol decides depth (full audit vs quick check) and enforces sub-rules like [[ui-library-registry-method]] and [[trust-verification-not-agent-reports]].

Before building any new quality/behavioral skill, check the existing landscape first to avoid duplication: **caveman** (communication brevity only), **review** (formal two-axis Standards+Spec diff review), **verification-before-completion** (evidence-before-claims gate), **do-less-better** (consolidates minimalism/quality-checks/brevity into one skill — its unique value is Gate 1, pre-work minimalism gating over-engineering before code is written; Gate 2 deliberately delegates to `/simplify`, `verification-before-completion`, and `review` rather than re-implementing them). `/simplify` is a BUILT-IN Claude Code command, not an editable skill — don't try to edit it. The `roukh-skill` router itself stays a dispatcher (grill-with-docs, to-prd, to-issues, triage, tdd, review) rather than a monolith — splitting it further had low marginal return; higher-leverage work improves the skills it dispatches to instead. Authoring convention: description under 400 chars, `SKILL.md` under 400 lines; register a new skill via `scripts/sync-skills.sh`.

Source: memri 276556f9-b125-4a65-8a82-df925706d696, d11a1ce0-05ed-46ce-90fa-7e2f05bf46e0, f02c18a5-80b2-42d5-a156-cd606a0dcce7, 978e68a0-4029-46d9-a6d0-c4fa58dfed62, 1e046e66-8ccf-4502-a317-4b515db80f56
