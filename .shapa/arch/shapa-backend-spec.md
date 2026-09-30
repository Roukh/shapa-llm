---
id: shapa-backend-spec
type: reference
created: "2026-09-30T02:00:00Z"
consequence: 9
locus: output-meta
uses: 0
---

Final architecture for shapa's multi-root memory backend: global+repo dual-wiki loading, semantic search/RAG, and context-optimized note structure, so `pip install shapa` fully operates for any user on any harness. Synthesized from a 3-design tournament (winner: design 0, total 6.6) plus grafts from designs 1/2 and fixes for every fatal flaw the judges found.

## 1. Answer to the operator's question today

**No.** A session today does not reliably load the correct global `.shapa` notes plus the current repo's own notes, and nothing keeps fetching more along the way, anywhere. Verdict from the empirical probe (live commands run against real repos, this session):

| Mechanism | Global+repo merge? | Cross-project leakage? | Live/wired today? | Empirical result |
|---|---|---|---|---|
| OLD hook (`roukh-llm/scripts/runtime/memory.py bootstrap`, memri-backed) | N/A (DB query) | **YES — severe** | Live now | Identical `open-trader`-only and `roukh-llm`-only notes injected verbatim into shapa-llm, personal-web, roukh-brain sessions |
| NEW roukh-llm prototype (`.worktrees/shapa-memory/scripts/runtime/memory.py bootstrap`) | Yes, by design | No — architecturally isolated | **Not live** (settings.json hook points at the OLD file, not this one) | Correct wiki resolution, but flat 5000-char budget is entirely consumed by ~11 generic global notes before any repo-specific note appears — 0% repo-note representation |
| shapa's own `fetch` (`UserPromptSubmit`) | **No** — single-root `discover()`, local wiki always wins, zero global fallback | None observed (most repos have no wiki reachable) | **Not wired anywhere** — no `~/.claude/settings.json` exists, no repo has the hook installed | shapa-llm/personal-web: 0 notes surfaced for all 8 prompts (resolves to `~/.shapa/memory`, which doesn't exist). Other repos: BM25-only (no `sentence-transformers` installed), 2/8 slots always the same fixed anchor notes regardless of query |

Root causes, in the actual code (`shapa-llm/.worktrees/shapa-backend` @ `11d01a7`):
- `shapa/config.py::memory_dir()`/`discover()`/`resolve()` return exactly **one** path. Every consumer (`fetch.py`, `capture.py`, `maintain.py`, `heartbeat.py`, `validate.py`) inherits single-root resolution — a repo-local wiki *shadows* global entirely; `tests/test_discover.py` locks this in as current intended behavior.
- There is **no `SessionStart` hook** in shapa at all. `install.sh` wires only `UserPromptSubmit`/`Stop`/`SubagentStop`.
- "Semantic search" is aspirational until `sentence-transformers` is actually installed (`embed.available()` is `False` by default); retrieval silently falls back to BM25.
- The dual-load capability (global + repo) that *would* answer the operator's question exists only as a hand-rolled, 4-repo-hardcoded prototype outside shapa, in `roukh-llm/.worktrees/shapa-memory/scripts/runtime/memory.py` — unreachable via any live hook today.

## 2. Target architecture

```
                shapa/store.py — one sqlite file per wiki root (<root>/.shapa-index.db)
                notes table + FTS5 virtual table (when available) + JSON-sidecar vectors
                              ▲                          ▲
                              │ read (merged across roots)│ write (single, gated root)
        ┌─────────────────────┴──────┐          ┌──────────┴─────────────┐
        │ config.wiki_roots(start)    │          │ capture.py / shapa save │
        │ → [repo?, external?, global]│          │ --scope required        │
        └─────────────────────────────┘          └─────────────────────────┘
                │            │            │
   ┌────────────┴──┐  ┌──────┴───────┐  ┌─┴──────────────────────────┐
   │ Claude Code    │  │ shapa CLI     │  │ MCP stdio server (shapa mcp)│
   │ hooks:         │  │ (manual /     │  │ search / get / save /       │
   │ SessionStart → │  │  scripted)    │  │ placement — vendor-neutral   │
   │  bootstrap     │  └──────────────┘  │ surface for Codex/OpenCode/  │
   │ UserPromptSubmit│                    │ anything that speaks MCP    │
   │  → fetch        │                    └──────────────────────────────┘
   │ Stop/SubagentStop → capture, maintain --prune
   └─────────────────┘
```

Reads always fan out across every wiki root in scope (§4). Writes stay single-target and explicitly scope-gated — automatic multi-root merge must never silently decide where a *new* note lands. Hooks remain the always-on Claude-Code-specific path; MCP is the one on-demand, harness-agnostic path every consumer (including Codex/OpenCode) can reach.

## 3. Install experience

```
pip install shapa                 # core, pure stdlib, BM25-only — unchanged default
pip install shapa[semantic]       # + model2vec embeddings (renames today's [embeddings]
                                   #   sentence-transformers extra; old name kept as a
                                   #   deprecated alias for one release)
pip install shapa[mcp]             # + official `mcp` SDK for the stdio server; falls back
                                   #   to a stdlib-only JSON-RPC shim when absent
```

`bootstrap.sh` (the curl one-liner, the one true install path most users hit) installs `shapa[semantic,mcp]` **by default** — not bare `shapa` — because a stranger who runs the one-liner and gets BM25-only silently fails the "ships with a bit of a backend" ask. It also unconditionally runs `shapa init` for the **global** default root (`~/.shapa/memory`) even when cwd has its own repo-local wiki, closing the observed "shapa-llm/personal-web resolve to `~/.shapa/memory`, which doesn't exist on disk" failure. README states plainly: `pip install shapa` alone (not via `bootstrap.sh`) is keyword-only; `[semantic]` is what turns on real embeddings.

`install.sh` gains:
- A fourth hook pair: `SessionStart → shapa bootstrap`.
- `--mcp` (default on for the curl path, opt-in for a bare `pip install` + manual `install.sh`) writes a project-scoped `.mcp.json` registering `shapa mcp`.
- `--harness claude|codex|opencode|all` (default `claude`) routes hook/MCP registration to the right config surface (§7).
- `--no-embeddings` / `--no-mcp` opt back out; existing `--uninstall`/idempotent `jq`-guard behavior unchanged.

## 4. Wiki resolution + loading tiers + per-prompt fetch

### 4.1 Multi-root resolution (the core fix)

New, additive API in `shapa/config.py` (existing `discover()`/`memory_dir()`/`resolve()` untouched — single-root callers, `init`, `where` keep working unchanged):

```python
@dataclass(frozen=True)
class WikiRoot:
    path: Path
    kind: Literal["repo", "external", "global"]
    repo: str | None = None   # set for "repo" and "external"

def global_root() -> Path:
    """$SHAPA_MEMORY > the `shapa init` pointer > ~/.shapa/memory. cwd-independent."""

def wiki_roots(start: Path | None = None) -> list[WikiRoot]:
    """Every wiki a READ should search, most-specific first:
      1. $SHAPA_MEMORY override -> single root (back-compat escape hatch; used by
         --root/CI, never by the hooks/MCP path).
      2. Repo-local wiki via config.discover(start), if found and its resolved path
         differs from global_root() -> kind="repo".
      3. Else, if start is inside a git checkout with no wiki of its own AND
         <global_root>/external/<repo_name>/ exists -> kind="external".
      4. Always append kind="global" — UNLESS step 2's root IS global_root()
         (dedup: a repo whose own wiki *is* the global wiki gets one entry, not two).
    Global is never dropped by a repo-local wiki. This is the direct, tested fix
    for 'session loads global+repo, not just the nearer one wins'."""
```

Writes stay narrow: `capture.py`/`shapa save` take a required `--scope {global,repo,external}` (no default that silently guesses); `shapa/assets/placement.md` (new, generic — no hardcoded repo allowlist) ships the decision rule ("would this still be true in a different repo tomorrow? yes → global, else → repo/external"), installed by `shapa init` alongside `AGENTS.md`.

**Fatal-flaw fix — cross-root ID collisions.** Design 0 left uniqueness "by convention" with a silent keep-higher-scored/warn-to-stderr policy — the judges correctly flagged this as the one correctness property the whole merge depends on, weakening as more independently authored wikis accumulate. Fixed here: `validate.py` gains **F09** (duplicate `id` across two roots in the same `wiki_roots()` result) as a hard *error*, checked by a new `shapa validate --all-roots`, and `fetch.py`'s merge refuses silent override — a collision surfaces to the agent as an explicit note in the fetch output ("id `X` exists in both `repo` and `global` — ambiguous, showing both"), not a coin-flip pick.

**Fatal-flaw fix — anchor/budget starvation.** Design 0's per-root anchor cap (fixed "2 global/1 repo/1 external") was itself flagged as an unvalidated constant that reproduces the starvation bug at a different granularity, untested past 2 roots. Fixed here: the per-root reservation is **proportional with a floor**, not a fixed table — each root gets `max(floor_chars, budget // len(roots))` of the total character budget before cross-root relevance fills the remainder, and `locus: meta` anchors are capped at **2 per root**, never 2 total. `tests/test_fetch_multiroot.py` runs this against 2, 3, and 5+ roots (including a growing `external/` bucket) to assert no root's share collapses to zero as root count grows.

**Fatal-flaw fix — confidence floor.** Both `fetch.select()` (Tier 2) and `shapa search` (Tier 3) always emitted exactly `k` results even at zero relevance (empirically observed: an unrelated query against roukh-brain's wiki returned confident-looking but wrong notes). Fixed here: results below a `MIN_RELEVANCE` threshold (tunable, default such that pure-anchor value alone doesn't count as "relevant") are dropped from the *relevance-ranked* fill — only the unconditional `locus: meta` anchors remain, and the fetch output explicitly says `<!-- no query-relevant notes found -->` when that happens, rather than padding with low-confidence filler.

### 4.2 Loading tiers

- **Tier 0 — schema authority** (static, loaded once): the wiki's own `AGENTS.md`, read by `shapa init`'s installed copy / the harness's native `AGENTS.md` ingestion. Never re-fetched per turn.
- **Tier 1 — SessionStart, metadata-only** (new `shapa/bootstrap.py`, wired as the `SessionStart` hook): for every root from `wiki_roots()`, load only `id/type/summary/tags/consequence/locus` — never bodies. Print every `locus: meta` note's `summary` unconditionally (max 2/root), then the rest ranked by `score.score_meta()`, truncated to a ~1800-token budget (~4 chars/token heuristic, no tokenizer dependency). Never blocks: any exception → `""`, exit 0, same contract as `fetch.py`.
- **Tier 2 — per-prompt, on demand** (`fetch.py::select()`, extended): iterates `wiki_roots()` instead of `config.resolve()`; relevance = Reciprocal Rank Fusion (§5) over BM25 + vector rank lists, gated by the confidence floor above; snippet-capped (500 chars/note) and total-budget-capped (4000 chars), per-root reserved as above.
- **Tier 3 — explicit, agent-invoked** (`shapa search "<query>"`, documented in `AGENTS.md` §8 as a named affordance, not a side door — plus the MCP `search`/`get` tools, §7): full bodies, no snippet cut, for re-querying mid-turn or following a `[[wikilink]]` a Tier-2 snippet truncated.

## 5. Index/backend + perf budgets

`shapa/store.py`, one sqlite file per wiki root (`<root>/.shapa-index.db`), never a hard dependency — every consumer falls back to the current full-directory scan + hand-rolled BM25 (unchanged, zero new dependency) if the index is missing, stale, or corrupt.

- **FTS5 availability probe = the `CREATE VIRTUAL TABLE ... USING fts5(...)` call itself**, not `PRAGMA compile_options` (design 1/2's approach) — the pragma is unreliable across CPython builds; attempting the real table creation and catching the failure is the honest probe. False → fall back to the existing hand-rolled `_bm25_scores()`.
- Vector side stays the existing JSON-sidecar cosine cache (`.shapa-vectors.json`), just fed by `model2vec` instead of `sentence-transformers` — `sqlite-vec` is explicitly **not** adopted (it needs `load_extension`, exactly as fragile as FTS5, doubly so for zero benefit at this corpus size).
- `embed.py`: swap backend to `model2vec`/`potion-base-8M` (MIT, numpy-only, no model forward pass, ~94.7% of MiniLM's MTEB at a fraction of the install/latency cost) behind the **identical** `available()`/`embed_one()`/`note_vectors()`/`cosine()` contract — zero call-site changes in `fetch.py`/`maintain.py`.
- **Ranking**: `shapa/rank.py::rrf()` — Reciprocal Rank Fusion (`score(d) = Σ 1/(k+rank_i(d))`, k=60) replaces the current `if embed.available(): cosine else: bm25` either/or branch, so both vote whenever both exist and degrade gracefully to BM25-only otherwise. This directly fixes the live 3-test failure in `test_maintain.py`: `MERGE_THRESHOLD=0.9` was calibrated only against Jaccard; RRF's rank-position semantics are scale-invariant across backends, so the merge decision stops depending on which similarity metric happened to be active.
- Reranking (cross-encoder) is deliberately **not** in the hot path (`UserPromptSubmit`/Tier 2) — 100–2000ms for modest gains blows the budget below. Reserved for Tier 3 only, and only if the operator later wants it.
- **Daemon** (optional, latency only, correctness unaffected either way): `shapa serve`, one Unix domain socket per wiki root (mode `0600`, owner-checked), holding the loaded model + open sqlite connection warm. `fetch`/`bootstrap`/`mcp` try the socket first (~0.25s connect timeout), fall back to the in-process cold path on any failure. **Fatal-flaw fix (design 1's staleness gap):** every request against the warm daemon re-checks each candidate note's `(mtime_ns, size)` signature before trusting cached index rows — a cheap stat, not a re-embed — so a `capture`/`save` mid-session is reflected on the *next* request rather than served stale. `install.sh` autostarts it lazily on first `fetch`/`bootstrap`; absence never breaks correctness, only latency.

Perf budgets (soft regression guards, machine-dependent, not hard CI gates):

| Path | Budget | Measured by |
|---|---|---|
| `shapa bootstrap` (SessionStart) | p50 < 200ms, hard cap 2s | `tests/test_perf.py` — synthetic 500-note fixture |
| `shapa fetch` (UserPromptSubmit) | p50 < 200ms warm, < 1.5s cold | same fixture, daemon on/off |
| `reindex()` on an unchanged wiki | < 50ms (mtime short-circuit) | `test_store.py::test_reindex_noop_is_cheap` |
| `reindex()` incremental (N changed notes) | linear in N, ~5ms/note (model2vec, no GPU) | `test_store.py::test_reindex_incremental_only_touches_changed` |
| MCP tool round-trip | p50 < 300ms | `test_mcp.py` |

Empirical baseline observed this session (single-root, no daemon, ~85 notes): 0.06–0.3s per `shapa fetch` — today's corpus has latency headroom; the daemon buys margin as corpora grow toward thousands of notes, not for today's size.

## 6. Note schema v2 + exact rule text for AGENTS.md/maintain

Additive frontmatter (backward-compatible; existing notes validate with **warnings**, not errors, for one release, promoted after a one-time backfill):

```yaml
summary: "One line, <=160 chars, no newline"   # the ONLY thing Tier 1 loads
scope: global | repo | external                 # must match physical bucket
applies_to: roukh-llm                            # required iff scope: external
tags: [git, worktree, safety]                    # optional keyword boost
status: active | superseded | draft              # default active
supersedes: old-note-id                          # directed retirement edge
```

`validate.py` new codes:

| code | check | severity |
|---|---|---|
| F04 | `summary` present, ≤160 chars, single line | error (memory/rule/issue), warning (reference) |
| F05 | `scope` present, valid enum | warning this release → error next |
| F06 | `scope` matches physical containing bucket | auto-fixable by `maintain --backfill` |
| F07 | body length within target (150–300 words memory/rule/issue; 2000-word hard cap reference) | warning; never auto-split |
| F08 | `supersedes` target id exists | auto-fixable (clears dangling ref) |
| F09 | duplicate `id` across two roots in one `wiki_roots()` result | **error** — the cross-root-collision fix from §4.1 |
| S04 | `status` valid enum | warning |

Exact text to insert into `shapa/assets/AGENTS.md` (installed by `shapa init` on every fresh install, and via a new `shapa init --upgrade-docs` flag that overwrites only `AGENTS.md`/`placement.md`, never a user's own notes, for existing wikis):

New §3.1, right after the existing frontmatter block:

```markdown
### 3.1 Progressive-disclosure fields (required for memory/rule/issue; recommended for reference)

- `summary` — one line, <=160 characters, no embedded newline. This is the
  ONLY thing a session-start bootstrap loads for a note; write it so it is
  useful on its own, the way a Claude Code skill's `description` is. Missing
  or oversized is warning F04 (error once backfilled).
- `scope` — `global` (true in any repo), `repo` (true only in this wiki's own
  repo), or `external` (true only in a non-workspace repo, filed under
  `external/<repo>/`). Must match physical location (F06, auto-fixable).
  Placement is an agent decision, not automatic — see `placement.md`.
- `applies_to` — repo name (or list); required when `scope: external`.
- `tags` — optional kebab-case list for keyword boosting.
- `status` — `active` (default), `superseded`, or `draft` (S04).
- `supersedes` — optional id of a note this one replaces; dangling ref is F08.
```

Revise §4's opening (replacing "removed in favour of autonomy"):

```markdown
## 4. Node body

The body is everything after the closing `---`. Format is the maintaining
LLM's discretion, but SIZE is not free:

- `memory` / `rule` / `issue`: target 150-300 words. A note needing more
  splits into two linked notes rather than growing one file — this keeps
  every note inside the range fetch/embed retrieval performs best at
  (roughly 100-400 tokens; matches this engine's own SNIPPET_CHARS=500 /
  DEFAULT_BUDGET=4000 constants). Past 300 words: warning F07.
- `reference` (installed into `arch/`): may run long, but MUST open with a
  1-2 sentence abstract immediately below the frontmatter, and MUST use `##`
  headings past ~500 words. Past 2000 words: F07 (split into
  `arch/<topic>-<n>.md` + an index note).
```

New §10.1:

```markdown
### 10.1 Reading spans more than one wiki

A session's memory is not one directory. `shapa bootstrap`/`fetch`/`search`
resolve and search ALL of: the global wiki, this repo's own wiki (if
distinct), and this repo's `external/<repo>/` bucket (if it has no wiki of
its own). Writing is narrower and agent-gated: a new note declares exactly
one `scope` and lands in exactly one place — see `placement.md`. Never
assume a single resolved directory is the whole picture when reading.
```

Migration: `shapa maintain --backfill` fixes F06/F08 mechanically. A missing `summary` is **never fabricated silently** — it reuses the existing `resolve_contradictions()` subprocess pattern (`claude -p`, stdin closed) to draft one, written with `summary_status: draft` until confirmed. **Fatal-flaw fix (design 1's Codex/OpenCode gap):** because that path requires the `claude` CLI, `maintain --backfill` explicitly reports (not silently skips) any note it could draft a summary for vs. any it couldn't (no `claude` on PATH) — a Codex/OpenCode-only install still gets F04 warnings pointing at exactly which notes need a manually or MCP-`save`-drafted summary, rather than the backfill quietly no-op'ing.

## 7. What moves out of roukh-llm (and roukh-llm's thin-consumer end state)

**Moves into shapa-llm** (generalized off roukh-llm's `WORKSPACE_REPOS`/4-repo hardcoding):
- `resolve_global_shapa()`/`resolve_repo_shapa()`/`note_files()` → `config.wiki_roots()` (§4.1).
- `bootstrap()` → `shapa/bootstrap.py` (§4.2 Tier 1), now index-only/summary-based with `score.py`'s richer value formula instead of a bare `(-consequence,-recency)` sort.
- `do_search()`/`rank_search()` → `shapa fetch --query` / `shapa search` (§4.2 Tier 2/3), now hybrid-ranked (§5) and MCP-exposed (§7.1), not a manual-invocation-only side door.
- `save`'s `--global`/`--repo` gate → `capture.py --scope` / MCP `save` tool / `shapa/assets/placement.md`, parameterized generically (no repo allowlist).

**Stays in roukh-llm**: `guard.py`'s destructive-git blocking and `.claude/`-write-scope enforcement (general session hardening, unrelated to memory); the legacy Supabase `projects`/`inspect`/`export` subcommands (rename the file — no longer about `.shapa` at all).

**roukh-llm's thin-consumer end state** — executed in roukh-llm, tracked here: (1) `.claude/settings.json`'s `SessionStart: memory.py bootstrap` → `shapa bootstrap`; add the currently-missing `UserPromptSubmit → shapa fetch`; (2) `Stop`/`SubagentStop` → `shapa capture` + `shapa maintain --prune`; (3) `skills/workflow/memory/SKILL.md` + `skills/hermes/brain-tools/SKILL.md` repointed to `shapa search`/the MCP tool; (4) roukh-llm's duplicated `.shapa/AGENTS.md` copy deleted once `shapa init --upgrade-docs` is the schema source of truth (it had already drifted — §6's dropped-autonomy language was one such drift point); (5) retire the memri-backed OLD hook (`roukh-llm/scripts/runtime/memory.py`) — this is the fix for the **already-live, severe cross-project leakage** in §1's table, and per the operator decision in §10 it should not wait for every shapa-llm slice to land first.

### 7.1 Codex/OpenCode — not fully deferred

Judges flagged both other designs for pushing Codex/OpenCode support entirely into "documented, not built," directly against the operator's named requirement. This spec ships a minimal real surface, not just a plan:

- **MCP stdio server** (`shapa/mcp.py`, Slice 6, §8) — `search`/`get`/`save`/`placement` tools, stdlib JSON-RPC fallback when the `mcp` SDK extra isn't installed. This is the vendor-neutral mechanism both Codex and OpenCode can register today (Basic Memory's own pitch — "works across Claude, Codex, Cursor, ChatGPT, and any MCP client" — is direct precedent this is reachable, not aspirational).
- **Codex**: no per-turn hook exists, but Codex already natively ingests `AGENTS.md` at session start — that's its Tier 1 for free. `shapa bootstrap --format=agents-md` (new flag, ships in Slice 6) regenerates a static context block a repo's own `AGENTS.md` can `@`-include, refreshed on demand. Tier 2/3 come from the MCP server.
- **OpenCode**: registers the same MCP server in its config for Tier 2/3. A plugin calling `shapa fetch` on OpenCode's own prompt-submit-equivalent event is a documented follow-up (OpenCode's plugin API is less stable than MCP registration), not blocking this spec's slices.
- `install.sh --harness codex|opencode|all` routes registration to the right config file (`~/.codex/config.toml`'s `[mcp_servers.shapa]`, OpenCode's own config) alongside the Claude-Code default.

## 8. Implementation slices with acceptance criteria + verification commands

Existing baseline: 65 tests / 9 files, 62 passing / 3 failing (`test_maintain.py`, the `MERGE_THRESHOLD` calibration bug). Every slice below keeps that baseline green throughout; single-root installs are the unchanged default path until a second root actually exists.

| # | Slice | Acceptance criteria | Verification |
|---|---|---|---|
| 0 | Fix 3 failing `test_maintain.py` tests + split `MERGE_THRESHOLD_EMBED`/`_JACCARD` | 65/65 green, with **and** without `[semantic]` installed | `python -m pytest tests/test_maintain.py` (or `unittest discover -s tests` if pytest isn't a dep yet) run twice, once per extra combo |
| 1 | `config.wiki_roots()` + F09 collision check | 3-repo fixture: correct set/order/kind, dedup, no crash on missing global dir, duplicate id across roots raises F09 | `python -m pytest tests/test_config_multiroot.py` |
| 2 | `fetch.select()` reads all roots, proportional-floor budget, confidence floor | Global+repo fixture: both roots' anchors present, no root's share hits zero at 2/3/5+ roots, off-topic query returns anchors-only + the no-match marker, not padded filler | `python -m pytest tests/test_fetch_multiroot.py` |
| 3 | `shapa/bootstrap.py` + `SessionStart` wiring | `echo '{"cwd":...}' \| shapa bootstrap` emits valid metadata-only `additionalContext` within budget; `install.sh --dry-run`/`--uninstall` cover it via the generic loop | `python -m pytest tests/test_bootstrap.py`; `bash install.sh --dry-run` |
| 4 | Schema fields + F04–F09/S04 + `AGENTS.md` §3.1/§4/§10.1 rewrite | New codes fire correctly; the real 155-note corpus stays `valid=True` (warnings only) | `python -m pytest tests/test_validate.py`; `shapa validate --all-roots <real-wiki>` |
| 5 | `shapa/store.py` sqlite FTS5 index + daemon staleness check | Incremental sync touches only changed files; FTS5-creation-failure path returns results identical to the pre-index hand-rolled BM25 on a fixed fixture; a `capture` mid-session is visible on the daemon's next request | `python -m pytest tests/test_store.py tests/test_serve.py` |
| 6 | `embed.py` → model2vec, RRF (`rank.py`), MCP server (§7.1), `--format=agents-md` | `pip install shapa[semantic]` installs no torch; merge decisions identical with/without the extra on a fixed fixture; MCP `tools/list`/`tools/call` round-trip against a stub wiki (no network needed) | `python -m pytest tests/test_embed.py tests/test_rrf.py tests/test_mcp.py` |
| 7 | `capture.py --scope`/`shapa save --scope` + generic `placement.md` asset | Default scope inference works with no flag when unambiguous; `external` without `--applies-to` errors clearly; capture never blocks | `python -m pytest tests/test_placement.py` |
| 8 | CI (`.github/workflows/test.yml`, currently absent) | A PR that reintroduces the Slice-0 calibration bug fails CI | push a scratch branch with the regression, confirm red |
| 9 | roukh-llm consumer migration (§7) | Both empirical failures from §1's table gone: repo notes appear at bootstrap, memri leakage hook retired | re-run this session's own 8-prompt probe per repo after the swap |

**Fatal-flaw fix (design 1's live-e2e gap):** Slices 3, 5, and 6 each additionally require one manual, non-synthetic check before being called done — installing into a real `~/.claude/settings.json` (or a scratch equivalent) and confirming an actual Claude Code session fires the hook and shows the expected merged output, not just the unit/synthetic-fixture result. A slice with green tests and no live check is not accepted.

## 9. Risks

- **Codex/OpenCode coverage is still thinner than Claude Code's.** The MCP surface is real and testable without either CLI, but the Codex `--format=agents-md` static-regeneration path and the OpenCode plugin are only manually verifiable against those actual tools, which this repo can't script. Residual risk after Slice 6: "fully operating" for Codex/OpenCode means MCP-tool-level parity, not hook-level parity, until an operator with those CLIs confirms.
- **`maintain --backfill`'s summary-drafting still needs the `claude` CLI.** For a Codex/OpenCode-only install with no `claude` on PATH, missing summaries surface as F04 warnings an agent or the MCP `save` tool must fill manually — not a blocker, but not silent-and-free either.
- **Daemon staleness fix is a stat-per-request, not zero-cost.** At very large corpora (thousands of notes touched between requests) the mtime/size check adds a small but nonzero tax per fetch; acceptable at the perf budgets in §5, worth re-measuring if corpora grow 10x.
- **Two competing global wikis in roukh-llm today** (stale 85-note main-branch `.shapa` vs. the curated 155-note unmerged `feat/shapa-memory` `.shapa`) are an operator-side fact this spec's multi-root mechanism cannot resolve on its own — merging roots doesn't tell you which root is canonical. Flagged as an operator decision below, not silently assumed.
- **RRF and the confidence floor change ranking behavior** on the real 155-note wiki in ways not yet measured against real prompts (only against fixtures) — Slice 6's acceptance should include one manual pass over the probe's original 8-prompt-per-repo set before calling relevance "fixed," not just "changed."

## 10. Operator decisions

1. **Which roukh-llm global wiki is canonical, and when does the leakage fix land relative to shapa-llm's build?**
   - Option A: pick the 155-note `feat/shapa-memory` `.shapa` as canonical now, merge it to roukh-llm `main`, and retire the memri OLD hook (point `SessionStart` at a no-op or the new prototype) immediately — decoupled from waiting on shapa-llm Slices 0–8, since the leakage is live today and Slice 9 is scheduled last.
   - Option B: hold the roukh-llm-side fix until shapa-llm ships through Slice 6 (real semantic backend + MCP), then do one migration pass straight to `shapa bootstrap`/`shapa fetch`, skipping an interim hand-off through the existing prototype.
   - Data: leakage is confirmed live and severe (§1); Option A removes it sooner but means a second migration later; Option B means it stays live longer but is migrated exactly once.
2. **Default `MIN_RELEVANCE` confidence-floor threshold (§4.1).** No universal "correct" value — too strict silently drops real matches (regressing recall on the real 155-note wiki), too loose reproduces the "always k results" bug. Options: (a) a fixed score cutoff tuned once against the probe's own 8-prompt-per-repo set and hand-checked; (b) a percentile-relative cutoff (e.g., drop results scoring below X% of the top result) that adapts per query. Needs a human relevance judgment on real prompts either way — not decidable from code alone.
3. **`bootstrap.sh` default extras (§3): `[semantic,mcp]` always, or ask?** Installing `model2vec`+`mcp` by default best serves "anyone who installs shapa gets it fully operating," but adds install weight/time a minimal-footprint user might not want. Options: (a) default-on as specified, `--no-embeddings`/`--no-mcp` to opt out (this spec's choice); (b) interactive prompt during `bootstrap.sh`; (c) default-off, requiring an explicit `--full` flag. No data favors one over the others — it's a stance on who the median installer is.

### Decisions taken (operator, 2026-09-30)
1. **Leakage fix timing: A, but only when it affects the build.** It doesn't affect slices 0–8: they test against the 155-note wiki at `roukh-llm/.worktrees/shapa-memory/.shapa` in place, and the live Claude Code checks use `claude -p --settings`. So the roukh-llm merge and hook switch happen in slice 9.
2. **`MIN_RELEVANCE`: (b) percentile-relative.** Drop results scoring below a fraction of the top result's fused score. The fraction is a config value, and its default is calibrated on the probe prompt set.
3. **`bootstrap.sh` extras: (b) interactive prompt.** Ask whether to install `[semantic]` and `[mcp]`. With no TTY (unattended installs), install the core only and print the exact command to add the extras. Flags `--with-semantic`, `--with-mcp`, and `--full` skip the prompt.
4. **Project memories live only in that project's own `.shapa` (operator, 2026-09-30).** The global wiki holds cross-project rules and facts only. There is no `external/<repo>/` staging inside the global wiki. `--scope external --applies-to <repo>` resolves the target repo's path and writes into `<that repo>/.shapa/`, creating the folder if needed. If the repo can't be found locally, it errors and doesn't fall back to the global wiki. `wiki_roots()` loads another project's notes only when the cwd is inside that project.
6. **Lean wiki shape (operator, 2026-09-30).** Every wiki, global or repo, has the same lean shape. The engine lints it and `shapa maintain` reports what to fix. Limits are config values; defaults are:
   - `agenda.md` is required. It lists the top 3 fires only (F11 if missing, or if it has more than 3 items). `ideas.md` is optional: an append-only dated log with a status per entry.
   - Live notes at the root (memory/rule/issue): at most 40 (F10). Reference docs in `arch/`: at most 12 (F10). Each note is 150–300 words and each reference at most 2,000 words (F07, already in §6).
   - Live wiki total (everything except `archive/` and `attic/`): at most 250 KB (F10).
   - No duplicates: one note per subject. `maintain` merges near-duplicates (the existing merge threshold) and never keeps two live files on the same topic. Superseded notes get `status: superseded` and move to `archive/`.
   - `archive/` and `attic/` are never loaded and don't count toward the limits. They hold history and scratch waiting on an operator decision.
   - `scope` values are `global | repo` only. `external` is retired by decision 4.
5. **No phases or roadmaps in any planning output (operator, 2026-09-29).** This covers spec prose, CLI output, and generated docs. Plans are written as options, stack, recommendation, open decisions and the next action. See roukh-llm/.shapa/no-phases-or-roadmaps.md.
