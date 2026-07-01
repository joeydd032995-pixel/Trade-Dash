---
name: correlation-scorer
description: Reference knowledge for Trade-Dash's confluence scoring engine (correlation_scorer.py) — the weighted, time-decayed, directional formula, half-life decay, dark signal queue, and CorrelationResult audit trail.
when_to_use: implementing or debugging CorrelationScorer, tuning half-life decay, computing agreement_ratio/conflict_ratio, building the dark signal queue
---

Trade-Dash's confluence scorer lives in `correlation_scorer.py` (see
CLAUDE.md §4 AD-03/04/05, §5 Formulas, §8 OQ-01/02/03/06, §9).

## The formula (exact)
```
C = Σ_i(w_i · s_i · d_i · δ_i) / Σ_i|w_i · s_i · δ_i|
  δ_i = exp(-ln(2) / half_life_min · age_min)
Output: C ∈ [-1, 1]. Never a raw sum or plain average — the abs-value
denominator is load-bearing (AD-04).
```

## Half-life (AD-03, OQ-02)
Equities 180 min, crypto 60–90 min (90 baseline). **Fixed baseline only** —
ATR-adaptive half-life is explicitly deferred to Phase 3; don't implement it
unless asked.

## Signal weights (§9)
`whale_transfer 1.7, options_flow 1.6, on_chain_move 1.5, funding_extreme
1.5, greek_anomaly 1.4, insider_trade 1.4, ml_forecast 1.3, strategy_break
1.3, macro_event 1.2, ta_signal 1.1, political_trade 1.1, news 1.0`. A new
signal type with no weight must raise, never silently default to 1.0.

## Required outputs (FR-07..11)
`CorrelationResult`: `confluence_score`, `weighted_signals` (full per-signal
audit trail — weight, decay, contribution), `agreement_ratio`,
`conflict_ratio`, `signal_count`. Never drop the audit trail.

## Dark signal queue (AD-02, highest priority output)
Assets with high-magnitude signals and zero `news`-type Events — this is the
most edge-rich category, not noise to filter.

## Storage
This module is storage-agnostic — pure functions in, `CorrelationResult`
out. Storage cadence (store all scores, OQ-06) and hot-cache/materialized
views (OQ-01) are `database-schema`'s concern.

## Delegate
- Deep implementation work → `correlation-scorer-expert` agent.
- Pre-merge review → `correlation-scorer-reviewer` agent.
