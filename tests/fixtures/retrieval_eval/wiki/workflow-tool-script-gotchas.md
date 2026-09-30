---
id: workflow-tool-script-gotchas
type: rule
created: "2026-07-12T01:13:42.287736+00:00"
summary: "Workflow-tool scripts can't call Date()/Math.random or trust args passed in, and the tool itself may not be loaded in a given session."
scope: global
status: active
consequence: 6
locus: meta
uses: 0
---
# Workflow Tool Script Gotchas

Three harness-level limits hit when a skill's `.workflow.js` script runs under the Workflow tool:

- **No `Date.now()` / `new Date()` inside workflow scripts.** The harness forbids both (breaks resume/determinism). A wave-pipeline run (`wf_c24a5c72-9d1`, uilib Wave-0, 8 tracks, PRs #50-57) had 6/8 tracks throw at the merge stage because `buildMergePrompt` stamped `operator-authorized <date>` — every affected track resolved to `null`, and the final aggregation crashed reading `p.mergeReady` off `null`. Fix: pass any timestamp in via `args`/`config`, never compute it inside the script.
- **`args` passed to the Workflow tool don't reliably reach the script.** Invoking with `args` as a JSON object (e.g. `{today, oos_cutoff}`) didn't interpolate — inside the script `args.today` evaluated to `undefined`, so every downstream agent prompt got the literal string `undefined` (observed 2026-07-17, open-trader analysis run; resume diagnostics showed `args` as a JSON-encoded string rather than parsed). Workaround: hardcode parameters directly into the script string instead of routing them through `args`; check whether this still reproduces before relying on parameterized scripts for anything reproducibility-sensitive.
- **The Workflow tool runtime is not guaranteed to be present in a given session.** `fresh-eyes-review`'s bundled `review-prs.workflow.js` expects Workflow-tool globals (`log`/`phase`/`pipeline`/`agent`); a session without that tool loaded can't run it. Fallback: dispatch a general-purpose agent with an equivalent no-build-context review prompt instead — this produced real, verified findings in practice.

See also [[git-safety-discipline]] (merge-stage hazards on the same kind of run) and [[wave-pipeline-multiworktree-safety-lessons]] (more Workflow-sandbox sharp edges, incl. a related `Date()` crash).

Source: memri 9d37771d-4068-4067-b37b-7d4952299040, c8629b8d-eea3-4edc-9324-9b206f7c9540, ea02d1b8-745d-4937-808f-4284683323a9
