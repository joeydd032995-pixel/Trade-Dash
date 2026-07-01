---
name: ml-forecast-lab
description: Reference knowledge for Trade-Dash's ML forecasting lab (forecast_lab.py) — LSTM/RNN/ARIMA/sklearn baselines fused with sentiment features, emitting typed ml_forecast Events with strict lookahead prevention.
when_to_use: building or training a forecasting model, wiring sentiment-feature fusion, checking a feature pipeline for lookahead leakage
---

Trade-Dash's ML forecasting lab lives in `forecast_lab.py` (see CLAUDE.md
§4 AD-12, §2 FR-15..18, §8 OQ-08).

## Pipeline
load → preprocess → feature extraction → sentiment fusion → normalize →
train → evaluate → emit.

## Models (FR-15)
LSTM/RNN (`torch.nn.LSTM`/`keras.layers.LSTM`), ARIMA
(`statsmodels.tsa.arima.model.ARIMA` or `pmdarima.auto_arima`), sklearn
regression baselines. Always keep ARIMA/sklearn running as sanity-check
baselines alongside deep models.

## Swap-ready interface (AD-12) — the whole point of this module
Every model backend emits the same `ml_forecast` Event (weight **1.3**, §9)
via the same function signature. `UnifiedPipeline`/`CorrelationScorer` must
never need to know which model produced a forecast.

## Lookahead prevention (OQ-08) — apply maximum scrutiny here
- Strict trailing windows only (no centered rolling windows).
- Sentiment features joined by publish timestamp ≤ t, not calendar date.
- Chronological train/test splits only — never a random shuffle split on
  time-series data.
- Normalization (scaler) fit on train only, then applied to test.
- Backtest-time integration must replay strictly forward in time.

## Delegate
- Deep implementation work → `ml-forecast-lab-expert` agent.
- Pre-merge review → `ml-forecast-lab-reviewer` agent.
