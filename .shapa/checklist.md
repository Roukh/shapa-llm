---
id: checklist
type: memory
created: "2026-10-02T20:00:00Z"
consequence: 9
locus: meta
summary: shapa-llm's one work checklist - capture-junk fix shipped and installed, full suite green, every wiki current, stranger install proven live.
scope: repo
status: active
tags: [checklist, agenda]
---
# Checklist (shapa-llm)

This wiki's single work queue; [[agenda]] names its top 3 by ID. Item line:
`- [ ] **ID** what done looks like — verify: \`command\``. Verify commands run
from this repo's root and must exit 0; `verify: manual` items are operator
calls. Claim 2-3 per session with the checklist hook's `claim ID` command
(see the global wiki's checklist); the Stop hook verifies and closes them.
Never tick a box by hand.

## capture and convention fixes · 2026-10-02

- [x] **SL1** Capture drops junk: subagent reports by default, filler and split ledger fragments, progress chatter, pasted content; report text is never a preference; "all sessions" preferences route global — verify: `.venv/bin/python -m pytest -q tests/test_capture.py` — done 2026-10-02 9b195457
- [x] **SL2** Full suite green, socket and installer tests included (the hook runs it outside the sandbox) — verify: `.venv/bin/python -m pytest -q` — done 2026-10-02 9b195457
- [ ] **SL3** The live CLI carries the fix (operator: `uv tool install --force` from this checkout, then `shapa upgrade --all`) — verify: `diff -q shapa/capture.py /home/roukh/.local/share/uv/tools/shapa/lib/python3.12/site-packages/shapa/capture.py && diff -q shapa/validate.py /home/roukh/.local/share/uv/tools/shapa/lib/python3.12/site-packages/shapa/validate.py`

## [[agenda]] · fires

- [ ] **SL4** Every registered wiki is current with the installed shapa — verify: `shapa upgrade --all --check`
- [ ] **SL5** A stranger's install via `bootstrap.sh` is proven in a live headless Claude Code session that shows the merged context, not on unit tests alone — verify: manual
