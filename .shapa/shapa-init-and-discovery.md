---
id: shapa-init-and-discovery
type: rule
created: "2026-07-10T00:00:00Z"
consequence: 7
locus: output-meta
scope: repo
summary: Explicit `shapa init DIR` repoints the recorded default wiki; bare init and discovery don't. Bring a marker-less .shapa in by adding AGENTS.md, then upgrade.
---

Rules for creating, connecting and finding wikis without breaking the operator's global wiki. Peers: [[shapa-llm-identity]], [[shapa-engine-safety]].

**Marker and discovery.** A wiki is any directory holding the `AGENTS.md` marker. Discovery walks up from the cwd and checks `.shapa/` before the legacy `shapa/` at every ancestor. The tool's own package directory `shapa/` is never mistaken for a wiki: its marker sits at `shapa/assets/AGENTS.md`, not `shapa/AGENTS.md`.

**Pointer policy** (`shapa/cli.py` `_init`). An explicit `shapa init DIR` calls `config.set_memory_dir`, making DIR the recorded default for every session outside a repo. Running it on a repo wiki silently repoints the global wiki. A bare `shapa init` relies on discovery and never writes the pointer.

**Legacy wikis.** `init` refuses a non-empty directory that lacks `AGENTS.md`, and `shapa upgrade PATH` refuses one too (exit 2). To bring such a `.shapa` in without touching the pointer, copy `shapa/assets/AGENTS.md` into it and run `shapa upgrade <dir>`. That refreshes `AGENTS.md`/`placement.md`, writes the cache `.gitignore`, derives `id`/`scope`, and writes `.shapa-format` once the work list is empty. This repo's `.shapa` was converted this way on 2026-10-01. `init`, `bootstrap`, `fetch` and `upgrade` also register each wiki in `~/.shapa/wikis.json`.

**Packaging.** `shapa init` installs whatever the wheel ships under `shapa/assets/`. On 2026-07-10 a stale `build/` directory re-shipped design docs that had been moved out of the assets, so they would have landed in user wikis. Delete `build/` before verifying a wheel's contents.

[[rule]]
