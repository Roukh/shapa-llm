---
id: schema-v2-mismatched-scope
type: memory
created: "2026-09-30T00:00:00Z"
consequence: 5
locus: output
uses: 0
summary: Declares scope global while physically living in a repo fixture dir.
scope: global
status: active
---
This file is not under global_root(), so F06 should fire.
