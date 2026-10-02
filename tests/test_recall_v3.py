"""Memory v3 recall: md notes and v3 memory records in one fused ranking
(fetch / bootstrap / MCP), summary-only injection, `shapa get`, and the
reported retrieval mode (`shapa status`/`doctor`)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from shapa import bootstrap, config, embed, fetch, get, mcp, memlog, status, store
from shapa.config import WikiRoot


def _note(root: Path, nid: str, body: str, summary: str = "", locus: str = "output"):
    root.mkdir(parents=True, exist_ok=True)
    s = f'summary: "{summary}"\n' if summary else ""
    (root / f"{nid}.md").write_text(
        f"---\nid: {nid}\ntype: rule\ncreated: \"2026-09-01T00:00:00Z\"\nconsequence: 6\n"
        f"locus: {locus}\n{s}---\n{body} [[rule]]\n", encoding="utf-8")


def _mem(root: Path, summary: str, body: str = "", kind: str = "gotcha", **kw):
    rec = memlog.make_record(kind=kind, summary=summary, body=body or summary,
                             created="2026-10-01T00:00:00Z", **kw)
    memlog.append(root, [rec])
    return rec


class RecallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wiki = self.tmp / "wiki"
        (self.wiki).mkdir()
        (self.wiki / "AGENTS.md").write_text("---\nid: AGENTS\ntype: reference\n---\nx\n")
        _note(self.wiki, "deploy-checklist", "Run the migration dry-run before every deploy "
              "to production, then tag the release.", summary="Deploy: dry-run migrations, tag.")
        _note(self.wiki, "colour-palette", "The brand palette is teal and sand.",
              summary="Brand colours.")
        self.webhook = _mem(self.wiki, "Stripe webhook retries flood the queue when the "
                            "handler returns 500", tags=["billing/webhooks.py"])
        self.lockfile = _mem(self.wiki, "pnpm lockfile conflicts when two branches add the "
                             "same dependency", kind="gotcha")
        self.roots = [WikiRoot(path=self.wiki, kind="repo")]
        self._env = mock.patch.dict(os.environ, {"SHAPA_MEMORY": str(self.wiki)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp)


class TestFusedRanking(RecallCase):
    def test_memory_surfaces_for_its_topic(self):
        sel = fetch.select_multi("why do webhook retries flood the queue", roots=self.roots)
        ids = [n.id for n, _ in sel.items]
        self.assertEqual(ids[0], self.webhook.id)
        self.assertFalse(sel.no_match)

    def test_note_and_memory_compete_in_one_list(self):
        sel = fetch.select_multi("deploy migration dry-run and webhook retries",
                                 roots=self.roots, k=8)
        ids = {n.id for n, _ in sel.items}
        self.assertIn("deploy-checklist", ids)
        self.assertIn(self.webhook.id, ids)

    def test_injection_is_summary_only_with_ids(self):
        sel = fetch.select_multi("webhook retries", roots=self.roots)
        block = fetch.render(sel)
        self.assertTrue(block.startswith(fetch.WRAPPER_OPEN))
        self.assertIn(f"- {self.webhook.id}: {self.webhook.summary}", block)
        for _, text in sel.items:
            self.assertLessEqual(len(text), fetch.LINE_CHARS)

    def test_superseded_and_archived_never_surface(self):
        memlog.archive(self.wiki, self.webhook.id, "test")
        sel = fetch.select_multi("webhook retries flood", roots=self.roots, k=8)
        self.assertNotIn(self.webhook.id, [n.id for n, _ in sel.items])

    def test_record_use_goes_to_memory_counters(self):
        fetch.fetch_context("webhook retries flood the queue", roots=self.roots, record=True)
        conn = memlog.open_index(self.wiki)
        try:
            self.assertEqual(memlog.uses(conn)[self.webhook.id][0], 1)
        finally:
            conn.close()

    def test_read_only_search_leaves_no_index_behind(self):
        store.db_path(self.wiki).unlink(missing_ok=True)
        fetch.select_multi("webhook retries", roots=self.roots, read_only=True)
        self.assertFalse(store.db_path(self.wiki).exists())

    def test_same_memory_in_two_roots_shown_once_not_a_collision(self):
        other = self.tmp / "global"
        other.mkdir()
        memlog.append(other, [self.webhook])
        roots = self.roots + [WikiRoot(path=other, kind="global")]
        sel = fetch.select_multi("webhook retries flood the queue", roots=roots, k=8)
        ids = [n.id for n, _ in sel.items]
        self.assertEqual(ids.count(self.webhook.id), 1)
        self.assertEqual(sel.collisions, [])
        self.assertEqual(sel.item_roots[self.webhook.id], self.wiki)

    def test_minmax_lexical_variant_also_ranks(self):
        with mock.patch.object(fetch, "UNIFIED_LEXICAL", "minmax"):
            sel = fetch.select_multi("why do webhook retries flood the queue", roots=self.roots)
        self.assertEqual(sel.items[0][0].id, self.webhook.id)


class TestNoAnswerFloor(RecallCase):
    def test_entity_terms_shapes(self):
        q = "Does the deploy use Kafka, gRPC or SAML? Postgres is fine; community plugins."
        terms = ["deploy", "kafka", "grpc", "saml", "postgres", "community", "plugins"]
        self.assertEqual(sorted(fetch.entity_terms(q, terms)), ["grpc", "kafka", "saml"])
        self.assertEqual(fetch.entity_terms("Kafka first.", ["kafka"]), [])

    def test_unseen_named_entity_is_no_answer(self):
        sel = fetch.select_multi("Do the webhook retries go through RabbitMQ?", roots=self.roots)
        self.assertTrue(sel.no_match)
        self.assertEqual(sel.features["entity_oov"], ["rabbitmq"])

    def test_unseen_lowercase_word_still_answers(self):
        sel = fetch.select_multi("why do webhook retries flood the queue so frequently",
                                 roots=self.roots)
        self.assertFalse(sel.no_match)
        self.assertEqual(sel.items[0][0].id, self.webhook.id)

    def test_known_named_entity_answers(self):
        sel = fetch.select_multi("Why do Stripe webhook retries flood the queue?", roots=self.roots)
        self.assertFalse(sel.no_match)

    def test_untuned_switch_restores_the_pre_v3_guard(self):
        with mock.patch.object(fetch, "CALIBRATED_FLOOR", False):
            sel = fetch.select_multi("Do the webhook retries go through RabbitMQ?", roots=self.roots)
        self.assertFalse(sel.no_match)

    def test_floor_holds_in_bm25_mode(self):
        with mock.patch.object(embed, "_AVAILABLE", False):
            sel = fetch.select_multi("Do the webhook retries go through RabbitMQ?", roots=self.roots)
        self.assertTrue(sel.no_match)


class TestModeReporting(RecallCase):
    def test_bm25_only_mode_is_reported_and_still_recalls(self):
        with mock.patch.object(embed, "_AVAILABLE", False):
            sel = fetch.select_multi("webhook retries flood the queue", roots=self.roots)
            self.assertEqual(sel.mode, "bm25")
            self.assertEqual(sel.items[0][0].id, self.webhook.id)
            self.assertIn("bm25-only", fetch.render(sel))
            report = status.gather(str(self.wiki))
            self.assertEqual(report["recall"]["mode"], "bm25")
            self.assertIn("bm25-only", bootstrap.recall_mode_line())

    @unittest.skipUnless(embed.available(), "needs the [semantic] extra")
    def test_fused_mode_is_reported(self):
        sel = fetch.select_multi("webhook retries", roots=self.roots)
        self.assertEqual(sel.mode, "fused")
        self.assertNotIn("bm25-only", fetch.render(sel))
        self.assertEqual(status.gather(str(self.wiki))["recall"]["mode"], "fused")

    def test_mcp_search_returns_mode_and_memory_kind(self):
        out = mcp.tool_search({"query": "webhook retries flood"}, str(self.wiki))
        self.assertIn(out["mode"], ("fused", "bm25"))
        hit = next(r for r in out["results"] if r["id"] == self.webhook.id)
        self.assertEqual((hit["source"], hit["kind"]), ("memory", "gotcha"))
        self.assertEqual(hit["summary"], self.webhook.summary)

    def test_doctor_flags_malformed_log_lines(self):
        with (memlog.log_dir(self.wiki) / "2026-10.jsonl").open("a") as fh:
            fh.write("{not json\n")
        report = status.gather(str(self.wiki))
        self.assertTrue(any("malformed" in p for p in status.problems(report)))


class TestGet(RecallCase):
    def test_get_memory_full_text(self):
        out = mcp.tool_get({"id": self.webhook.id}, str(self.wiki))
        self.assertEqual(out["source"], "memory")
        self.assertEqual(out["status"], "active")
        self.assertEqual(out["record"]["tags"], ["billing/webhooks.py"])
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            get.main([self.webhook.id, "--cwd", str(self.wiki)])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(self.webhook.body, buf.getvalue())

    def test_get_note_and_missing(self):
        self.assertEqual(mcp.tool_get({"id": "deploy-checklist"}, str(self.wiki))["type"], "rule")
        self.assertIn("error", mcp.tool_get({"id": "m-0000000000"}, str(self.wiki)))


class TestBootstrap(RecallCase):
    def test_memories_join_overview_capped_with_counts(self):
        for i in range(12):
            _mem(self.wiki, f"distinct memory {chr(97 + i)}lpha {chr(98 + i)}eta topic {i}x"
                 f" {chr(99 + i)}amma", kind="preference")
        text = bootstrap.build_context(roots=self.roots)
        mem_lines = [l for l in text.splitlines() if l.startswith("- [") and " (memory): " in l]
        self.assertTrue(mem_lines)
        self.assertLessEqual(len(mem_lines), bootstrap.MEMORY_CAP_PER_ROOT)
        self.assertIn("14 memories", text)
        self.assertIn("recall: ", text)


class TestNoMemoriesUnchanged(unittest.TestCase):
    """A wiki that never captured gets no memory tables, no log, no lines."""

    def test_notes_only_wiki(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            _note(tmp, "deploy-checklist", "Run the migration dry-run before every deploy.")
            sel = fetch.select_multi("migration dry-run deploy", roots=[WikiRoot(tmp, "repo")])
            self.assertEqual(sel.items[0][0].id, "deploy-checklist")
            self.assertFalse((tmp / "memory").exists())
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
