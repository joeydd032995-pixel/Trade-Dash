---
name: ta-engine
description: Reference knowledge for Trade-Dash's technical analysis engine (ta_engine.py) — RSI, MACD, Ichimoku, SMA, EMA, MFI, OBV, VWAP, Momentum in both batch and streaming modes.
when_to_use: adding a new indicator, fixing streaming-vs-batch numeric mismatches, wiring ta_signal alerts into the confluence pipeline
---

Trade-Dash's TA engine lives in `ta_engine.py` (see CLAUDE.md §5, §2
FR-12..14, §6).

## Indicators (FR-12)
RSI, MACD, Ichimoku Cloud (tenkan/kijun/senkou A&B/chikou), SMA, EMA, MFI,
OBV, VWAP, Momentum — both batch (DataFrame in/out) and streaming
(per-candle) modes.

## Library specifics
- `TA-Lib` gives RSI/MACD/SMA/EMA/MFI/OBV natively (`talib.RSI`, etc.) —
  requires the system C library (`apt-get install ta-lib` or build from
  source). Prefer it over hand-rolled math.
- Ichimoku and VWAP aren't in TA-Lib — implement directly. Senkou spans
  plot 26 periods *ahead*, chikou span 26 periods *behind* — an off-by-one
  here silently misaligns the cloud.
- VWAP needs an explicit session-reset decision (daily reset vs. continuous)
  — don't silently accumulate across days.

## Batch/streaming parity — the main bug class
Incremental (streaming) and batch implementations must produce numerically
identical output for the same input sequence. Always test this explicitly.
Respect warm-up periods (e.g. RSI-14 needs 14+ candles) — streaming mode
must not emit before warm-up completes.

## Alerts (FR-13)
Each trigger emits a `ta_signal` Event via `SignalFactory`, weight **1.1**
(§9) — TA confirms, it doesn't initiate; don't weight it higher. Delivery
fan-out is `event-bus`'s `AlertRouter`, not this module's job.

## Delegate
- Deep implementation work → `ta-engine-expert` agent.
- Pre-merge review → `ta-engine-reviewer` agent.
