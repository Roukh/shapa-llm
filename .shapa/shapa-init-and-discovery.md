---
id: shapa-init-and-discovery
type: rule
created: "2026-07-10T00:00:00Z"
consequence: 7
locus: output-meta
scope: repo
summary: Only `shapa init --global` repoints the global wiki. `shapa init DIR` adopts a marker-less notes folder untouched; `shapa upgrade DIR` migrates it.
---

Rules for creating, connecting and finding wikis without breaking the operator's global wiki. Peers: [[shapa-llm-identity]], [[shapa-engine-safety]].

**Marker and discovery.** A wiki is any directory holding the `AGENTS.md` marker. Discovery walks up from the cwd and checks `.shapa/` before the legacy `shapa/` at every ancestor. The tool's own package directory `shapa/` is never mistaken for a wiki: its marker sits at `shapa/assets/AGENTS.md`, not `shapa/AGENTS.md`.

**Pointer policy** (`shapa/cli.py` `_init`). Only `shapa init --global [DIR]` calls `config.set_memory_dir`, making DIR (default `~/.shapa/memory`) the global wiki for every session outside a repo; `install.sh` passes it. Plain `shapa init` and `shapa init DIR` never write `~/.shapa/config.json`. Up to 0.7.0 an explicit DIR did, so scaffolding a repo wiki silently repointed the global one.

**Legacy wikis.** A named `shapa init DIR` adopts a non-empty folder that lacks `AGENTS.md`: it adds the missing scaffold files and leaves every note byte-identical, with no format marker. `shapa upgrade DIR` then refreshes `AGENTS.md`/`placement.md`, writes the cache `.gitignore`, strips counters, derives `id`/`scope`, and writes `.shapa-format` once the work list is empty. A bare `shapa init` still refuses a non-wiki `./shapa` (package-name collision), and `shapa upgrade PATH` refuses a folder without the marker (exit 2). `init`, `bootstrap`, `fetch` and `upgrade` register each wiki in `~/.shapa/wikis.json`.

**Packaging.** `shapa init` installs whatever the wheel ships under `shapa/assets/`. On 2026-07-10 a stale `build/` directory re-shipped design docs that had been moved out of the assets, so they would have landed in user wikis. Delete `build/` before verifying a wheel's contents.

[[rule]]
