---
name: strategy-exporters
description: Reference knowledge for Trade-Dash's strategy export layer (exporters/pine_exporter.py, exporters/mql5_exporter.py) — Pine Script v5 generation and the safety-gated MQL5 EA skeleton exporter.
when_to_use: generating a Pine Script strategy/indicator from internal rules, building the MQL5 EA skeleton with signal injection, checking exported script safety
---

Trade-Dash's exporters live in `exporters/pine_exporter.py` and
`exporters/mql5_exporter.py` (see CLAUDE.md §4 AD-14, §2 FR-44/45, §8
OQ-11/12).

## Pine Script exporter (OQ-11: both strategies and indicators)
Detect rule type: backtestable entry/exit rules → `//@version=5
strategy(...)`; signal/overlay outputs → `indicator(...)`. Expose tunable
parameters via `input.*()`, not hardcoded values. Verify generated Pine v5
syntax (balanced parens, valid built-ins, current v5 function names, not
v4-era `study()`).

## MQL5 exporter (OQ-12: skeleton + signal injection ONLY by default —
safety-critical, ties to AD-10/AD-13)
Default output is an `OnTick()`/`OnInit()` skeleton with signal hooks, but
risk management and position sizing left as **explicit stubs** requiring
manual completion. **Never generate a fully-automated order-placing EA by
default** — that would let research output reach live capital without
human review. A `--full-ea` flag may opt in to complete generation for
advanced users, with generated risk logic clearly commented "REVIEW BEFORE
LIVE USE."

## Shared convention
Both exporters take the same internal rule-object shape as input; every
exported script carries a generated-timestamp header comment.

## Delegate
- Deep implementation work → `strategy-exporters-expert` agent.
- Pre-merge review → `strategy-exporters-reviewer` agent (applies extra
  scrutiny to the MQL5 safety gate).
