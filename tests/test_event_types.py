"""Tests for event_types.py — the canonical Event/Article/NLPResult shapes.

Covers CLAUDE.md AD-01 (canonical Event schema, direction/confidence
constraints), NFR-03/OQ-01 (append-only — validated via frozen dataclass
immutability), and the Article/NLPResult handoff shapes feeding
nlp_pipeline.py (Step 3).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from event_types import Article, Event, NLPResult


UTC_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event(**overrides) -> Event:
    defaults = dict(
        asset="BTC",
        asset_class="crypto",
        event_type="whale_transfer",
        direction=1,
        confidence=0.8,
        timestamp=UTC_NOW,
        source="arkham",
        meta={"amount_usd": 5_000_000},
    )
    defaults.update(overrides)
    return Event(**defaults)


# ---------------------------------------------------------------------------
# Event: valid construction round-trip
# ---------------------------------------------------------------------------


def test_event_valid_construction_round_trips_all_fields():
    ts = UTC_NOW
    ev = Event(
        asset="NVDA",
        asset_class="equity",
        event_type="options_flow",
        direction=1,
        confidence=0.72,
        timestamp=ts,
        source="polygon",
        meta={"contract": "NVDA240920C00120000", "premium": 250_000},
    )
    assert ev.asset == "NVDA"
    assert ev.asset_class == "equity"
    assert ev.event_type == "options_flow"
    assert ev.direction == 1
    assert ev.confidence == 0.72
    assert ev.timestamp == ts
    assert ev.source == "polygon"
    assert ev.meta == {"contract": "NVDA240920C00120000", "premium": 250_000}


def test_event_direction_neutral_and_bearish_are_valid():
    assert make_event(direction=0).direction == 0
    assert make_event(direction=-1).direction == -1


def test_event_confidence_boundaries_are_valid():
    assert make_event(confidence=0.0).confidence == 0.0
    assert make_event(confidence=1.0).confidence == 1.0


def test_event_is_frozen_append_only():
    ev = make_event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.confidence = 0.99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Event: direction validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_direction", [2, -2, 0.5, "up", None, 1.0])
def test_event_direction_outside_valid_set_raises(bad_direction):
    with pytest.raises(ValueError):
        make_event(direction=bad_direction)


# ---------------------------------------------------------------------------
# Event: confidence validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_confidence", [-0.01, 1.01, 2.0, -5, "0.5", None, True])
def test_event_confidence_outside_valid_range_raises(bad_confidence):
    with pytest.raises(ValueError):
        make_event(confidence=bad_confidence)


def test_event_timestamp_must_be_timezone_aware():
    naive = datetime(2026, 7, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        make_event(timestamp=naive)


@pytest.mark.parametrize(
    "field_name", ["asset", "asset_class", "event_type", "source"]
)
def test_event_required_string_fields_cannot_be_empty(field_name):
    with pytest.raises(ValueError):
        make_event(**{field_name: ""})


# ---------------------------------------------------------------------------
# Event: meta accepts arbitrary type-specific data (AD-01)
# ---------------------------------------------------------------------------


def test_event_meta_accepts_arbitrary_type_specific_payload_no_new_top_level_fields():
    # A greek_anomaly-specific payload.
    greek_event = make_event(
        event_type="greek_anomaly",
        meta={"greek": "gamma", "z_score": 3.2, "expiry": "2026-08-15"},
    )
    assert greek_event.meta["greek"] == "gamma"
    assert greek_event.meta["z_score"] == 3.2

    # A wildly different shape for a different signal type — still just
    # `meta`, no schema change required.
    political_event = make_event(
        event_type="political_trade",
        asset="TSLA",
        asset_class="equity",
        meta={
            "politician": "Jane Doe",
            "chamber": "senate",
            "transaction_type": "purchase",
            "disclosed_range": "$50,001 - $100,000",
        },
    )
    assert political_event.meta["politician"] == "Jane Doe"

    # Both are still plain Events with the exact same top-level shape.
    assert set(dataclasses.asdict(greek_event).keys()) == set(
        dataclasses.asdict(political_event).keys()
    )


def test_event_meta_defaults_to_empty_dict_when_omitted():
    ev = Event(
        asset="ETH",
        asset_class="crypto",
        event_type="on_chain_move",
        direction=0,
        confidence=0.5,
        timestamp=UTC_NOW,
        source="glassnode",
    )
    assert ev.meta == {}


def test_event_meta_correction_uses_meta_key_not_mutation():
    original = make_event()
    correction = make_event(
        meta={"amount_usd": 4_500_000, "correction_of": "some-fingerprint-or-id"}
    )
    # The "correction" is a distinct, new Event object.
    assert original is not correction
    assert correction.meta["correction_of"] == "some-fingerprint-or-id"


def test_event_meta_must_be_a_dict():
    with pytest.raises(ValueError):
        make_event(meta=["not", "a", "dict"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Article
# ---------------------------------------------------------------------------


def test_article_constructs_with_expected_fields():
    art = Article(
        headline="Fed holds rates steady",
        body="The Federal Reserve announced today that...",
        url="https://example.com/news/fed-holds-rates",
        published_at=UTC_NOW,
        source="reuters_rss",
        fingerprint="abc123md5",
    )
    assert art.headline == "Fed holds rates steady"
    assert art.body.startswith("The Federal Reserve")
    assert art.url == "https://example.com/news/fed-holds-rates"
    assert art.published_at == UTC_NOW
    assert art.source == "reuters_rss"
    assert art.fingerprint == "abc123md5"
    assert art.embedding is None


def test_article_optional_fields_default_to_none():
    art = Article(
        headline="Some headline",
        body="Some body",
        url="https://example.com/x",
        published_at=UTC_NOW,
        source="finnhub",
    )
    assert art.fingerprint is None
    assert art.embedding is None


def test_article_requires_timezone_aware_published_at():
    with pytest.raises(ValueError):
        Article(
            headline="Headline",
            body="Body",
            url="https://example.com/x",
            published_at=datetime(2026, 7, 1, 12, 0, 0),
            source="finnhub",
        )


def test_article_headline_cannot_be_empty():
    with pytest.raises(ValueError):
        Article(
            headline="",
            body="Body",
            url="https://example.com/x",
            published_at=UTC_NOW,
            source="finnhub",
        )


# ---------------------------------------------------------------------------
# NLPResult
# ---------------------------------------------------------------------------


def make_article() -> Article:
    return Article(
        headline="Whale moves 10,000 BTC to exchange",
        body="On-chain data shows a large transfer...",
        url="https://example.com/whale-move",
        published_at=UTC_NOW,
        source="finnhub",
        fingerprint="deadbeef",
    )


def test_nlpresult_constructs_with_expected_fields():
    art = make_article()
    result = NLPResult(
        article=art,
        tickers=["BTC"],
        sentiment=-0.6,
        topic="on_chain",
        urgency=0.85,
        is_duplicate=False,
    )
    assert result.article is art
    assert result.tickers == ["BTC"]
    assert result.sentiment == -0.6
    assert result.topic == "on_chain"
    assert result.urgency == 0.85
    assert result.is_duplicate is False
    assert result.duplicate_of is None


def test_nlpresult_supports_multiple_tickers():
    art = make_article()
    result = NLPResult(
        article=art,
        tickers=["BTC", "ETH"],
        sentiment=0.2,
        topic="macro",
        urgency=0.4,
    )
    assert result.tickers == ["BTC", "ETH"]


def test_nlpresult_duplicate_requires_duplicate_of():
    art = make_article()
    with pytest.raises(ValueError):
        NLPResult(
            article=art,
            tickers=["BTC"],
            sentiment=0.0,
            topic="on_chain",
            urgency=0.1,
            is_duplicate=True,
            duplicate_of=None,
        )


def test_nlpresult_duplicate_with_reference_is_valid():
    art = make_article()
    result = NLPResult(
        article=art,
        tickers=["BTC"],
        sentiment=0.0,
        topic="on_chain",
        urgency=0.1,
        is_duplicate=True,
        duplicate_of="original-fingerprint-123",
    )
    assert result.is_duplicate is True
    assert result.duplicate_of == "original-fingerprint-123"


@pytest.mark.parametrize("bad_sentiment", [-1.5, 1.5, -1.01, 1.01])
def test_nlpresult_sentiment_out_of_range_raises(bad_sentiment):
    art = make_article()
    with pytest.raises(ValueError):
        NLPResult(
            article=art,
            tickers=["BTC"],
            sentiment=bad_sentiment,
            topic="on_chain",
            urgency=0.5,
        )


@pytest.mark.parametrize("bad_urgency", [-0.1, 1.1, 2.0])
def test_nlpresult_urgency_out_of_range_raises(bad_urgency):
    art = make_article()
    with pytest.raises(ValueError):
        NLPResult(
            article=art,
            tickers=["BTC"],
            sentiment=0.0,
            topic="on_chain",
            urgency=bad_urgency,
        )
