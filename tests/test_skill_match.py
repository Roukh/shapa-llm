"""``skill_match``: a task text in, at most 6 ranked active skills out (local CLI and local MCP)."""

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from shapa import bm25, mcp, skills

LIBRARY = {
    "durable-objects": "Build stateful Cloudflare Durable Objects: coordination, storage, alarms and RPC.",
    "workers-best-practices": "Cloudflare Workers best practices for production applications.",
    "tdd": "Test-driven development. Use when building features or fixing bugs test-first.",
    "supabase-postgres-best-practices": "Postgres schema, migrations, indexes and RLS policies.",
    "write-a-skill": "Creates, updates and retires skills in this library.",
    "hono": "Hono web framework routing and middleware.",
    "threejs-materials": "Three.js materials, shaders and textures.",
    "stripe-docs": "Stripe payments API documentation.",
}


@pytest.fixture
def conn(wikis):
    c = wikis.conn(wikis.global_wiki)
    for n, d in LIBRARY.items():
        skills.add_version(c, n, d, f"body of {n}", new=True, active=True, privileged=True)
    yield c
    c.close()


def names(found):
    return [r["name"] for r in found]


def test_a_task_ranks_the_skills_that_fit_it_first_with_the_words_that_matched(conn):
    found = skills.match_skills(skills.active_summaries(conn, "global"),
                                "add a Durable Objects room with alarms to my Cloudflare Worker")
    assert names(found)[0] == "durable-objects"
    assert "workers-best-practices" in names(found)
    assert "tdd" not in names(found)
    top = found[0]
    assert top["reason"].startswith("matches: ") and "durable" in top["reason"] and "alarms" in top["reason"]
    assert set(top) >= {"name", "description", "reason", "scope", "score"}


def test_at_most_six_come_back_whatever_the_caller_asks(wikis):
    c = wikis.conn(wikis.global_wiki)
    try:
        for i in range(15):
            skills.add_version(c, f"cloudflare-thing-{i}", "Cloudflare product guidance.", "x", new=True, active=True,
                               privileged=True)
        found = skills.match_skills(skills.active_summaries(c, "global"), "cloudflare product", limit=50)
        assert len(found) == 6
        assert len(skills.match_skills(skills.active_summaries(c, "global"), "cloudflare product", limit=2)) == 2
    finally:
        c.close()


def test_a_task_that_shares_no_word_with_any_skill_returns_nothing(conn):
    assert skills.match_skills(skills.active_summaries(conn, "global"), "quantum entanglement of sourdough") == []
    assert skills.match_skills(skills.active_summaries(conn, "global"), "") == []
    assert skills.match_skills(skills.active_summaries(conn, "global"), "the and for with") == []  # stop words only
    assert skills.match_skills([], "anything at all") == []


def test_only_skills_with_an_active_version_are_candidates(conn):
    skills.add_version(conn, "draft-only", "Cloudflare draft skill.", "x", new=True, active=False, privileged=True)
    assert "draft-only" not in names(skills.active_summaries(conn, "global"))
    assert "draft-only" not in names(skills.match_skills(skills.active_summaries(conn, "global"), "cloudflare draft"))


def test_the_name_counts_for_more_than_the_same_word_in_a_description(conn):
    skills.add_version(conn, "alarm-clock", "Wake people.", "x", new=True, active=True, privileged=True)
    skills.add_version(conn, "wake-up", "Use the alarm clock to wake people.", "x", new=True, active=True,
                       privileged=True)
    found = skills.match_skills(skills.active_summaries(conn, "global"), "alarm clock")
    assert names(found)[0] == "alarm-clock"


def test_a_repo_skill_hides_the_global_one_of_the_same_name(wikis, conn):
    r = wikis.conn()
    try:
        skills.add_version(r, "tdd", "This repo's own test-first rules.", "repo", new=True, active=True,
                           privileged=True)
    finally:
        r.close()
    found = skills.match_skills(skills.local_candidates(str(wikis.repo)), "test-first bugs")
    tdd = [x for x in found if x["name"] == "tdd"]
    assert len(tdd) == 1 and tdd[0]["scope"] == "repo"


def run_cli(wikis, *argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = skills.run_skill([*argv, "--cwd", str(wikis.repo)])
    return code, out.getvalue()


def test_the_cli_prints_matches_as_text_and_json(wikis, conn):
    code, text = run_cli(wikis, "match", "stripe payments")
    assert code == 0 and text.startswith("stripe-docs [global] - ") and "matches: stripe, payments" in text
    code, js = run_cli(wikis, "match", "stripe payments", "--json")
    assert json.loads(js)["skills"][0]["name"] == "stripe-docs"
    code, text = run_cli(wikis, "match", "nothing related whatsoever")
    assert text.strip() == "no skill matches this text"


def test_the_local_mcp_tool_returns_the_same_matches(wikis, conn):
    res = mcp._HANDLERS["skill_match"]({"text": "hono routing middleware"}, str(wikis.repo))
    assert res["skills"][0]["name"] == "hono"
    assert set(res["skills"][0]) == {"name", "description", "scope", "reason"}
    assert "error" in mcp._HANDLERS["skill_match"]({"text": " "}, str(wikis.repo))
    assert any(t["name"] == "skill_match" for t in mcp.TOOLS)


def test_a_worked_example_scores_exactly_as_the_hosted_matcher_does():
    """The same candidates and tasks, and the same numbers, are asserted in hosted/test/skills.test.ts."""
    cands = [
        {"name": "durable-objects", "description": "Stateful Cloudflare Durable Objects, alarms and RPC.", "scope": "global"},
        {"name": "tdd", "description": "Test-driven development: test first.", "scope": "global"},
        {"name": "hono", "description": "Hono web framework routing.", "scope": "global"},
    ]
    first = skills.match_skills(cands, "test first development")
    assert [(r["name"], r["score"], r["reason"]) for r in first] == [("tdd", 3.483, "matches: test, first, development")]
    second = skills.match_skills(cands, "build a Durable Objects room with alarms")
    assert [(r["name"], r["score"]) for r in second] == [("durable-objects", 3.9009)]


def test_the_hosted_matcher_uses_the_same_stop_words_and_formula(wikis):
    """hosted/src/skillmatch.ts holds its own tokenizer and BM25: the constants must equal the engine's."""
    src = Path(__file__).resolve().parent.parent / "hosted" / "src" / "skillmatch.ts"
    if not src.is_file():
        pytest.skip("the Worker is not in this checkout")
    ts = src.read_text(encoding="utf-8")
    stop = set(re.findall(r'"([a-z]+)"', re.search(r"STOP\s*=\s*new Set\(\[(.*?)\]\)", ts, re.S).group(1)))
    assert stop == set(bm25._STOP)
    assert float(re.search(r"\bK1\s*=\s*([\d.]+)", ts).group(1)) == bm25._BM25_K1
    assert float(re.search(r"\bB\s*=\s*([\d.]+)", ts).group(1)) == bm25._BM25_B
    assert int(re.search(r"\bMATCH_MAX\s*=\s*(\d+)", ts).group(1)) == skills.MATCH_MAX
    assert int(re.search(r"\bMATCH_TEXT_MAX\s*=\s*(\d+)", ts).group(1)) == skills.MATCH_TEXT_MAX
    assert re.search(r"\[a-z\]\[a-z0-9\]\{2,\}", ts) and bm25._WORD_RE.pattern == r"[a-z][a-z0-9]{2,}"
