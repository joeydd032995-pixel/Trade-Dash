---
name: connector-layer
description: Reference knowledge for Trade-Dash's bounded live-execution layer (connector_profiles.py) — paper/live/read-only profiles, mandate controls, audit ledger, instant halt, and broker adapters. Highest-stakes module in the repo.
when_to_use: building a connector profile, wiring the audit ledger, implementing the instant halt, adding a broker adapter (IBKR/Alpaca/Bybit/OKX)
---

Trade-Dash's connector layer lives in `connector_profiles.py` (see CLAUDE.md
§4 AD-10/AD-13, §2 FR-60..62, NFR-04/09). **This is the only layer that can
touch real capital — treat every constraint below as a hard gate, not a
guideline.**

## Structural safety, not procedural (AD-13)
- Paper is the default; live mode must be an explicit, unambiguous opt-in —
  never inferred or defaulted on.
- Mandates (max position size, max daily loss, allowed symbols/order types)
  are enforced **in code**, rejecting orders before they reach the broker —
  never log-and-proceed.
- Read-only profile has **no reachable code path** to order placement — a
  separate class/interface, not a flag check inside a shared class.

## Audit ledger (FR-61)
Every action (order, cancel, mandate check, halt) recorded append-only,
written before/atomically with the action — never fire-and-forget logging
that could be lost on crash.

## Instant halt (FR-62)
Checked immediately before every broker API call (not just loop start);
idempotent under repeated invocation.

## Boundary (AD-10, NFR-04)
No other module may call a broker API directly — everything routes through
this layer. If you find direct broker calls elsewhere in the repo, flag it
for a design conversation rather than silently rerouting it.

## Working style
When a request implies loosening a mandate, removing a halt check, or
defaulting toward live — **stop and ask the user explicitly** rather than
implementing it.

## Delegate
- Deep implementation work → `connector-layer-expert` agent.
- Pre-merge review → `connector-layer-reviewer` agent (apply maximum
  scrutiny; re-verify before declaring a diff clean).
