---
name: event-pipeline
description: Reference knowledge for Trade-Dash's core event pipeline (unified_pipeline.py) — the canonical Event schema, SignalFactory, and UnifiedPipeline orchestration. Use when creating/extending the Event dataclass, adding a new signal type, or wiring ingest_articles()/ingest_signal_events()/correlate()/alert_payloads().
when_to_use: writing or reviewing unified_pipeline.py, adding a new SignalFactory constructor, debugging why an Event isn't reaching the scorer
---

Trade-Dash's core event pipeline lives in `unified_pipeline.py` (see
CLAUDE.md §4 AD-01/AD-02, §5, §9).

## The canonical Event (never deviate from this shape — AD-01)
`asset, asset_class, event_type, direction, confidence, timestamp, source, meta`
- `direction`: `{-1, 0, 1}` only — never a raw sentiment float.
- `confidence`: `[0.0, 1.0]`.
- Anything type-specific goes in `meta` (JSONB). Never add a new top-level
  field, and never branch scorer logic on `event_type`.

## SignalFactory
One typed constructor per non-news signal category: `greek_anomaly`,
`options_flow`, `strategy_break`, `on_chain_move`, `funding_extreme`,
`whale_transfer`, `macro_event`, `political_trade`, `insider_trade`,
`ml_forecast`, `ta_signal`. A **new signal type must get a weight in
CLAUDE.md §9's Signal Weight Reference table** before wiring into
`alert_payloads()` — an unweighted type silently corrupts the confluence
normalization denominator.

## UnifiedPipeline
`ingest_articles()`, `ingest_signal_events()`, `correlate(asset)`,
`correlate_all()`, `alert_payloads(threshold=0.65, min_signals=3)`. Keep it a
thin orchestrator — scoring math belongs in `correlation_scorer.py`, dedup/
sentiment in `nlp_pipeline.py`.

## Invariants
- **Append-only** (NFR-03): never mutate/delete an ingested Event; a
  correction is a new Event referencing the original via `meta`.
- **Bidirectional correlation** (AD-02): news-derived and non-news Events
  must be indistinguishable to the scorer.

## Delegate
- Deep implementation work → `event-pipeline-expert` agent.
- Pre-merge review → `event-pipeline-reviewer` agent.
