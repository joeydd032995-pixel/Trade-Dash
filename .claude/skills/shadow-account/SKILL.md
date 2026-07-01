---
name: shadow-account
description: Reference knowledge for Trade-Dash's trade journal and shadow account module (shadow_account.py) — broker export ingestion, behavioral diagnostics, rule extraction, and shadow-strategy comparison.
when_to_use: parsing a new broker export format (IBKR/Alpaca/Tastytrade), building behavioral diagnostics, inferring entry/exit rules from journal history
---

Trade-Dash's shadow account module lives in `shadow_account.py` (see
CLAUDE.md §5, §2 FR-40..43).

## Pipeline
parse (broker CSV) → behavioral diagnostics → rule extraction → shadow
strategy backtest → comparison report → `trade_journal` DB rows.

## Broker parsing
Normalize IBKR/Alpaca/Tastytrade formats to one common trade-record shape
(symbol, side, quantity, entry/exit price+timestamp, fees, account tag) —
never branch broker-specific logic downstream of parsing. Validate
non-negative quantity, exit-after-entry timestamps, positive prices before
diagnostics runs (partial fills / corporate actions are common real-world
noise).

## Rule extraction (FR-41)
Human-readable inferred rules ("exits cluster within 2% of round numbers"),
not an opaque classifier.

## Shadow comparison (FR-42)
Reuse the same expectancy formula as backtesting
(`E = (W × R_win) − (L × R_loss)`) rather than reimplementing it.

## Reports (FR-43)
2-3 concrete, plain-language findings — not a metrics dump.

## Delegate
- Deep implementation work → `shadow-account-expert` agent.
- Pre-merge review → `shadow-account-reviewer` agent.
