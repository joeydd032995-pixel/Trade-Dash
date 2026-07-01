"""Tests for unified_pipeline.py — SignalFactory (FR-02), UnifiedPipeline
orchestration (ingest_articles/ingest_signal_events/correlate/correlate_all/
alert_payloads, FR-19), and the CLAUDE.md §5 re-export contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from event_types import Article, Event, NLPResult, NON_NEWS_EVENT_TYPES
from correlation_scorer import CorrelationScorer, SIGNAL_WEIGHTS
from nlp_pipeline import NLPPipeline
import unified_pipeline as up
from unified_pipeline import SignalFactory, UnifiedPipeline

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_article(**overrides) -> Article:
    defaults = dict(
        headline="NVDA surges on record earnings beat",
        body="Shares of NVDA surged after the company beat expectations.",
        url="https://example.com/nvda-earnings",
        published_at=NOW,
        source="finnhub",
    )
    defaults.update(overrides)
    return Article(**defaults)


# ---------------------------------------------------------------------------
# SignalFactory — one constructor per non-news type (FR-02)
# ---------------------------------------------------------------------------

_COMMON_KWARGS = dict(
    asset="BTC",
    asset_class="crypto",
    direction=1,
    confidence=0.8,
    timestamp=NOW,
    source="arkham",
)


def test_signal_factory_covers_all_non_news_event_types():
    # Every event_type in event_types.NON_NEWS_EVENT_TYPES must have a
    # corresponding SignalFactory constructor of the same name.
    for event_type in NON_NEWS_EVENT_TYPES:
        assert hasattr(SignalFactory, event_type), (
            f"SignalFactory is missing a constructor for {event_type!r}"
        )


def test_signal_factory_covers_exactly_the_signal_weights_non_news_keys():
    non_news_weighted = set(SIGNAL_WEIGHTS) - {"news"}
    assert non_news_weighted == set(NON_NEWS_EVENT_TYPES)


def test_greek_anomaly():
    ev = SignalFactory.greek_anomaly(
        **_COMMON_KWARGS, greek="gamma", z_score=3.2, option_symbol="BTC-250101-C-60000"
    )
    assert isinstance(ev, Event)
    assert ev.event_type == "greek_anomaly"
    assert ev.meta["greek"] == "gamma"
    assert ev.meta["z_score"] == 3.2
    assert ev.meta["option_symbol"] == "BTC-250101-C-60000"


def test_options_flow():
    ev = SignalFactory.options_flow(
        **_COMMON_KWARGS,
        contract_type="call",
        strike=65000.0,
        expiry="2026-08-01",
        premium=250000.0,
    )
    assert ev.event_type == "options_flow"
    assert ev.meta["contract_type"] == "call"
    assert ev.meta["strike"] == 65000.0
    assert ev.meta["expiry"] == "2026-08-01"
    assert ev.meta["premium"] == 250000.0


def test_strategy_break():
    ev = SignalFactory.strategy_break(
        **_COMMON_KWARGS,
        strategy_name="opening_range_breakout",
        level=64000.0,
        break_type="breakout",
    )
    assert ev.event_type == "strategy_break"
    assert ev.meta["strategy_name"] == "opening_range_breakout"
    assert ev.meta["level"] == 64000.0
    assert ev.meta["break_type"] == "breakout"


def test_on_chain_move():
    ev = SignalFactory.on_chain_move(
        **_COMMON_KWARGS, metric="exchange_netflow", value=-1200.5, change_pct=15.2
    )
    assert ev.event_type == "on_chain_move"
    assert ev.meta["metric"] == "exchange_netflow"
    assert ev.meta["value"] == -1200.5
    assert ev.meta["change_pct"] == 15.2


def test_funding_extreme():
    ev = SignalFactory.funding_extreme(
        **_COMMON_KWARGS, funding_rate=0.015, exchange="binance", open_interest=5_000_000.0
    )
    assert ev.event_type == "funding_extreme"
    assert ev.meta["funding_rate"] == 0.015
    assert ev.meta["exchange"] == "binance"
    assert ev.meta["open_interest"] == 5_000_000.0


def test_whale_transfer():
    ev = SignalFactory.whale_transfer(
        **_COMMON_KWARGS,
        amount_usd=25_000_000.0,
        from_label="binance_hot_wallet",
        to_label="unknown_wallet",
    )
    assert ev.event_type == "whale_transfer"
    assert ev.meta["amount_usd"] == 25_000_000.0
    assert ev.meta["from_label"] == "binance_hot_wallet"
    assert ev.meta["to_label"] == "unknown_wallet"


def test_macro_event():
    ev = SignalFactory.macro_event(
        asset="SPY",
        asset_class="equity",
        direction=-1,
        confidence=0.7,
        timestamp=NOW,
        source="bls",
        indicator="CPI",
        actual=3.7,
        forecast=3.4,
        previous=3.3,
    )
    assert ev.event_type == "macro_event"
    assert ev.meta["indicator"] == "CPI"
    assert ev.meta["actual"] == 3.7
    assert ev.meta["forecast"] == 3.4
    assert ev.meta["previous"] == 3.3


def test_political_trade():
    ev = SignalFactory.political_trade(
        asset="NVDA",
        asset_class="equity",
        direction=1,
        confidence=0.6,
        timestamp=NOW,
        source="house_stock_act",
        politician="Jane Doe",
        committee="Financial Services",
        transaction_type="buy",
        amount_range="$15,001 - $50,000",
    )
    assert ev.event_type == "political_trade"
    assert ev.meta["politician"] == "Jane Doe"
    assert ev.meta["committee"] == "Financial Services"
    assert ev.meta["transaction_type"] == "buy"
    assert ev.meta["amount_range"] == "$15,001 - $50,000"


def test_insider_trade():
    ev = SignalFactory.insider_trade(
        asset="NVDA",
        asset_class="equity",
        direction=1,
        confidence=0.65,
        timestamp=NOW,
        source="sec_edgar",
        insider_name="John Smith",
        title="CEO",
        shares=10000,
        transaction_type="buy",
    )
    assert ev.event_type == "insider_trade"
    assert ev.meta["insider_name"] == "John Smith"
    assert ev.meta["title"] == "CEO"
    assert ev.meta["shares"] == 10000
    assert ev.meta["transaction_type"] == "buy"


def test_ml_forecast():
    ev = SignalFactory.ml_forecast(
        **_COMMON_KWARGS,
        model_name="lstm_v3",
        predicted_price=72000.0,
        horizon_days=5,
    )
    assert ev.event_type == "ml_forecast"
    assert ev.meta["model_name"] == "lstm_v3"
    assert ev.meta["predicted_price"] == 72000.0
    assert ev.meta["horizon_days"] == 5


def test_ta_signal():
    ev = SignalFactory.ta_signal(
        **_COMMON_KWARGS,
        indicator="RSI",
        trigger_condition="oversold",
        value=24.5,
    )
    assert ev.event_type == "ta_signal"
    assert ev.meta["indicator"] == "RSI"
    assert ev.meta["trigger_condition"] == "oversold"
    assert ev.meta["value"] == 24.5


def test_signal_factory_extra_meta_kwargs_flow_through():
    ev = SignalFactory.whale_transfer(**_COMMON_KWARGS, amount_usd=1e6, custom_field="x")
    assert ev.meta["custom_field"] == "x"


def test_signal_factory_events_validate_through_event_post_init():
    # Bad direction should raise via Event.__post_init__, not a duplicated
    # check in SignalFactory.
    bad_kwargs = dict(_COMMON_KWARGS)
    bad_kwargs["direction"] = 2
    with pytest.raises(ValueError):
        SignalFactory.whale_transfer(**bad_kwargs, amount_usd=1e6)


# ---------------------------------------------------------------------------
# UnifiedPipeline.ingest_articles
# ---------------------------------------------------------------------------


def test_ingest_articles_produces_one_event_per_ticker():
    pipeline = UnifiedPipeline()
    article = make_article(
        headline="NVDA and AMD surge on strong AI demand",
        body="NVDA and AMD both surged today.",
    )
    events = pipeline.ingest_articles([article])
    assets = {e.asset for e in events}
    assert assets == {"NVDA", "AMD"}
    for e in events:
        assert e.event_type == "news"
        assert e.source == "finnhub"
        assert e.timestamp == NOW


def test_ingest_articles_direction_mapping_bullish():
    pipeline = UnifiedPipeline()
    article = make_article(
        headline="NVDA surges on record earnings beat",
        body="NVDA surged after beating expectations with strong growth.",
    )
    events = pipeline.ingest_articles([article])
    assert len(events) == 1
    assert events[0].direction == 1
    assert events[0].confidence > 0.0


def test_ingest_articles_direction_mapping_bearish():
    pipeline = UnifiedPipeline()
    article = make_article(
        headline="NVDA plunges on weak guidance and lawsuit",
        body="NVDA crashed after missing expectations and warns of a lawsuit.",
    )
    events = pipeline.ingest_articles([article])
    assert len(events) == 1
    assert events[0].direction == -1


def test_ingest_articles_direction_mapping_neutral_for_no_sentiment_words():
    pipeline = UnifiedPipeline()
    article = make_article(
        headline="NVDA holds investor day event",
        body="NVDA hosted an ordinary event with no notable commentary.",
    )
    events = pipeline.ingest_articles([article])
    assert len(events) == 1
    assert events[0].direction == 0


def test_ingest_articles_meta_contains_nlp_fields_not_new_toplevel_fields():
    pipeline = UnifiedPipeline()
    article = make_article()
    events = pipeline.ingest_articles([article])
    assert len(events) == 1
    ev = events[0]
    assert "topic" in ev.meta
    assert "sentiment" in ev.meta
    assert "urgency" in ev.meta
    assert "is_duplicate" in ev.meta
    assert "headline" in ev.meta
    assert "url" in ev.meta
    # Confirm no unexpected new top-level Event attributes were introduced.
    assert set(Event.__dataclass_fields__) == {
        "asset", "asset_class", "event_type", "direction", "confidence",
        "timestamp", "source", "meta",
    }


def test_ingest_articles_no_tickers_produces_no_events():
    pipeline = UnifiedPipeline()
    article = make_article(
        headline="Local weather turns cloudy today",
        body="No tickers mentioned anywhere in this text at all.",
    )
    events = pipeline.ingest_articles([article])
    assert events == []
    assert pipeline.events == []


def test_ingest_articles_skips_duplicate_articles():
    pipeline = UnifiedPipeline()
    original = make_article(
        headline="NVDA surges on record earnings beat",
        body="NVDA surged after beating expectations.",
        url="https://example.com/nvda-1",
    )
    duplicate = make_article(
        headline="NVDA surges on record earnings beat",  # identical headline -> fingerprint match
        body="NVDA surged after beating expectations.",
        url="https://example.com/nvda-1-mirror",
    )

    first_events = pipeline.ingest_articles([original])
    assert len(first_events) == 1

    second_events = pipeline.ingest_articles([duplicate])
    assert second_events == []
    # Only the original's Event(s) should be stored — duplicate produced none.
    assert len(pipeline.events) == 1


def test_ingest_articles_accumulates_corpus_across_calls():
    pipeline = UnifiedPipeline()
    article1 = make_article(url="https://example.com/a")
    article2 = make_article(url="https://example.com/b")  # same headline/body -> dup

    pipeline.ingest_articles([article1])
    events2 = pipeline.ingest_articles([article2])
    assert events2 == []  # deduped against article1 from the prior call


# ---------------------------------------------------------------------------
# UnifiedPipeline.ingest_signal_events — append-only (NFR-03)
# ---------------------------------------------------------------------------


def make_signal_event(**overrides) -> Event:
    kwargs = dict(_COMMON_KWARGS)
    kwargs.update(overrides)
    return SignalFactory.whale_transfer(**kwargs, amount_usd=1e6)


def test_ingest_signal_events_stores_events():
    pipeline = UnifiedPipeline()
    ev = make_signal_event()
    result = pipeline.ingest_signal_events([ev])
    assert result == [ev]
    assert pipeline.events == [ev]


def test_ingest_signal_events_is_append_only_and_does_not_mutate_prior_list():
    pipeline = UnifiedPipeline()
    ev1 = make_signal_event(source="arkham_1")
    pipeline.ingest_signal_events([ev1])

    snapshot = pipeline.events
    assert snapshot == [ev1]

    ev2 = make_signal_event(source="arkham_2")
    pipeline.ingest_signal_events([ev2])

    # The previously-taken snapshot must be unaffected by the later ingest —
    # proves `.events` returns a defensive copy, not a live reference, and
    # that ingestion only appends.
    assert snapshot == [ev1]
    assert pipeline.events == [ev1, ev2]


def test_ingest_signal_events_rejects_non_event_items():
    pipeline = UnifiedPipeline()
    with pytest.raises(TypeError):
        pipeline.ingest_signal_events([{"not": "an event"}])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# UnifiedPipeline.correlate / correlate_all — delegation to CorrelationScorer
# ---------------------------------------------------------------------------


def test_correlate_delegates_to_correlation_scorer():
    pipeline = UnifiedPipeline()
    events = [
        make_signal_event(asset="BTC", direction=1, confidence=0.8, timestamp=NOW),
        make_signal_event(asset="BTC", direction=1, confidence=0.6, timestamp=NOW),
        make_signal_event(asset="ETH", direction=-1, confidence=0.5, timestamp=NOW),
    ]
    pipeline.ingest_signal_events(events)

    direct_scorer = CorrelationScorer()
    expected = direct_scorer.correlate("BTC", events, now=NOW)

    actual = pipeline.correlate("BTC", now=NOW)

    assert actual.asset == expected.asset
    assert actual.confluence_score == pytest.approx(expected.confluence_score)
    assert actual.signal_count == expected.signal_count
    assert actual.agreement_ratio == pytest.approx(expected.agreement_ratio)
    assert actual.conflict_ratio == pytest.approx(expected.conflict_ratio)


def test_correlate_all_covers_every_distinct_asset():
    pipeline = UnifiedPipeline()
    events = [
        make_signal_event(asset="BTC", timestamp=NOW),
        make_signal_event(asset="ETH", timestamp=NOW),
        make_signal_event(asset="SOL", timestamp=NOW),
    ]
    pipeline.ingest_signal_events(events)

    results = pipeline.correlate_all(now=NOW)
    assert set(results.keys()) == {"BTC", "ETH", "SOL"}
    for asset, result in results.items():
        assert result.asset == asset


def test_correlate_all_empty_when_no_events():
    pipeline = UnifiedPipeline()
    assert pipeline.correlate_all() == {}


# ---------------------------------------------------------------------------
# UnifiedPipeline.alert_payloads — threshold + min_signals gates (FR-19)
# ---------------------------------------------------------------------------


def _strong_agreeing_events(asset: str, count: int, event_type: str = "whale_transfer"):
    """Build `count` same-direction, fresh, high-confidence Events for `asset`
    so the confluence score comes out strongly positive (near +1.0)."""
    return [
        Event(
            asset=asset,
            asset_class="crypto",
            event_type=event_type,
            direction=1,
            confidence=0.9,
            timestamp=NOW,
            source=f"source_{i}",
            meta={},
        )
        for i in range(count)
    ]


def test_alert_payloads_fires_above_threshold_with_enough_signals():
    pipeline = UnifiedPipeline()
    pipeline.ingest_signal_events(_strong_agreeing_events("BTC", 3))

    payloads = pipeline.alert_payloads(threshold=0.65, min_signals=3, now=NOW)
    assert len(payloads) == 1
    assert payloads[0]["asset"] == "BTC"
    assert abs(payloads[0]["confluence_score"]) >= 0.65
    assert payloads[0]["signal_count"] == 3


def test_alert_payloads_min_signals_gate_blocks_below_minimum():
    pipeline = UnifiedPipeline()
    # Only 2 strong agreeing signals -> confluence_score will be high, but
    # signal_count (2) < min_signals (3) -> must not fire.
    pipeline.ingest_signal_events(_strong_agreeing_events("BTC", 2))

    payloads = pipeline.alert_payloads(threshold=0.65, min_signals=3, now=NOW)
    assert payloads == []


def test_alert_payloads_threshold_gate_just_below_does_not_fire():
    pipeline = UnifiedPipeline()
    # Mix directions so the confluence score lands just under 0.65: 2 events
    # agreeing bullish at full confidence, 1 weak bearish to pull the score
    # down slightly below 0.65 while keeping signal_count at 3.
    events = [
        Event(asset="BTC", asset_class="crypto", event_type="whale_transfer",
              direction=1, confidence=0.9, timestamp=NOW, source="s1", meta={}),
        Event(asset="BTC", asset_class="crypto", event_type="whale_transfer",
              direction=1, confidence=0.9, timestamp=NOW, source="s2", meta={}),
        Event(asset="BTC", asset_class="crypto", event_type="ta_signal",
              direction=-1, confidence=0.9, timestamp=NOW, source="s3", meta={}),
    ]
    pipeline.ingest_signal_events(events)

    result = pipeline.correlate("BTC", now=NOW)
    assert abs(result.confluence_score) < 0.65  # sanity check on the fixture

    payloads = pipeline.alert_payloads(threshold=0.65, min_signals=3, now=NOW)
    assert payloads == []


def test_alert_payloads_threshold_gate_at_or_above_fires():
    pipeline = UnifiedPipeline()
    pipeline.ingest_signal_events(_strong_agreeing_events("BTC", 5))
    result = pipeline.correlate("BTC", now=NOW)
    assert result.confluence_score == pytest.approx(1.0)

    payloads = pipeline.alert_payloads(threshold=result.confluence_score, min_signals=3, now=NOW)
    assert len(payloads) == 1


def test_alert_payloads_top_4_contributors_sorted_by_abs_contribution():
    pipeline = UnifiedPipeline()
    events = [
        Event(asset="BTC", asset_class="crypto", event_type="whale_transfer",
              direction=1, confidence=0.9, timestamp=NOW, source="whale_big", meta={}),
        Event(asset="BTC", asset_class="crypto", event_type="options_flow",
              direction=1, confidence=0.85, timestamp=NOW, source="options_big", meta={}),
        Event(asset="BTC", asset_class="crypto", event_type="ta_signal",
              direction=1, confidence=0.2, timestamp=NOW, source="ta_small", meta={}),
        Event(asset="BTC", asset_class="crypto", event_type="political_trade",
              direction=1, confidence=0.2, timestamp=NOW, source="political_small", meta={}),
        Event(asset="BTC", asset_class="crypto", event_type="macro_event",
              direction=1, confidence=0.3, timestamp=NOW, source="macro_mid", meta={}),
    ]
    pipeline.ingest_signal_events(events)

    payloads = pipeline.alert_payloads(threshold=0.0, min_signals=3, now=NOW)
    assert len(payloads) == 1
    top = payloads[0]["top_contributors"]
    assert len(top) == 4  # capped at top 4 of 5 signals

    contributions = [abs(c["contribution"]) for c in top]
    assert contributions == sorted(contributions, reverse=True)

    # The two strongest (whale_transfer, options_flow) must be present;
    # the two weakest ta/political signals (both confidence=0.2) — one of
    # them must be dropped since only 4 of 5 fit.
    top_sources = {c["source"] for c in top}
    assert "whale_big" in top_sources
    assert "options_big" in top_sources


def test_alert_payloads_sorted_by_abs_confluence_score_descending():
    pipeline = UnifiedPipeline()
    pipeline.ingest_signal_events(_strong_agreeing_events("BTC", 5))
    pipeline.ingest_signal_events(_strong_agreeing_events("ETH", 3))
    # Weaken ETH's agreement so it scores lower than BTC's full +1.0 but
    # still clears the gates.
    weak_ETH_conflict = Event(
        asset="ETH", asset_class="crypto", event_type="ta_signal",
        direction=-1, confidence=0.3, timestamp=NOW, source="conflict", meta={},
    )
    pipeline.ingest_signal_events([weak_ETH_conflict])

    payloads = pipeline.alert_payloads(threshold=0.5, min_signals=3, now=NOW)
    assert [p["asset"] for p in payloads] == sorted(
        (p["asset"] for p in payloads),
        key=lambda a: next(abs(p["confluence_score"]) for p in payloads if p["asset"] == a),
        reverse=True,
    )
    scores = [abs(p["confluence_score"]) for p in payloads]
    assert scores == sorted(scores, reverse=True)


def test_alert_payloads_severity_bands():
    pipeline = UnifiedPipeline()
    pipeline.ingest_signal_events(_strong_agreeing_events("BTC", 3))  # -> confluence 1.0
    payloads = pipeline.alert_payloads(threshold=0.65, min_signals=3, now=NOW)
    assert payloads[0]["severity"] == "critical"  # abs(1.0) >= 0.92


# ---------------------------------------------------------------------------
# Re-export sanity check (CLAUDE.md §5 literal phrasing compatibility)
# ---------------------------------------------------------------------------


def test_reexports_are_the_same_objects_as_home_modules():
    from event_types import Event as HomeEvent
    from event_types import Article as HomeArticle
    from event_types import NLPResult as HomeNLPResult
    from nlp_pipeline import NLPPipeline as HomeNLPPipeline
    from correlation_scorer import CorrelationScorer as HomeCorrelationScorer
    from correlation_scorer import CorrelationResult as HomeCorrelationResult

    assert up.Event is HomeEvent
    assert up.Article is HomeArticle
    assert up.NLPResult is HomeNLPResult
    assert up.NLPPipeline is HomeNLPPipeline
    assert up.CorrelationScorer is HomeCorrelationScorer
    assert up.CorrelationResult is HomeCorrelationResult


def test_reexports_importable_directly_from_unified_pipeline():
    # Exercises the literal `from unified_pipeline import X` phrasing from
    # CLAUDE.md §5, for every name it lists.
    from unified_pipeline import (
        Event as E,
        Article as A,
        NLPResult as N,
        NLPPipeline as P,
        CorrelationScorer as C,
    )

    assert E is not None
    assert A is not None
    assert N is not None
    assert P is not None
    assert C is not None
