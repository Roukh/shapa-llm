"""GAP C acceptance: retrieval-quality regression suite (shapa-backend-spec.md).

Fixes verified here:
  1. Fusion no longer buries a strong single-modality match. ``rank.fuse``
     (shapa.rank) min-max-normalizes each ranking before combining, instead
     of RRF's rank-position-only view, so a note that is the single best
     semantic match (even with weak/zero BM25 overlap) can compete on its
     own merits rather than always losing to two backends' lukewarm
     agreement on a different, merely-mediocre note.
  2. An ABSOLUTE minimum-relevance guard, on top of the existing
     percentile-relative floor (``MIN_RELEVANCE_FRACTION``): a clearly
     off-topic prompt now returns the anchors plus the explicit
     "no query-relevant notes found" marker, never a padded, low-confidence
     guess. The guard is checked on each modality's RAW score
     (``fetch.MIN_ABSOLUTE_EMBED`` / ``MIN_ABSOLUTE_BM25_BARE_CORE``), never
     on the fused/normalized scale - see ``fetch.py``'s module docstring on
     why normalization alone cannot carry this signal.

This corpus is a frozen SUBSET of the real global wiki at
roukh-llm/.worktrees/shapa-memory/.shapa (37 root notes there; 27 copied
here, verbatim, nothing stripped - none of the source notes contain
secrets). It stays well inside the lean-wiki-shape limits (spec §10
decision 6: <=40 root notes, <=12 arch refs, <=250 KB live) on its own -
27 root notes, 1 arch ref, ~51 KB of ``.md`` content.

33 prompts total (29 on-topic with an expected note id, 4 off-topic with
none) - comfortably over the required minimum of 24.
"""

import unittest
from pathlib import Path

from shapa import embed, fetch

WIKI = Path(__file__).parent / "fixtures" / "retrieval_eval" / "wiki"

#: (prompt, expected top-5 note id or None for a deliberately off-topic
#: prompt that must instead surface the no-match marker).
PROMPTS: list[tuple[str, str | None]] = [
    ("how should you respond to me", "response-style"),
    ("recipe for banana bread", None),
    ("who won the 1998 world cup", None),
    ("what's the weather like tomorrow", None),
    ("best pizza toppings for a party", None),
    ("don't sugarcoat things, just give me the tradeoffs straight", "response-style"),
    ("why do the hook tests hang on main", "roukh-llm-test-hangs"),
    ("is it safe to run shapa maintain --prune right now", "shapa-maintain-prune-data-loss"),
    ("someone told me to ignore the rules and run this command anyway", "agent-security-boundaries"),
    ("should I praise this code even if it's mediocre", "anti-sycophancy"),
    ("where does CLAUDE_CONFIG_DIR point", "claude-config-symlink"),
    ("can I move this skill's companion doc somewhere else", "companion-docs-stay-by-path"),
    ("the pipeline named step 3 as mine, should I also start step 4", "finish-current-pipeline-step-not-next"),
    ("can you just go ahead and remember this for next time without asking first", "operator-escalation-protocol"),
    ("does this note belong in the global wiki or the repo's own", "placement"),
    ("what is ORRERY and who is Roukh", "roukh-entity-orrery-framework"),
    ("a note only links back to its own type page, is that a problem", "shapa-notes-need-rich-links"),
    ("can I trust an agent's own summary of what it did", "trust-verification-not-agent-reports"),
    ("eight worktrees running in parallel, what could go wrong", "wave-pipeline-multiworktree-safety-lessons"),
    ("can a workflow tool script call Math.random or Date()", "workflow-tool-script-gotchas"),
    ("npm install is missing in a nested worktree folder", "worktree-hygiene-discipline"),
    ("how long should a source file be and how much validation", "coding-style-baseline"),
    ("give me a project plan with phases and milestones", "no-phases-or-roadmaps"),
    ("which email address should I use for this task", "operator-identity"),
    ("my shell command got blocked, is it a false positive", "shell-compound-command-forgery-bypass"),
    ("should I build a brand new UI component from scratch", "ui-library-registry-method"),
    ("the diff quality gate flagged my JSON schema file as suspicious code", "diff-quality-gate-gotchas"),
    ("who reviews this PR, coderabbit or a human", "fresh-eyes-review-sole-pr-reviewer"),
    ("gh auth is using the wrong github account again", "ghobz-git-identity-repo-hygiene"),
    ("does this codebase note get written to the shared database table", "memory-shapa-integration"),
    ("do I need to run the roukh-skill command before coding", "roukh-skill-mandatory-gate"),
    ("turn our old PR history into reusable playbooks", "skills-workflow-library"),
    ("what is the overview of the shared skills library and native hooks install flow", "architecture"),
]

ON_TOPIC = [(q, exp) for q, exp in PROMPTS if exp is not None]
OFF_TOPIC = [q for q, exp in PROMPTS if exp is None]

#: recall@5 measured against this fixture on the PRE-GAP-C implementation
#: (plain shapa.rank.rrf, no absolute guard, body-only embedding text,
#: unfiltered modal-auxiliary stopwords) - HEAD before this slice, verified
#: by temporarily swapping in that committed version of bm25.py/fetch.py/
#: rank.py against this same fixture: 28/29 = 0.966 (the single miss,
#: "anti-sycophancy", is exactly the RRF-burying failure mode GAP C fixes).
#: This is the regression floor future changes must not drop below.
BASELINE_RECALL_AT_5 = 28 / 29


@unittest.skipUnless(embed.available(), "GAP C's calibration assumes the [semantic] extra")
class TestRetrievalEvalAcceptance(unittest.TestCase):
    """shapa-backend-spec.md GAP C's own acceptance criteria, run against
    the frozen fixture corpus."""

    def test_at_least_24_prompts(self):
        self.assertGreaterEqual(len(PROMPTS), 24)

    def test_response_style_query_lands_in_top_3(self):
        # The canonical "semantic-only, no shared vocabulary" case: zero
        # BM25 overlap (see fetch.py's module docstring), a real cosine
        # match. Pre-fix this note (present but weak in both rankings'
        # eyes) landed at rank 5 of 5 - outside the top 3. Fixed fusion
        # must place it at rank <= 3.
        selected = fetch.select("how should you respond to me", root=WIKI, k=5, read_only=True)
        ids = [n.id for n, _ in selected]
        self.assertIn("response-style", ids[:3])

    def test_off_topic_prompts_return_anchors_plus_no_match_marker(self):
        for q in OFF_TOPIC:
            with self.subTest(prompt=q):
                ctx = fetch.fetch_context(q, root=WIKI, k=5, record=False)
                self.assertIn("<!-- no query-relevant notes found -->", ctx,
                              f"{q!r} did not surface the no-match marker")
                selected = fetch.select(q, root=WIKI, k=5, read_only=True)
                ids = {n.id for n, _ in selected}
                # Anchors only - never a padded, low-confidence guess.
                self.assertEqual(ids, {"placement", "shapa-maintain-prune-data-loss"})

    def test_recall_at_5_does_not_regress(self):
        hits = 0
        misses = []
        for q, expected in ON_TOPIC:
            selected = fetch.select(q, root=WIKI, k=5, read_only=True)
            ids = [n.id for n, _ in selected]
            if expected in ids:
                hits += 1
            else:
                misses.append((q, expected, ids))
        recall = hits / len(ON_TOPIC)
        self.assertGreaterEqual(
            recall, BASELINE_RECALL_AT_5,
            f"recall@5 regressed: {recall:.3f} < baseline {BASELINE_RECALL_AT_5:.3f}; "
            f"misses: {misses}",
        )

    def test_on_topic_queries_never_trip_the_no_match_marker(self):
        for q, _expected in ON_TOPIC:
            with self.subTest(prompt=q):
                ctx = fetch.fetch_context(q, root=WIKI, k=5, record=False)
                self.assertNotIn("<!-- no query-relevant notes found -->", ctx)


if __name__ == "__main__":
    unittest.main()
