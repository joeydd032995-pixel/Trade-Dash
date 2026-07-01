---
name: intelligence-briefing
description: Reference knowledge for Trade-Dash's intelligence-ops layer (intelligence_brief_service.py, freshness_guard.py) — AI-synthesized briefs with source attribution and the mandatory feed-freshness gate.
when_to_use: implementing freshness_guard.py, wiring the freshness gate before scoring/briefing jobs, building scheduled or event-triggered briefs
---

Trade-Dash's intelligence-ops layer lives in `intelligence_brief_service.py`
and `freshness_guard.py` (see CLAUDE.md §4 AD-15, §2 FR-46..48, §8 OQ-14).

## freshness_guard.py — the prerequisite gate, build first
Checks feed staleness **before any briefing or scoring job runs** (AD-15,
hard requirement, no exceptions for "urgent" event-triggered paths). Use
per-source-group thresholds, not one global threshold — different feed
types have different acceptable staleness windows.

## intelligence_brief_service.py
Input: scored Events + freshness-validated feeds. Output: brief with
**source attribution on every claim** (auditability) and cross-stream
correlation (FR-48) — reuse `CorrelationScorer`'s `agreement_ratio`/
`conflict_ratio`/dark signal queue rather than re-deriving correlation
logic.

## Cadence (OQ-14: scheduled baseline + event-triggered supplements)
Scheduled: pre-market 6:30 AM CDT, mid-day 12:00 PM CDT, post-market 5:00 PM
CDT. Event-triggered: **only** when `severity == critical AND signal_count
>= 5` (strict AND — don't loosen either half). Both cadences pass through
`freshness_guard.py` identically.

## Delegate
- Deep implementation work → `intelligence-briefing-expert` agent.
- Pre-merge review → `intelligence-briefing-reviewer` agent.
