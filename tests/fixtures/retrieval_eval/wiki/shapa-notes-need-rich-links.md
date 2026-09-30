---
id: shapa-notes-need-rich-links
type: rule
created: "2026-07-12T22:53:24.845558+00:00"
consequence: 5
locus: meta
uses: 0
summary: A .shapa note ending in a single Type: [[memory]] link is spoke-to-hub; link to real peer notes/arch docs a reader would actually want to jump to.
scope: global
status: active
---
# .shapa Notes Need Rich Links

.shapa notes that only end with a single `Type: [[memory]]`-style link are a spoke-to-hub pattern that defeats the point of a wiki graph. Caught 2026-07-12 across 49 auto-generated notes, all wired to one generic keyword instead of to each other. What's actually wanted: real links between architecture docs (arch/PRD/system-design), other memories, rules, and sibling notes that share a dependency or attribute — not a placeholder hub link. When writing or consolidating .shapa notes (including curation passes like this one), add `[[wikilinks]]` to the specific related slugs a reader would actually want to jump to.

Source: memri cbcdc400-0545-439a-bfd7-a42114048b5d
