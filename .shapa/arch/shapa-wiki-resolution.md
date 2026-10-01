---
id: shapa-wiki-resolution
type: reference
created: "2026-09-30T02:00:00Z"
consequence: 9
locus: output-meta
scope: repo
summary: shapa spec §4 - wiki_roots() multi-root reads, F09 id collisions, proportional-floor budget, confidence floor, and the four loading tiers.
---

How a session finds every wiki in scope and what it loads from each, at session start, per prompt, and on demand. Part of [[shapa-backend-spec]] (§4).

## 4.1 Multi-root resolution (the core fix)

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
      3. Always append kind="global" — UNLESS step 2's root IS global_root()
         (dedup: a repo whose own wiki *is* the global wiki gets one entry, not two).
    Global is never dropped by a repo-local wiki."""
```

**External staging is retired** (operator decision 4, [[shapa-operator-decisions]]). Project memories live only in that project's own `.shapa`; there is no `external/<repo>/` bucket inside the global wiki, and `wiki_roots()` loads another project's notes only when the cwd is inside that project. The `kind="external"` branch still present in `config.py` is legacy.

Writes stay narrow: `capture.py`/`shapa save` take a required `--scope` (no default that silently guesses). `--scope external --applies-to <repo>` is only a routing flag: it resolves that repo's path and writes into `<that repo>/.shapa/` (stored `scope: repo`), erroring rather than falling back to the global wiki when the repo can't be found locally. `shapa/assets/placement.md` (generic, no hardcoded repo allowlist) ships the decision rule ("would this still be true in a different repo tomorrow? yes → global, else → repo"), installed alongside `AGENTS.md`.

**Fatal-flaw fix — cross-root ID collisions.** Design 0 left uniqueness "by convention" with a silent keep-higher-scored policy — the one correctness property the whole merge depends on. Fixed: `validate.py` **F09** (duplicate `id` across two roots in one `wiki_roots()` result) is a hard *error*, checked by `shapa validate --all-roots`, and `fetch.py`'s merge refuses silent override — a collision surfaces to the agent as an explicit note ("id `X` exists in both `repo` and `global` — ambiguous, showing both"). Ids every wiki carries by construction (`AGENTS`, `placement`, `agenda`, the arch templates) are exempt.

**Fatal-flaw fix — anchor/budget starvation.** A fixed per-root anchor table reproduces starvation at a different granularity. Fixed: the per-root reservation is **proportional with a floor** — each root gets `max(floor_chars, budget // len(roots))` of the total character budget before cross-root relevance fills the remainder, and `locus: meta` anchors are capped at **2 per root**, never 2 total. `tests/test_fetch_multiroot.py` asserts no root's share collapses to zero at 2, 3 and 5+ roots.

**Fatal-flaw fix — confidence floor.** `fetch.select()` (Tier 2) and `shapa search` (Tier 3) used to emit exactly `k` results even at zero relevance. Fixed: results below the floor are dropped from the *relevance-ranked* fill; only the unconditional `locus: meta` anchors remain, and the fetch output says `<!-- no query-relevant notes found -->` instead of padding with filler. The floor is percentile-relative (operator decision 2): drop results scoring below a fraction of the top result's fused score — `MIN_RELEVANCE_FRACTION = 0.3` in `shapa/fetch.py`, a config value calibrated on the probe prompt set.

## 4.2 Loading tiers

- **Tier 0 — schema authority** (static, loaded once): the wiki's own `AGENTS.md`, read via `shapa init`'s installed copy / the harness's native `AGENTS.md` ingestion. Never re-fetched per turn.
- **Tier 1 — SessionStart, metadata-only** (`shapa/bootstrap.py`, the `SessionStart` hook): for every root from `wiki_roots()`, load only `id/type/summary/tags/consequence/locus` — never bodies. Print every `locus: meta` note's `summary` unconditionally (max 2/root), then the rest ranked by `score.score_meta()`, truncated to a ~1800-token budget (~4 chars/token heuristic, no tokenizer dependency). Never blocks: any exception → `""`, exit 0, same contract as `fetch.py`.
- **Tier 2 — per-prompt, on demand** (`fetch.py::select()`): iterates `wiki_roots()`; relevance = Reciprocal Rank Fusion ([[shapa-index-backend]]) over BM25 + vector rank lists, gated by the confidence floor; snippet-capped (500 chars/note) and total-budget-capped (4000 chars), per-root reserved as above.
- **Tier 3 — explicit, agent-invoked** (`shapa search "<query>"`, a named affordance in `AGENTS.md`, plus the MCP `search`/`get` tools): full bodies, no snippet cut, for re-querying mid-turn or following a `[[wikilink]]` a Tier-2 snippet truncated.
