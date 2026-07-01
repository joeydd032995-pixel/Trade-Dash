---
name: backtesting-harness
description: Reference knowledge for Trade-Dash's backtesting harness — vectorbt-based signal replay, walk-forward validation, expectancy computation, and the live-deployment gate.
when_to_use: implementing chronological event replay, computing expectancy per setup×regime, building walk-forward windows, wiring the deployment gate
---

Trade-Dash's backtesting harness (see CLAUDE.md §4 AD-09, §2 FR-28..34, §5
Formulas, §8 OQ-04).

## Formulas (exact)
```
Expectancy:      E = (W × R_win) − (L × R_loss)
Deployment Gate: trade_count ≥ 30  AND  Sharpe ≥ 1.0
                 AND  max_drawdown ≤ 0.15  AND  walk_forward_consistency ≥ 0.70
```
The gate is a **strict AND** — 3 of 4 passing is a fail. Never loosen
thresholds silently; they're policy (AD-09), not tuning knobs.

## Replay (FR-28/29) — lookahead is the #1 bug class
Replay `events` **chronologically**, never peeking ahead. Tag every
simulated trade by setup, regime, catalyst, venue — expectancy must be
computable per setup × regime, not just in aggregate (FR-30).

## Walk-forward (FR-31)
60-day train / 20-day test rolling windows. Use `vectorbt`'s
`Portfolio.from_signals()` rather than a hand-rolled bar-by-bar loop.

## OQ-04 (both, per resolved verdict)
Nightly cron (~2 AM CDT) for full portfolio runs; on-demand
`POST /backtest/run` (Celery, poll `GET /backtest/{run_id}`) for spot
checks.

## Run cards (NFR-07/10)
Must include the per-condition gate breakdown (not just pass/fail overall)
and enough config detail to reproduce the exact run.

## Delegate
- Deep implementation work → `backtesting-harness-expert` agent.
- Pre-merge review → `backtesting-harness-reviewer` agent.
