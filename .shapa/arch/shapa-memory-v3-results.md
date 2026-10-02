---
id: shapa-memory-v3-results
type: reference
created: "2026-10-02T17:00:00Z"
consequence: 8
locus: output-meta
scope: repo
summary: Memory v3 measured - the storage lab's variant grid, the no-answer floor's calibration, and shapa 0.8.0 against 0.7.1 on the held-out set.
---

What the memory lab measured before v3 was built, how the no-answer floor was calibrated, and what the shipped code scores on the held-out set against the pre-v3 baseline. The design these numbers back is [[shapa-memory-v3]]. Part of [[shapa-backend-spec]].

## Method

- **Corpus.** 609 curated, secret-scrubbed rows from four memri project exports, plus the global wiki's 56 md notes: 665 source documents. The real importer (`shapa upgrade --import-memri`) turns the rows into 2,181 records, 19 of them archived because their row was superseded. The md notes stay md notes.
- **Held-out set.** 82 questions written before the build: 72 answerable (exact, paraphrase, multi-document) and 10 with no answer anywhere in the corpus. It was never used to tune anything.
- **Dev set.** 110 questions (80 answerable, 30 with no answer), written by a separate agent from rows disjoint from the held-out gold. All calibration used this set.
- **Metrics.** recall@k and MRR over source documents (records collapse to their memri row), from the lab's own metrics module. The no-answer false-positive rate is the share of no-answer questions that still got a memory. Tokens per prompt are the injected block's chars/4, wrapper included, at the hook's own k.
- **Baseline.** shapa 0.7.1, unmodified, on the same corpus with one md note per row.

## The lab grid (prototype, held-out set)

The lab built 32 storage variants of the record design with a throwaway prototype. The pilot, crossing vector representation with fusion:

| variant | recall@5 | MRR | bytes / 100 memories |
|---|---|---|---|
| BM25 only, unicode61 | 0.716 | 0.655 | 94.8 KB |
| float32 vectors only | 0.581 | 0.561 | 236.6 KB |
| float32 + BM25, RRF | 0.700 | 0.642 | 236.6 KB |
| **float32 + BM25, min-max fuse** | **0.752** | **0.700** | **236.6 KB** |
| int8 + BM25, RRF | 0.688 | 0.618 | 131.6 KB |
| binary + float rescore, fuse | 0.708 | 0.671 | 103.0 KB |

- **Tokenizer.** For the fused config, unicode61 scored 0.752, trigram 0.734 at 1.6x the bytes, and porter 0.720 (stemming mangles jargon and proper nouns).
- **Storage.** zlib text and supersession filtering changed neither recall nor size on this already-curated corpus. Contentless FTS5 is 63% smaller but cannot return the text, so it was not used.
- **Tokens.** The baseline injects full ~500-char bodies: 1,103 tokens per prompt with its wrapper. Summary-only v3 injects 308.
- **No-answer.** Every variant, baseline included, returned memory for 10 of 10 no-answer questions. Generic engineering words clear any fixed cosine floor (0.49-0.69 here, against a floor of 0.22), and BM25's idf grows with corpus size, so the 0.5 BM25 floor tuned on 29 notes never fires on 665 documents.

The operator chose float32 + fuse with unicode61: the top recall, at about 236 KB per 100 memories.

## Calibrating the no-answer floor (dev set only)

Feature study on the dev set: every no-answer question wrote some term as a name (an acronym, a camelCase word, or a capitalized word mid-sentence) that occurs nowhere in memory. No answerable question did. Answerable paraphrases often contain words the corpus lacks, but always plain lowercase ones. The highest top cosine among dev no-answer questions was 0.739.

The rule (`fetch.answerable`): keep the pre-v3 guard. Then, if the query names an unseen entity, answer only when the fused top cosine reaches 0.75. In bm25-only mode, an unseen entity always means no answer.

On dev, false positives fell from 30/30 to 0/30 with zero answerable questions rejected, in fused and bm25-only mode alike. A second rule came from the adversarial verify pass, not from either question set. A prompt with no content word at all ("ok"), or with two or more of which memory knows none, needs the same 0.75 cosine. In fused mode such prompts scored the same weak cosine (~0.23-0.25) against every item and cleared the old 0.22 guard. A single unknown word is left alone, because it is often a variant of a known word ("respond" for "response"). On dev it changed nothing: 0/30 false positives, 0 answerable questions rejected. Per-source min-max for the shared lexical scale was also tried there (recall@5 0.856, MRR 0.761). It lost to re-scoring notes and records with FTS5's BM25 formula over union corpus statistics (0.869, 0.823).

## Shipped code on the held-out set (shapa 0.8.0)

| config | recall@1 | recall@3 | recall@5 | MRR | no-answer FP | tokens / prompt |
|---|---|---|---|---|---|---|
| baseline, shapa 0.7.1 | 0.584 | 0.668 | **0.759** | **0.744** | 1.00 | 1,103 |
| v3 fused, floor off | 0.543 | 0.676 | 0.713 | 0.700 | 1.00 | 308 |
| **v3 fused (shipped)** | 0.543 | 0.676 | 0.713 | 0.700 | **0.20** | **290** |
| v3 bm25-only, floor off | 0.484 | 0.654 | 0.705 | 0.649 | 1.00 | 310 |
| v3 bm25-only (fallback) | 0.484 | 0.654 | 0.705 | 0.649 | 0.20 | 288 |

recall@5 by question kind, baseline then v3 fused: exact 0.95 then 1.00, paraphrase 0.735 then 0.647, multi-document 0.593 then 0.519.

Latency and size: the same corpus, all three configs timed back to back (20 runs each, a shared machine at load average 8-14):

| config | fetch warm p50 / p95 | fetch cold p50 / p95 | hook wall p50 / p95 | peak RSS | on disk per 100 |
|---|---|---|---|---|---|
| baseline, shapa 0.7.1 | 379 / 857 ms | 1,558 / 3,950 ms | 1,865 / 5,065 ms | 153 MB | 751 KB (notes + caches) |
| v3 fused | 160 / 260 ms | 1,127 / 1,596 ms | 1,380 / 2,627 ms | 145 MB | 64 KB log + 262 KB index |
| v3 bm25-only | 105 / 173 ms | 83 / 146 ms | 238 / 530 ms | 30 MB | same |

- **Warm** is a prompt answered by a process that already answered one; the optional daemon serves prompts this way.
- **Cold** is the first prompt in a fresh process, model load included, interpreter start excluded.
- **Hook wall** is `python -m shapa.fetch` end to end.
- **Cold fused** is dominated by loading model2vec, which 0.7.1 pays too.
- **Warm fused** is about ten times the lab prototype's 14 ms p95. The prototype searched one records table; the shipped path ranks notes and memories together and re-scores their union lexically on every prompt.
- **Capture:** a fresh `python -m shapa.capture` on a 1.2 MB, 1,000-line transcript, writing into a 2.2k-record log, ran at p50 231 / p95 323 ms wall (221 / 311 ms CPU). An incremental Stop, which re-reads only new lines, ran at p50 160 / p95 217 ms. Its in-process work is 92 ms; the rest is interpreter start and imports. Across three runs at this load, first-parse p50 ranged from 231 to 396 ms.
- **Pathological input:** a 50 MB transcript stays at 0.5 s, because input is bounded before parsing and redaction.

## Reading it

- **Both targets are met.** No-answer false positives are 0.20, against a target of at most 0.20. The floor cost no recall against untuned v3 (0.000, against an allowed 0.02), and no answerable question was rejected.
- **Tokens drop 74%:** 290 per prompt against 1,103, wrapper included.
- **Recall trails the baseline:** 4.6 points of recall@5 and 4.4 of MRR, all on paraphrase and multi-document questions. Exact recall rises to 1.00. The lab prototype showed the same direction with a smaller gap (0.752 against 0.759). The held-out set is reserved for measurement, so the cause has not been investigated on it.
- **Dev is easier than held-out** (recall@5 0.869 against 0.713). The floor was fitted on dev, so 0.20 is the floor's out-of-sample rate.
- **The two held-out false positives** ask about a project that memory does know, for a feature it doesn't have, in lowercase words only ("mobile app", "options hedging"). This is the rule's known blind spot: an unseen thing written as a name is caught, but a lowercase one is not.
