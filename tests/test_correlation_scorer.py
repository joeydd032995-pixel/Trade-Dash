"""Tests for correlation_scorer.py — the confluence formula (AD-04), fixed
half-life decay (AD-03/OQ-02), the per-signal audit trail (FR-07..11), and
the dark signal queue (AD-02, design principle 2).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from correlation_scorer import (
    CorrelationScorer,
    UnknownSignalTypeError,
    compute_decay,
    detect_dark_signals,
    get_half_life_minutes,
    get_signal_weight,
)
from event_types import Event

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event(**overrides) -> Event:
    defaults = dict(
        asset="BTC",
        asset_class="crypto",
        event_type="whale_transfer",
        direction=1,
        confidence=0.8,
        timestamp=NOW,
        source="arkham",
        meta={},
    )
    defaults.update(overrides)
    return Event(**defaults)


# ---------------------------------------------------------------------------
# Half-life selection (AD-03 / OQ-02)
# ---------------------------------------------------------------------------


def test_crypto_half_life_is_90_minutes():
    assert get_half_life_minutes("crypto") == 90.0


def test_equity_half_life_is_180_minutes():
    assert get_half_life_minutes("equity") == 180.0


@pytest.mark.parametrize("asset_class", ["forex", "futures"])
def test_forex_and_futures_default_to_equity_half_life(asset_class):
    # CLAUDE.md does not carve out a distinct half-life for forex/futures;
    # per this module's task spec they're treated like equities (180 min)
    # for Phase 1 rather than guessing at an unstated value.
    assert get_half_life_minutes(asset_class) == 180.0


# ---------------------------------------------------------------------------
# Decay curve (AD-03)
# ---------------------------------------------------------------------------


def test_decay_is_1_at_age_zero():
    assert compute_decay(0.0, 90.0) == pytest.approx(1.0)


def test_decay_is_half_at_exactly_one_half_life():
    assert compute_decay(90.0, 90.0) == pytest.approx(0.5)
    assert compute_decay(180.0, 180.0) == pytest.approx(0.5)


def test_decay_is_quarter_at_two_half_lives():
    assert compute_decay(180.0, 90.0) == pytest.approx(0.25)


def test_decay_formula_matches_exact_definition():
    age = 45.0
    half_life = 90.0
    expected = math.exp(-math.log(2) / half_life * age)
    assert compute_decay(age, half_life) == pytest.approx(expected)


def test_decay_clamps_negative_age_to_zero():
    # Slight clock skew (Event timestamped marginally after `now`) should
    # not produce decay > 1.0.
    assert compute_decay(-5.0, 90.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Signal weight lookup (AD-05, §9 table) — raise loudly on unknown types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type,expected_weight",
    [
        ("whale_transfer", 1.7),
        ("options_flow", 1.6),
        ("on_chain_move", 1.5),
        ("funding_extreme", 1.5),
        ("greek_anomaly", 1.4),
        ("insider_trade", 1.4),
        ("ml_forecast", 1.3),
        ("strategy_break", 1.3),
        ("macro_event", 1.2),
        ("ta_signal", 1.1),
        ("political_trade", 1.1),
        ("news", 1.0),
    ],
)
def test_signal_weights_match_claude_md_table(event_type, expected_weight):
    assert get_signal_weight(event_type) == expected_weight


def test_unknown_event_type_raises_loudly():
    with pytest.raises(UnknownSignalTypeError):
        get_signal_weight("some_brand_new_signal_type")


def test_correlate_raises_on_unknown_event_type_in_events_list():
    # Event.event_type is a plain str field (not a hard enum per
    # event_types.py), so it's possible to construct one with a type that
    # isn't yet registered in SIGNAL_WEIGHTS. correlate() must raise rather
    # than silently default to weight=1.0.
    ev = make_event(event_type="totally_unregistered_type")
    scorer = CorrelationScorer()
    with pytest.raises(UnknownSignalTypeError):
        scorer.correlate("BTC", [ev], now=NOW)


def test_detect_dark_signals_raises_on_unknown_event_type():
    ev = make_event(event_type="totally_unregistered_type")
    with pytest.raises(UnknownSignalTypeError):
        detect_dark_signals([ev], now=NOW)


# ---------------------------------------------------------------------------
# Formula correctness — hand-computed fixture
# ---------------------------------------------------------------------------


def test_single_event_hand_computed_score():
    # w=1.7 (whale_transfer), s=0.5, d=+1, age=90min, crypto half_life=90min
    # => delta = exp(-ln2/90 * 90) = exp(-ln2) = 0.5
    # numerator = 1.7 * 0.5 * 1 * 0.5 = 0.425
    # denominator = |1.7 * 0.5 * 0.5| = 0.425
    # C = 0.425 / 0.425 = 1.0
    ev = make_event(
        event_type="whale_transfer",
        confidence=0.5,
        direction=1,
        asset_class="crypto",
        timestamp=NOW - timedelta(minutes=90),
    )
    scorer = CorrelationScorer()
    result = scorer.correlate("BTC", [ev], now=NOW)
    assert result.confluence_score == pytest.approx(1.0)

    ws = result.weighted_signals[0]
    assert ws.weight == 1.7
    assert ws.decay == pytest.approx(0.5)
    assert ws.contribution == pytest.approx(0.425)
    assert ws.abs_weighted_confidence == pytest.approx(0.425)


def test_two_agreeing_events_hand_computed_score():
    # Event A: w=1.7, s=0.5, d=+1, age=0 -> delta=1.0 -> term = 0.85
    # Event B: w=1.0 (news), s=0.5, d=+1, age=0 -> delta=1.0 -> term = 0.5
    # numerator = 0.85 + 0.5 = 1.35
    # denominator = 0.85 + 0.5 = 1.35
    # C = 1.0 (both agree fully, so C should saturate to +1.0)
    ev_a = make_event(
        event_type="whale_transfer", confidence=0.5, direction=1, timestamp=NOW
    )
    ev_b = make_event(
        event_type="news",
        confidence=0.5,
        direction=1,
        asset_class="crypto",
        timestamp=NOW,
        source="finnhub",
    )
    scorer = CorrelationScorer()
    result = scorer.correlate("BTC", [ev_a, ev_b], now=NOW)
    assert result.confluence_score == pytest.approx(1.0)
    assert result.signal_count == 2
    assert result.agreement_ratio == pytest.approx(1.0)
    assert result.conflict_ratio == pytest.approx(0.0)


def test_single_signal_normalizes_to_full_magnitude_regardless_of_weight():
    # With only one signal type present, the weight cancels between
    # numerator and denominator: |C| should be exactly 1.0 (direction sign)
    # for any single strong, non-neutral-direction event, regardless of its
    # weight -- this is the AD-04 normalization property under test.
    for event_type in ["whale_transfer", "news", "ta_signal", "macro_event"]:
        ev = make_event(
            event_type=event_type,
            confidence=0.9,
            direction=1,
            asset_class="equity",
            timestamp=NOW,
        )
        scorer = CorrelationScorer()
        result = scorer.correlate("BTC", [ev], now=NOW)
        assert result.confluence_score == pytest.approx(1.0), event_type

    # And for a bearish single event, C should be exactly -1.0.
    ev = make_event(
        event_type="options_flow",
        confidence=0.9,
        direction=-1,
        asset_class="equity",
        timestamp=NOW,
    )
    scorer = CorrelationScorer()
    result = scorer.correlate("BTC", [ev], now=NOW)
    assert result.confluence_score == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Conflicting signals — normalization denominator behavior
# ---------------------------------------------------------------------------


def test_conflicting_signals_net_toward_zero_with_high_conflict_ratio():
    # Two equal-magnitude, opposite-direction signals of the *same* type/
    # weight/confidence/age must fully cancel to C == 0.0, with maximal
    # conflict.
    ev_bull = make_event(
        event_type="options_flow",
        confidence=0.7,
        direction=1,
        asset_class="equity",
        timestamp=NOW,
        asset="NVDA",
    )
    ev_bear = make_event(
        event_type="options_flow",
        confidence=0.7,
        direction=-1,
        asset_class="equity",
        timestamp=NOW,
        asset="NVDA",
    )
    scorer = CorrelationScorer()
    result = scorer.correlate("NVDA", [ev_bull, ev_bear], now=NOW)
    assert result.confluence_score == pytest.approx(0.0)
    assert result.signal_count == 2
    # overall_sign is 0 here (per documented tie-break), so both ratios are
    # defined as 0.0 rather than reporting a spurious 50/50 split.
    assert result.agreement_ratio == pytest.approx(0.0)
    assert result.conflict_ratio == pytest.approx(0.0)


def test_conflicting_signals_produce_lower_abs_score_than_agreeing_at_equal_magnitude():
    # Explicit denominator test requested by the task spec: a scenario with
    # signals in conflicting directions must produce a lower |C| than the
    # same signals all agreeing, even at equal Sigma|w*s*delta|.
    def build(direction_b: int) -> list[Event]:
        ev_a = make_event(
            event_type="whale_transfer",
            confidence=0.6,
            direction=1,
            asset_class="crypto",
            timestamp=NOW,
            asset="ETH",
        )
        ev_b = make_event(
            event_type="on_chain_move",
            confidence=0.6,
            direction=direction_b,
            asset_class="crypto",
            timestamp=NOW,
            asset="ETH",
        )
        return [ev_a, ev_b]

    scorer = CorrelationScorer()
    agreeing_result = scorer.correlate("ETH", build(direction_b=1), now=NOW)
    conflicting_result = scorer.correlate("ETH", build(direction_b=-1), now=NOW)

    # Sigma|w*s*delta| (the denominator) is identical in both scenarios
    # since only direction differs, not weight/confidence/decay.
    agreeing_denom = sum(
        ws.abs_weighted_confidence for ws in agreeing_result.weighted_signals
    )
    conflicting_denom = sum(
        ws.abs_weighted_confidence for ws in conflicting_result.weighted_signals
    )
    assert agreeing_denom == pytest.approx(conflicting_denom)

    assert abs(conflicting_result.confluence_score) < abs(
        agreeing_result.confluence_score
    )
    assert agreeing_result.confluence_score == pytest.approx(1.0)


def test_majority_conflict_ratio_when_signals_mostly_oppose_the_winner():
    # 1 strong bullish signal vs. 3 weaker bearish signals that still net
    # out to a small positive C — agreement_ratio should reflect the single
    # agreeing (bullish) signal, conflict_ratio the three opposing ones.
    bull = make_event(
        event_type="whale_transfer",
        confidence=1.0,
        direction=1,
        asset_class="crypto",
        timestamp=NOW,
        asset="SOL",
    )
    bears = [
        make_event(
            event_type="ta_signal",
            confidence=0.3,
            direction=-1,
            asset_class="crypto",
            timestamp=NOW,
            asset="SOL",
            source=f"ta-{i}",
        )
        for i in range(3)
    ]
    scorer = CorrelationScorer()
    result = scorer.correlate("SOL", [bull, *bears], now=NOW)
    assert result.confluence_score > 0.0
    assert result.signal_count == 4
    assert result.agreement_ratio == pytest.approx(0.25)
    assert result.conflict_ratio == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Empty events / zero-denominator edge case
# ---------------------------------------------------------------------------


def test_empty_events_list_returns_zero_without_raising():
    scorer = CorrelationScorer()
    result = scorer.correlate("BTC", [], now=NOW)
    assert result.confluence_score == 0.0
    assert result.signal_count == 0
    assert result.weighted_signals == []
    assert result.agreement_ratio == 0.0
    assert result.conflict_ratio == 0.0


def test_all_zero_confidence_events_return_zero_without_raising():
    ev = make_event(confidence=0.0, direction=1, timestamp=NOW)
    scorer = CorrelationScorer()
    result = scorer.correlate("BTC", [ev], now=NOW)
    assert result.confluence_score == 0.0
    assert result.signal_count == 1
    # Still carries the audit trail even though it didn't move the score.
    assert len(result.weighted_signals) == 1


def test_correlate_filters_by_asset():
    btc_event = make_event(asset="BTC", timestamp=NOW)
    eth_event = make_event(asset="ETH", timestamp=NOW)
    scorer = CorrelationScorer()
    result = scorer.correlate("BTC", [btc_event, eth_event], now=NOW)
    assert result.signal_count == 1
    assert result.weighted_signals[0].event is btc_event


def test_correlate_defaults_now_to_current_utc_time_when_omitted():
    # Not pinning `now` should not raise and should produce a result close
    # to "fresh" for an event timestamped at construction time.
    fresh_event = make_event(timestamp=datetime.now(timezone.utc))
    scorer = CorrelationScorer()
    result = scorer.correlate("BTC", [fresh_event])
    assert result.signal_count == 1
    assert result.weighted_signals[0].age_minutes >= 0.0
    assert result.weighted_signals[0].age_minutes < 1.0


# ---------------------------------------------------------------------------
# Dark signal queue (AD-02, design principle 2)
# ---------------------------------------------------------------------------


def test_dark_signal_flagged_for_high_magnitude_non_news_with_no_news():
    ev = make_event(
        asset="XYZ",
        event_type="whale_transfer",
        confidence=0.9,
        direction=1,
        asset_class="crypto",
        timestamp=NOW,
    )
    dark_signals = detect_dark_signals([ev], now=NOW)
    assert len(dark_signals) == 1
    assert dark_signals[0].asset == "XYZ"
    assert dark_signals[0].non_news_signal_count == 1
    assert dark_signals[0].contributing_event_types == ("whale_transfer",)


def test_asset_with_news_event_is_not_flagged_as_dark_signal():
    strong_signal = make_event(
        asset="XYZ",
        event_type="whale_transfer",
        confidence=0.9,
        direction=1,
        asset_class="crypto",
        timestamp=NOW,
    )
    news_event = make_event(
        asset="XYZ",
        event_type="news",
        confidence=0.5,
        direction=1,
        asset_class="crypto",
        timestamp=NOW,
        source="finnhub",
    )
    dark_signals = detect_dark_signals([strong_signal, news_event], now=NOW)
    assert dark_signals == []


def test_low_magnitude_non_news_signal_not_flagged():
    weak_signal = make_event(
        asset="XYZ",
        event_type="ta_signal",
        confidence=0.05,
        direction=1,
        asset_class="crypto",
        timestamp=NOW,
    )
    dark_signals = detect_dark_signals([weak_signal], now=NOW, magnitude_threshold=1.0)
    assert dark_signals == []


def test_dark_signals_sorted_by_magnitude_descending():
    strong = make_event(
        asset="AAA",
        event_type="whale_transfer",
        confidence=1.0,
        direction=1,
        asset_class="crypto",
        timestamp=NOW,
    )
    weaker_but_still_dark = make_event(
        asset="BBB",
        event_type="options_flow",
        confidence=0.65,
        direction=1,
        asset_class="equity",
        timestamp=NOW,
    )
    dark_signals = detect_dark_signals(
        [weaker_but_still_dark, strong], now=NOW, magnitude_threshold=1.0
    )
    assert [d.asset for d in dark_signals] == ["AAA", "BBB"]
    assert (
        dark_signals[0].total_abs_weighted_confidence
        > dark_signals[1].total_abs_weighted_confidence
    )


def test_dark_signal_detection_ignores_direction_and_agreement():
    # AD-02: any strong unexplained statistical move is high-edge, even a
    # mix of conflicting-direction statistical signals with no news.
    bull = make_event(
        asset="MIX",
        event_type="whale_transfer",
        confidence=0.9,
        direction=1,
        asset_class="crypto",
        timestamp=NOW,
    )
    bear = make_event(
        asset="MIX",
        event_type="on_chain_move",
        confidence=0.9,
        direction=-1,
        asset_class="crypto",
        timestamp=NOW,
    )
    dark_signals = detect_dark_signals([bull, bear], now=NOW)
    assert len(dark_signals) == 1
    assert dark_signals[0].asset == "MIX"
    assert dark_signals[0].non_news_signal_count == 2


def test_dark_signal_decays_with_age_and_can_drop_below_threshold():
    stale_signal = make_event(
        asset="OLD",
        event_type="whale_transfer",
        confidence=0.5,
        direction=1,
        asset_class="crypto",
        # Many crypto half-lives old -> decay ~ negligible.
        timestamp=NOW - timedelta(minutes=90 * 10),
    )
    dark_signals = detect_dark_signals([stale_signal], now=NOW, magnitude_threshold=1.0)
    assert dark_signals == []


def test_detect_dark_signals_empty_input_returns_empty_list():
    assert detect_dark_signals([], now=NOW) == []
