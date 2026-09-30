---
id: finish-current-pipeline-step-not-next
type: rule
created: "2026-08-12T20:47:25.602827+00:00"
consequence: 5
locus: meta
uses: 0
summary: When a pipeline names an operator-run step, finish the current step's own deliverable - don't self-chain into the next step past it.
scope: global
status: active
---
# Finish The Current Pipeline Step, Not The Next One

During a job-build-front run, an agent finished a design kit (step 3) then immediately scaffolded a full Next.js app from its own design output — before the operator had run the intended operator-run step (Claude Design, step 4), which existed specifically to convert that design output 1:1 into the app. The operator caught it mid-run and killed the workflow rather than let a divergent scaffold compound.

**Rule**: when a pipeline names an operator-run step, "continue" means finish the CURRENT step's own deliverable — never start the next step by building from the agent's own prior output when a human or tool step is supposed to sit between them. That an agent can technically produce the next step's output anyway is not license to skip the step; the operator-run step exists precisely to keep the two aligned, and self-chaining past it silently drops that check. When a pipeline's next step is ambiguous or unowned, stop and ask rather than assume the agent should absorb it.

Source: memri e4551e5b-c99a-42c4-9c03-6ecc3017ba62
