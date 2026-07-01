"""Weighted, time-decayed, directional confluence scorer.

Implements CLAUDE.md AD-04's confluence formula:

    C = Σ_i(w_i · s_i · d_i · δ_i) / Σ_i|w_i · s_i · δ_i|

where for each contributing `Event` i:
    w_i = signal type weight (SIGNAL_WEIGHTS, CLAUDE.md §9)
    s_i = Event.confidence, in [0.0, 1.0]
    d_i = Event.direction, in {-1, 0, 1}
    δ_i = exp(-ln(2) / half_life_min * age_min)  (AD-03 exponential decay)

`C` is bounded to [-1.0, 1.0] by construction, since the numerator is a
direction-signed sum of the same nonnegative terms (w·s·δ) whose absolute
values make up the denominator.

This module is intentionally storage-agnostic (per the correlation-scorer
skill / OQ-01 / OQ-06): it is pure functions in, `CorrelationResult` out. It
does not decide *when* to persist a score, at what cadence, or how
materialized views refresh — that is `database-schema-expert`/`api_service.py`
territory. Every function here is safe to call as often as you like; nothing
here writes to disk, a database, or a cache.

Design principle 2 / AD-02 (bidirectional correlation, dark signals):
informed money often moves before public information is available. This
module treats "large statistical signal, zero corresponding news" as the
single highest-priority output — see `detect_dark_signals`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from event_types import Event

# ---------------------------------------------------------------------------
# Signal weights (CLAUDE.md §9 Signal Weight Reference, AD-05)
# ---------------------------------------------------------------------------

#: Baseline signal type weights. Hardcoded intentionally — a new signal type
#: must be explicitly registered here (in coordination with SignalFactory /
#: event-pipeline-expert) before it can be scored. `correlate()` raises
#: loudly on an unrecognized `event_type` rather than silently defaulting to
#: 1.0 (see the correlation-scorer skill's documented anti-pattern warning).
SIGNAL_WEIGHTS: dict[str, float] = {
    "whale_transfer": 1.7,
    "options_flow": 1.6,
    "on_chain_move": 1.5,
    "funding_extreme": 1.5,
    "greek_anomaly": 1.4,
    "insider_trade": 1.4,
    "ml_forecast": 1.3,
    "strategy_break": 1.3,
    "macro_event": 1.2,
    "ta_signal": 1.1,
    "political_trade": 1.1,
    "news": 1.0,
}


class UnknownSignalTypeError(ValueError):
    """Raised when an `Event.event_type` has no entry in `SIGNAL_WEIGHTS`.

    Per CLAUDE.md's correlation-scorer skill: "New signal types get their
    weight from SignalFactory/event-pipeline-expert coordination — never
    hardcode a weight fallback here; missing weights should raise loudly,
    not silently default to 1.0." A silent `1.0` fallback would let an
    unregistered signal type quietly enter the confluence formula with an
    arbitrary weight, corrupting the score without any visible error.
    """


def get_signal_weight(event_type: str) -> float:
    """Look up the confluence weight for `event_type`.

    Raises `UnknownSignalTypeError` (a `ValueError` subclass) if `event_type`
    is not registered in `SIGNAL_WEIGHTS` — never silently defaults.
    """
    try:
        return SIGNAL_WEIGHTS[event_type]
    except KeyError as exc:
        raise UnknownSignalTypeError(
            f"No signal weight registered for event_type={event_type!r}. "
            "New signal types must be added to SIGNAL_WEIGHTS (CLAUDE.md §9) "
            "in coordination with SignalFactory before they can be scored — "
            "refusing to silently default to weight=1.0."
        ) from exc


# ---------------------------------------------------------------------------
# Half-life (CLAUDE.md AD-03 / OQ-02 — fixed baseline only, no ATR-adaptive)
# ---------------------------------------------------------------------------

#: Minutes. OQ-02's resolved verdict is a fixed baseline for Phase 1;
#: ATR-volatility-adaptive half-life (`half_life = base / (1 + k * atr_z)`)
#: is explicitly deferred to Phase 3 pending a live ATR pipeline. Do not
#: implement that here even though it looks like a natural extension.
EQUITY_HALF_LIFE_MIN = 180.0
CRYPTO_HALF_LIFE_MIN = 90.0

_LN2 = math.log(2)


def get_half_life_minutes(asset_class: str) -> float:
    """Return the fixed baseline half-life (minutes) for `asset_class`.

    Crypto uses the 90-minute baseline; everything else (equity, forex,
    futures) uses the 180-minute equity baseline — CLAUDE.md does not carve
    out a distinct half-life for forex/futures, so per this module's task
    spec they are treated like equities for Phase 1 rather than guessing at
    an unstated value.
    """
    if asset_class == "crypto":
        return CRYPTO_HALF_LIFE_MIN
    return EQUITY_HALF_LIFE_MIN


def compute_decay(age_minutes: float, half_life_minutes: float) -> float:
    """Exponential time decay: `exp(-ln(2) / half_life_minutes * age_minutes)`.

    Returns 1.0 at `age_minutes == 0` and 0.5 at `age_minutes ==
    half_life_minutes`, per AD-03. `age_minutes` is clamped to be
    nonnegative — an Event timestamped slightly in the "future" relative to
    `now` (e.g. clock skew) should not produce decay > 1.0.
    """
    age_minutes = max(0.0, age_minutes)
    return math.exp(-_LN2 / half_life_minutes * age_minutes)


# ---------------------------------------------------------------------------
# Per-signal audit trail + result shape (FR-07..11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeightedSignal:
    """Full per-signal audit trail entry for one contributing `Event`.

    Every derived confluence score must be traceable back to the raw source
    Events that produced it (design principle 4 / AD-08 auditability) — this
    is that traceability record. Never drop or compress this to save space.
    """

    event: Event
    weight: float
    confidence: float
    direction: int
    age_minutes: float
    half_life_minutes: float
    decay: float
    contribution: float  # w_i * s_i * d_i * delta_i
    abs_weighted_confidence: float  # |w_i * s_i * delta_i| (denominator term)


@dataclass(frozen=True)
class CorrelationResult:
    """Output of `CorrelationScorer.correlate()` for one asset (FR-07..11).

    Attributes:
        asset: The asset symbol this result was computed for.
        confluence_score: `C` in [-1.0, 1.0]. 0.0 if the denominator
            Σ|w·s·δ| is zero (no events, or all events had
            weight*confidence*decay == 0) — see `correlate()` docstring for
            why this is a documented edge case, not an error.
        weighted_signals: Full per-signal audit trail (`WeightedSignal` per
            contributing Event) — weight, decay, and contribution for every
            Event considered (FR-09).
        agreement_ratio: Fraction of signals whose direction matches the
            overall sign of `confluence_score` (see docstring below for the
            exact tie-breaking convention used when C == 0 or d_i == 0).
        conflict_ratio: Fraction of signals whose direction opposes the
            overall sign of `confluence_score`.
        signal_count: Number of Events considered for this asset that assert
            an actual directional stance (`direction != 0`) — i.e. the count
            used to gate FR-19's `signal_count >= min_signals` alerting
            requirement, which is meant to require multiple *corroborating*
            signals, not merely multiple Events. A `direction == 0` (neutral)
            Event — however high its `confidence`/`weight` — asserts no
            directional evidence and must not be able to satisfy this gate
            on its own (review finding: a single direction-neutral,
            urgency-only news Event could previously pad `signal_count` from
            2 to 3 and flip an alert from blocked to firing, despite
            contributing nothing directional). `weighted_signals` still
            contains the FULL audit trail including neutral-direction
            Events — this field only affects the *count* used for gating,
            never what's recorded or how `confluence_score` itself is
            computed (AD-04's formula is unchanged: a neutral Event still
            legitimately dilutes the denominator, since "I read this and
            it's genuinely unclear" is real information about conviction).
    """

    asset: str
    confluence_score: float
    weighted_signals: list[WeightedSignal]
    agreement_ratio: float
    conflict_ratio: float
    signal_count: int


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# CorrelationScorer
# ---------------------------------------------------------------------------


class CorrelationScorer:
    """Computes the weighted, time-decayed, directional confluence score.

    Pure and storage-agnostic per AD-08/design principle 4 and the
    correlation-scorer skill: `correlate()` takes a list of `Event`s and
    returns a `CorrelationResult`; it never reads from or writes to a
    database, cache, or file. Callers (e.g. `unified_pipeline.py`,
    `api_service.py`) own persistence and storage cadence decisions
    (OQ-01/OQ-06).
    """

    def correlate(
        self,
        asset: str,
        events: list[Event],
        now: datetime | None = None,
    ) -> CorrelationResult:
        """Compute the confluence score for `asset` from `events`.

        Only Events where `event.asset == asset` are considered; callers may
        pass a mixed multi-asset list and this method will filter it down.

        `now` defaults to `datetime.now(timezone.utc)` when omitted; it
        exists as an explicit parameter so callers (and tests) can pin the
        "current" time for deterministic, reproducible decay calculations.

        Edge case (documented, not an error): if there are no matching
        Events, or every matching Event contributes `w*s*delta == 0` (e.g.
        confidence == 0.0 for every event), the denominator Σ|w·s·δ| is
        zero. Rather than raising `ZeroDivisionError`, this returns
        `confluence_score=0.0` — "no signal" is a meaningful, valid answer
        for an asset with no actionable evidence either way, not a fault
        condition. In that case `agreement_ratio` and `conflict_ratio` are
        both 0.0 (no signals to agree or conflict).

        Raises:
            UnknownSignalTypeError: if any matching Event's `event_type` has
                no entry in `SIGNAL_WEIGHTS`. This is intentionally not
                caught/skipped — an unregistered signal type silently
                entering (or silently being dropped from) a score would be
                worse than a loud failure.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        asset_events = [e for e in events if e.asset == asset]

        weighted_signals: list[WeightedSignal] = []
        numerator = 0.0
        denominator = 0.0

        for ev in asset_events:
            weight = get_signal_weight(ev.event_type)  # raises loudly if unknown
            half_life = get_half_life_minutes(ev.asset_class)
            age_minutes = (now - ev.timestamp).total_seconds() / 60.0
            decay = compute_decay(age_minutes, half_life)

            weighted_confidence_decay = weight * ev.confidence * decay
            contribution = weighted_confidence_decay * ev.direction
            abs_term = abs(weighted_confidence_decay)

            numerator += contribution
            denominator += abs_term

            weighted_signals.append(
                WeightedSignal(
                    event=ev,
                    weight=weight,
                    confidence=ev.confidence,
                    direction=ev.direction,
                    age_minutes=age_minutes,
                    half_life_minutes=half_life,
                    decay=decay,
                    contribution=contribution,
                    abs_weighted_confidence=abs_term,
                )
            )

        if denominator == 0.0:
            confluence_score = 0.0
        else:
            confluence_score = numerator / denominator
            # Clamp defensively against float rounding drift pushing
            # marginally outside [-1.0, 1.0] (e.g. -1.0000000000000002).
            confluence_score = max(-1.0, min(1.0, confluence_score))

        agreement_ratio, conflict_ratio = _agreement_and_conflict_ratios(
            weighted_signals, confluence_score
        )

        # signal_count deliberately excludes direction == 0 (neutral) Events
        # — see CorrelationResult.signal_count's docstring. This does NOT
        # affect confluence_score/weighted_signals: neutral Events are still
        # fully included in the formula and the audit trail above.
        directional_signal_count = sum(
            1 for ev in asset_events if ev.direction != 0
        )

        return CorrelationResult(
            asset=asset,
            confluence_score=confluence_score,
            weighted_signals=weighted_signals,
            agreement_ratio=agreement_ratio,
            conflict_ratio=conflict_ratio,
            signal_count=directional_signal_count,
        )


def _agreement_and_conflict_ratios(
    weighted_signals: list[WeightedSignal], confluence_score: float
) -> tuple[float, float]:
    """Compute `agreement_ratio` / `conflict_ratio` for a scored asset.

    Definition used here (documented per FR-10's "or some similarly
    reasonable definition — document exactly what you compute"):

    - `overall_sign = sign(confluence_score)` (one of -1, 0, +1).
    - A signal "agrees" if `sign(event.direction) == overall_sign` AND
      `overall_sign != 0` (a neutral/zero overall score has no directional
      claim for anything to agree *with*).
    - A signal "conflicts" if `sign(event.direction) == -overall_sign` AND
      `overall_sign != 0`.
    - Neutral-direction Events (`direction == 0`) never count as agreeing or
      conflicting (they aren't a directional claim at all).
    - `agreement_ratio = agreeing_count / signal_count`,
      `conflict_ratio = conflicting_count / signal_count`.
    - If there are no signals, or `overall_sign == 0` (including the
      zero-denominator edge case), both ratios are 0.0.

    Ratios are fractions of *all* signals considered (not just directional
    ones), so `agreement_ratio + conflict_ratio <= 1.0`, with the remainder
    made up of neutral-direction signals (and, when `overall_sign == 0`,
    every signal falls into that remainder).
    """
    total = len(weighted_signals)
    if total == 0:
        return 0.0, 0.0

    overall_sign = _sign(confluence_score)
    if overall_sign == 0:
        return 0.0, 0.0

    agreeing = 0
    conflicting = 0
    for ws in weighted_signals:
        d_sign = _sign(ws.direction)
        if d_sign == 0:
            continue
        if d_sign == overall_sign:
            agreeing += 1
        else:
            conflicting += 1

    return agreeing / total, conflicting / total


# ---------------------------------------------------------------------------
# Dark signal queue (AD-02, design principle 2 — HIGHEST PRIORITY output)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DarkSignal:
    """One flagged 'dark signal' asset: strong statistical evidence, no news.

    Per AD-02: informed money often moves before public information is
    available, so an asset with a large-magnitude statistical signal and
    zero corresponding `news`-type Events is the highest-edge case in the
    whole confluence system — not noise to be filtered out. This is
    surfaced as first-class output, not buried inside `CorrelationResult`.
    """

    asset: str
    total_abs_weighted_confidence: float  # Σ|w·s·δ| over non-news Events
    non_news_signal_count: int
    contributing_event_types: tuple[str, ...]


def detect_dark_signals(
    events: list[Event],
    *,
    now: datetime | None = None,
    magnitude_threshold: float = 1.0,
) -> list[DarkSignal]:
    """Flag assets with high-magnitude non-news signals and zero news Events.

    For each distinct asset present in `events`:
      1. Compute Σ|w_i · s_i · δ_i| over that asset's non-news Events only
         (this magnitude term mirrors the denominator of the confluence
         formula, i.e. how much decayed, weighted, confidence-scaled
         statistical evidence exists for the asset).
      2. If that total is >= `magnitude_threshold` AND the asset has zero
         `event_type == "news"` Events, it is a dark signal — flagged
         regardless of the statistical signals' direction/agreement, since
         AD-02's point is that *any* strong unexplained statistical move is
         high-edge, not just directionally coherent ones.

    `magnitude_threshold` is deliberately a tunable keyword (not a magic
    hardcoded constant) since "high-magnitude" has no single canonical value
    in CLAUDE.md — callers (e.g. the eventual scoring loop in
    `unified_pipeline.py`/`api_service.py`) may calibrate this per
    deployment. The default of `1.0` is chosen so that a single
    high-confidence, high-weight, freshly-decayed statistical signal (e.g.
    one `whale_transfer` at confidence=0.8, decay~1.0 => ~1.36) already
    clears it, matching AD-02's framing that even one strong unexplained
    signal is worth flagging.

    Returns a list of `DarkSignal` — full audit context (not just a bare
    asset symbol list) so this is at least as auditable as
    `CorrelationResult` (design principle 4), while still being usable as
    `[d.asset for d in detect_dark_signals(events)]` if only symbols are
    needed.

    Raises:
        UnknownSignalTypeError: if any Event's `event_type` has no entry in
            `SIGNAL_WEIGHTS` (same loud-failure contract as `correlate()`).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    assets = {e.asset for e in events}
    dark_signals: list[DarkSignal] = []

    for asset in assets:
        asset_events = [e for e in events if e.asset == asset]
        has_news = any(e.event_type == "news" for e in asset_events)
        if has_news:
            continue

        non_news_events = [e for e in asset_events if e.event_type != "news"]
        if not non_news_events:
            continue

        total_abs = 0.0
        event_types: list[str] = []
        for ev in non_news_events:
            weight = get_signal_weight(ev.event_type)  # raises loudly if unknown
            half_life = get_half_life_minutes(ev.asset_class)
            age_minutes = (now - ev.timestamp).total_seconds() / 60.0
            decay = compute_decay(age_minutes, half_life)
            total_abs += abs(weight * ev.confidence * decay)
            event_types.append(ev.event_type)

        if total_abs >= magnitude_threshold:
            dark_signals.append(
                DarkSignal(
                    asset=asset,
                    total_abs_weighted_confidence=total_abs,
                    non_news_signal_count=len(non_news_events),
                    contributing_event_types=tuple(event_types),
                )
            )

    # Highest-magnitude dark signals first — this is the highest-priority
    # output of the scorer (AD-02), so present the most urgent ones first.
    dark_signals.sort(key=lambda d: d.total_abs_weighted_confidence, reverse=True)
    return dark_signals


__all__ = [
    "SIGNAL_WEIGHTS",
    "UnknownSignalTypeError",
    "get_signal_weight",
    "EQUITY_HALF_LIFE_MIN",
    "CRYPTO_HALF_LIFE_MIN",
    "get_half_life_minutes",
    "compute_decay",
    "WeightedSignal",
    "CorrelationResult",
    "CorrelationScorer",
    "DarkSignal",
    "detect_dark_signals",
]
