---
name: nlp-pipeline
description: Reference knowledge for Trade-Dash's news NLP stage (nlp_pipeline.py) — ticker extraction, sentiment scoring, topic classification, urgency scoring, and article deduplication, including the dev-lexicon-to-prod-transformer swap contract.
when_to_use: writing or reviewing nlp_pipeline.py, swapping lexicon sentiment for finbert, debugging duplicate articles, extracting tickers from headlines
---

Trade-Dash's NLP stage lives in `nlp_pipeline.py` (see CLAUDE.md §4 AD-06,
§5, §7 Phase 2 TODOs).

## Pipeline order
ticker extraction → sentiment scoring → topic classification → urgency
scoring → deduplication → Event emission.

## Dev↔prod swap contract (AD-06) — function-body swaps only
| Stage | Dev | Prod | Interface |
|---|---|---|---|
| Sentiment | BULL/BEAR word-list lexicon | `ProsusAI/finbert` | `score_sentiment(text) -> float [-1,1]` |
| Dedup | BoW cosine similarity | `sentence-transformers/all-MiniLM-L6-v2` + pgvector `<=>` | `is_duplicate(article, corpus) -> bool` |
| NER | Regex/lexicon ticker list | spaCy `en_core_web_sm` | `extract_tickers(text) -> list[str]` |

If a swap forces a change to `unified_pipeline.py` call sites, the signature
broke the contract — fix the signature, not the caller.

## Dedup mechanics (FR-03)
Scope similarity search to articles sharing a ticker *before* applying the
cosine threshold (~0.92 starting point) — unscoped global similarity dedups
unrelated articles with similar boilerplate text.

## Gotchas
- Ticker false positives (single-letter/common-word symbols) — always
  cross-check against a known-assets list.
- Urgency feeds `confidence`/`meta`, never `direction`.
- Keep prod-only imports (`transformers`, `torch`, `spacy`) lazy/guarded so
  the zero-dependency lexicon path still runs standalone.

## Delegate
- Deep implementation work → `nlp-pipeline-expert` agent.
- Pre-merge review → `nlp-pipeline-reviewer` agent.
