# shapa

**shapa** = **S**elf-**H**ealing **A**utonomous **P**ersistent **A**gent.

The operational memory of an LLM agent: a persistent markdown graph of notes about *how the agent works*, scored by how much each note matters and kept healthy by automatic maintenance. The core engine is pure Python 3.11+ standard library; an optional `sentence-transformers` install activates semantic embeddings for retrieval (otherwise BM25).

A note's value is its effect on the consuming agent's performance. shapa is **not** a topic knowledge base; it stores operational memory only.

---

## What shapa is

A memory made of plain markdown files. An agent writes to it at the end of a work session (the "always-meta" discipline). The fetch hook (`shapa/fetch.py`, wired by `install.sh` on `UserPromptSubmit`) reads the most relevant, highest-scored notes at the start of every prompt and calls `record_use` on each surfaced note.

| Part | What it is |
|------|-----------|
| **Wiki** (external) | The memory. A directory named `shapa/` you scaffold with `shapa init` — at a **repo root** (`<repo>/shapa/`, resolved automatically for any session inside that repo) or globally (`~/.shapa/memory`). Every file — the installed `arch/` templates and your operational notes alike — has the same frontmatter and links to others with `[[wikilinks]]`, so the whole thing is one graph you can browse in Obsidian. |
| **AGENTS.md** (installed into the wiki) | The rules. The authoritative schema and operating conventions, and the marker that makes a `shapa/` directory a wiki. Shipped with the tool and installed by `shapa init`. |
| **Engine** (`shapa/`) | The maintainer. A stdlib Python package that validates frontmatter, scores notes, retrieves relevant notes (fetch), captures new ones (capture), and self-heals (maintain: prune + auto-merge duplicates). Optional embeddings via `requirements.txt`. |

There is **one coherent file format** across the whole wiki — no separate "node" format.

---

## The MemRI framework

Every file is one of:

- **memory** — a record of what happened in a session.
- **rule** — a standing policy promoted from a memory when a pattern stabilizes.
- **issue** — a detected problem or gap; open until a resolving memory or rule is written.
- **reference** — design and spec material (the `arch/` docs).

---

## How connections work

Connections are the edges of the graph. They are Obsidian `[[wikilinks]]` written in the body — the same links Obsidian renders in its graph view. Two files are connected when one links the other. A file that links to nothing and that nothing links to is an **orphan**, and the heartbeat prunes it.

---

## Scoring

Every file carries a value score driven by its frontmatter:

```
score = locus_weight × (consequence / 10) × freshness × use_factor
```

- **consequence** (1–10) — how much agent performance would degrade without this note (author-set).
- **locus** — `output` (1.0), `output-meta` (1.5), or `meta` (2.0): what the note affects.
- **freshness** — decays since `last_used`; stability grows with consequence.
- **uses** — a mechanical counter bumped by `record_use` (never by an LLM); incremented by the fetch hook on every prompt a note is surfaced.

---

## Install

One line — installs the tool and wires the Claude Code hooks (no clone):

```
curl -fsSL https://raw.githubusercontent.com/Roukh/shapa-llm/main/bootstrap.sh | sh
```

It installs `shapa` (pipx > uv > pip --user), wires the fetch/capture/maintain
hooks, and scaffolds the default global wiki. `SHAPA_EMBEDDINGS=1` adds local
embeddings; `SHAPA_NO_HOOKS=1` installs the tool only. Or install by hand:

```
pipx install shapa                 # the tool (semantic retrieval via BM25)
pipx install "shapa[embeddings]"   # + local embeddings (sentence-transformers)
```

### Where memory lives

Your **memory is external to the tool** — never inside the installed package or
this repo. A wiki is a directory named `shapa/` holding an `AGENTS.md` marker.
Two ways to make one:

```
cd <your-repo> && shapa init       # a repo-root wiki: <your-repo>/shapa/
shapa init ~/.shapa/memory         # a global wiki (LLM rules, any session)
```

`shapa init [DIR]` creates the directory, installs `AGENTS.md` + the `arch/`
project templates, scaffolds an Obsidian vault, and records the path. A session
running **inside a repo that has a `shapa/` wiki uses it automatically** — the
hooks walk up from the cwd to the nearest `shapa/AGENTS.md` (like git finding
`.git`). Outside any such repo they fall back to `$SHAPA_MEMORY`, the recorded
path, or `~/.shapa/memory`. Open any wiki folder as an Obsidian vault to browse
the graph.

To wire the hooks into Claude Code from a clone instead of the one-liner, run
`./install.sh` (wires the hooks, installs the docs, registers the Obsidian
vault). Contributors can `git clone` and work from the repo.

## How to run

```
shapa init [DIR]                       # scaffold a wiki (default: ./shapa)
shapa where                            # print the resolved memory directory
shapa fetch --query "fix the git flow" # surface relevant memory (read path)
shapa heartbeat --dry-run              # preview orphan pruning
shapa maintain --dry-run               # preview merges/prunes (nothing changes)
shapa maintain --prune                 # prune orphans/stale + auto-merge duplicates
shapa maintain --resolve               # LLM-reconcile contradictions (claude CLI)
shapa score                            # rank notes by value
shapa validate                         # validate every note's frontmatter
```

Every command resolves the wiki the same way (local `shapa/` → `$SHAPA_MEMORY` → recorded path → `~/.shapa/memory`); pass a directory to override. From a clone without installing, use `python3 -m shapa <command>`.

## Repo layout (the tool)

```
shapa-llm/
  pyproject.toml         ← packaging (pipx/PyPI); `shapa` console command
  bootstrap.sh           ← curl|sh installer (installs the tool + wires hooks)
  install.sh             ← wires the Claude Code hooks + Obsidian vault (from a clone)
  shapa/                 ← Python engine (core is stdlib; embeddings optional)
    config.py            ← wiki resolution (local shapa/ · $SHAPA_MEMORY · pointer · default)
    cli.py               ← the unified `shapa` command (incl. `init`)
    frontmatter.py · nodes.py · heartbeat.py · validate.py · score.py
    fetch.py · capture.py · maintain.py · embed.py
    assets/              ← docs installed into every wiki by `shapa init`
      AGENTS.md          ← the schema and rules (also the wiki marker)
      arch/              ← generic project templates (PRD, architecture, system-design)
  docs/design/           ← shapa's OWN design docs (PRD, MACRO, memri-spec, hook-design)
  tests/                 ← regression suite
```

The repo contains **no user memory** — your notes live in the external `shapa/` wiki you scaffold with `shapa init`, so private notes are never inside this repo at all. There is nothing to gitignore and no commit guard to maintain: the tool ships only the engine, the `AGENTS.md` rules, and the generic `arch/` templates under `shapa/assets/`. shapa's own design docs live in `docs/design/`, not in the install payload.

---

## File format (quick reference)

```markdown
---
id: kebab-case-slug          # must equal the filename stem
type: memory | rule | issue | reference
created: "2026-06-30T12:00:00Z"
consequence: 7               # 1-10
locus: output | output-meta | meta
uses: 0
---
Free markdown. Headings, lists, emphasis are fine. Connect to other notes
with [[wikilinks]] in the body, e.g. its type [[rule]] and a peer [[memory-hygiene]].
```

See `AGENTS.md` (installed into your wiki by `shapa init`, source at `shapa/assets/AGENTS.md`) for the full schema.

---

## What is deferred

- **Semantic embeddings** for retrieval. `fetch` uses BM25 (zero-dep) today; true embedding-similarity would need a model dependency or an API.
- **Contradiction resolution.** `maintain` *detects* similar/contradiction-candidate pairs; *reconciling* them (merge a duplicate, resolve a conflict) is a semantic judgement left to the maintaining agent.

---

## Standalone

shapa is a standalone, self-contained repo. The engine has zero external dependencies; your memory lives in an external wiki directory as plain markdown (connect it with `shapa init`). Clone the tool, point it at your own memory, and run.
