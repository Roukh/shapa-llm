---
id: schema-v2-bad-status
type: memory
created: "2026-09-30T00:00:00Z"
consequence: 5
locus: output
uses: 0
summary: Status is set to a value outside the valid enum.
scope: repo
status: retired
---
'retired' is not one of active/superseded/draft, so S04 should fire.
