---
id: schema-v2-dangling-supersedes
type: memory
created: "2026-09-30T00:00:00Z"
consequence: 5
locus: output
uses: 0
summary: Supersedes an id that does not exist anywhere in the known scan.
scope: repo
status: active
supersedes: this-id-does-not-exist-anywhere
---
F08 should fire only when the caller supplies a known_ids set.
