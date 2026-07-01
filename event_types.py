"""Canonical shared types for Trade-Dash's core event pipeline.

This module exists to break a circular import that would otherwise result
from CLAUDE.md's literal file layout: `nlp_pipeline.py` and
`correlation_scorer.py` both need to consume/produce the canonical `Event`
(plus `Article`/`NLPResult`), while `unified_pipeline.py` needs to import
`NLPPipeline` and `CorrelationScorer` from those same modules. Putting the
shared dataclasses here lets every module depend on `event_types` without
depending on each other.

`unified_pipeline.py` (Step 5) does:

    from event_types import Event, Article, NLPResult

and re-exports them, so `from unified_pipeline import Event` — the literal
phrasing used elsewhere in CLAUDE.md — still works for downstream
consumers (API layer, event bus, tests, etc.).

See CLAUDE.md:
  - AD-01: single canonical Event schema for every signal type.
  - AD-02: bidirectional correlation — news and non-news Events must be
    structurally indistinguishable to the scorer.
  - AD-06: NLP interfaces are swap-ready (lexicon dev -> finbert/spaCy prod);
    nothing in this module's signatures may encode that distinction.
  - Design principle 4: auditability first — nothing here is ever mutated
    in place; corrections are new Events (see `meta["correction_of"]`).
  - Design principle 5: swap-ready interfaces.

No pydantic here on purpose: pydantic is reserved for `api_service.py`'s
request/response validation layer (Step 7). This module is pure stdlib
`dataclasses` + `typing` so every downstream module (including non-API
consumers like `event_bus.py`, backtesting, dataset factory) can import it
without pulling in a web-framework dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Canonical enumerations
# ---------------------------------------------------------------------------

#: Valid `Event.direction` values — AD-01 / design principle: direction is
#: always +1 (bullish), -1 (bearish), or 0 (neutral); never a raw sentiment
#: float. Enforced in `Event.__post_init__`.
VALID_DIRECTIONS = frozenset({-1, 0, 1})

#: Asset classes in scope for Phase 1+ (CLAUDE.md §1 Scope). `futures` is
#: called out as a v2 asset class in CLAUDE.md but is included here now so
#: the type doesn't need to change later — nothing downstream should assume
#: this set is exhaustive; treat it as a soft reference, not an enforced
#: constraint (asset classes are a str field, not a hard enum, to stay
#: forward-compatible with FR-39's multi-market loaders).
ASSET_CLASSES = ("equity", "crypto", "forex", "futures")

#: The 11 non-news signal categories (FR-02) plus "news" for NLP-derived
#: Events. `event_type` is a plain str field (not a hard enum) so a new
#: signal type can be added without a dataclass/schema migration — but per
#: AD-01/CLAUDE.md §9, any new type must get a Signal Weight Reference entry
#: before being wired into `alert_payloads()`. This tuple is a reference
#: list for documentation/validation helpers, not an enforced constraint.
NON_NEWS_EVENT_TYPES = (
    "greek_anomaly",
    "options_flow",
    "strategy_break",
    "on_chain_move",
    "funding_extreme",
    "whale_transfer",
    "macro_event",
    "political_trade",
    "insider_trade",
    "ml_forecast",
    "ta_signal",
)

#: All known event types, including the news-derived type emitted by
#: `NLPPipeline`.
EVENT_TYPES = NON_NEWS_EVENT_TYPES + ("news",)


def _ensure_utc(dt: datetime, *, field_name: str) -> datetime:
    """Validate that `dt` is timezone-aware, normalized to UTC.

    Naive datetimes are rejected rather than silently assumed-UTC: CLAUDE.md
    AD-03's exponential time decay depends on precise `age_minutes`
    computation, and silently guessing a timezone would corrupt that decay
    calculation without any visible error.
    """
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(
            f"{field_name} must be timezone-aware (UTC); got naive datetime {dt!r}"
        )
    return dt


# ---------------------------------------------------------------------------
# Event — the canonical schema (AD-01)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """The single canonical event shape emitted by every signal source.

    Every signal type — news-derived or statistical — must be constructed
    through this exact shape. Type-specific data belongs in `meta`, never as
    a new top-level field (AD-01, CLAUDE.md §9 design principle 3/5). This
    is what lets `CorrelationScorer` treat every Event identically
    regardless of which factory produced it (AD-02).

    Frozen/immutable by design: Trade-Dash's event store is append-only
    (NFR-03, OQ-01). A correction is a new `Event` with
    `meta["correction_of"] = <original event id or fingerprint>`, never an
    in-place mutation. Making the dataclass frozen makes accidental mutation
    a hard error rather than a silent bug.

    Attributes:
        asset: Ticker/symbol, e.g. "BTC", "NVDA".
        asset_class: e.g. "equity", "crypto", "forex", "futures".
        event_type: One of the 11 non-news signal categories (FR-02) or
            "news" for NLP-derived events.
        direction: +1 bullish, -1 bearish, 0 neutral. Never a raw float.
        confidence: Confidence/strength in [0.0, 1.0] (this is `s_i` in the
            confluence formula, AD-04).
        timestamp: Timezone-aware UTC datetime the event occurred/was
            observed.
        source: Origin identifier, e.g. "finnhub", "coinglass", "polygon".
        meta: Arbitrary JSONB-shaped type-specific payload. This is the
            *only* place new, signal-type-specific data may live.
    """

    asset: str
    asset_class: str
    event_type: str
    direction: int
    confidence: float
    timestamp: datetime
    source: str
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.asset:
            raise ValueError("Event.asset must be a non-empty string")
        if not self.asset_class:
            raise ValueError("Event.asset_class must be a non-empty string")
        if not self.event_type:
            raise ValueError("Event.event_type must be a non-empty string")
        if not self.source:
            raise ValueError("Event.source must be a non-empty string")

        # Reject bool (a subclass of int) and any non-int numeric type (e.g.
        # a raw float like 1.0) even though `1.0 == 1` and `1.0 in {-1,0,1}`
        # would otherwise pass a membership check — direction must be a
        # genuine discrete int, never a raw sentiment float (AD-01).
        if isinstance(self.direction, bool) or not isinstance(self.direction, int):
            raise ValueError(
                "Event.direction must be an int in {-1, 0, 1} "
                f"(+1 bullish / -1 bearish / 0 neutral); got {self.direction!r} "
                f"({type(self.direction).__name__})"
            )
        if self.direction not in VALID_DIRECTIONS:
            raise ValueError(
                "Event.direction must be one of -1, 0, 1 "
                f"(+1 bullish / -1 bearish / 0 neutral); got {self.direction!r}"
            )

        if not isinstance(self.confidence, (int, float)) or isinstance(
            self.confidence, bool
        ):
            raise ValueError(
                f"Event.confidence must be a float in [0.0, 1.0]; got {self.confidence!r}"
            )
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError(
                f"Event.confidence must be in [0.0, 1.0]; got {self.confidence!r}"
            )

        _ensure_utc(self.timestamp, field_name="Event.timestamp")

        if not isinstance(self.meta, dict):
            raise ValueError(f"Event.meta must be a dict; got {type(self.meta)!r}")


# ---------------------------------------------------------------------------
# Article — raw news input to NLPPipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Article:
    """Raw news article shape, prior to NLP processing.

    Feeds into `NLPPipeline` (Step 3, `nlp_pipeline.py`), which produces an
    `NLPResult` and ultimately emits a canonical `Event` with
    `event_type="news"`. Raw articles are never mutated after ingestion
    (NFR-03) — `fingerprint` supports MD5-based dedup (FR-03) without
    altering the original payload.

    Attributes:
        headline: Article headline/title.
        body: Full body text or summary used for NLP processing.
        url: Canonical source URL.
        published_at: Timezone-aware UTC datetime of publication.
        source: Origin identifier, e.g. "finnhub", "reuters_rss".
        fingerprint: Optional precomputed MD5 (or similar) fingerprint for
            dedup (FR-03). If not supplied, `NLPPipeline` is responsible for
            computing one — this field just gives it a stable home so raw
            payloads are never touched post-ingestion.
        embedding: Optional precomputed dense embedding for semantic dedup
            (cosine similarity, FR-03; `vector(384)` per AD-08). Left
            optional/`None` in dev (lexicon phase, AD-06) and populated once
            `sentence-transformers` is wired in production.
    """

    headline: str
    body: str
    url: str
    published_at: datetime
    source: str
    fingerprint: Optional[str] = None
    embedding: Optional[list[float]] = None

    def __post_init__(self) -> None:
        if not self.headline:
            raise ValueError("Article.headline must be a non-empty string")
        if not self.url:
            raise ValueError("Article.url must be a non-empty string")
        if not self.source:
            raise ValueError("Article.source must be a non-empty string")
        _ensure_utc(self.published_at, field_name="Article.published_at")


# ---------------------------------------------------------------------------
# NLPResult — output of NLP processing, prior to Event construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NLPResult:
    """Result of running the NLP pipeline stages over an `Article`.

    Mirrors `nlp_pipeline.py`'s documented stage order (CLAUDE.md §5):
    ticker extraction -> sentiment scoring -> topic classification ->
    urgency scoring -> deduplication -> Event emission. This dataclass is
    the handoff point between the last NLP stage and Event construction —
    `NLPPipeline` builds one `NLPResult` per `Article`, then
    `UnifiedPipeline`/`NLPPipeline` turns non-duplicate results into
    canonical `Event`s (one Event per extracted ticker, typically).

    Kept structurally independent of *how* each stage is implemented
    (lexicon vs. finbert sentiment, regex vs. spaCy NER) per AD-06 / design
    principle 5 — the swap happens inside `nlp_pipeline.py`'s function
    bodies, never in this shape.

    Attributes:
        article: The source `Article` this result was derived from.
        tickers: Extracted ticker symbols, e.g. ["BTC", "NVDA"].
        sentiment: Sentiment score in [-1.0, 1.0] (-1 fully bearish, +1
            fully bullish, 0 neutral). This is distinct from `Event.direction`
            (an int in {-1,0,1}) — `NLPPipeline` is responsible for mapping
            this continuous score down to a discrete direction when it
            constructs the Event.
        topic: Classified topic/category label, e.g. "earnings", "macro",
            "regulatory".
        urgency: Urgency score, conventionally in [0.0, 1.0] (higher = more
            time-sensitive). Feeds into confidence/weighting downstream,
            not `Event.confidence` directly.
        is_duplicate: Whether dedup (MD5 fingerprint + cosine similarity,
            FR-03) flagged this as a duplicate of a previously seen article.
            Duplicate results should not produce new Events.
        duplicate_of: Fingerprint (or other identifier) of the original
            article this was deduped against, if `is_duplicate` is True.
    """

    article: Article
    tickers: list[str]
    sentiment: float
    topic: str
    urgency: float
    is_duplicate: bool = False
    duplicate_of: Optional[str] = None

    def __post_init__(self) -> None:
        if not (-1.0 <= float(self.sentiment) <= 1.0):
            raise ValueError(
                f"NLPResult.sentiment must be in [-1.0, 1.0]; got {self.sentiment!r}"
            )
        if not (0.0 <= float(self.urgency) <= 1.0):
            raise ValueError(
                f"NLPResult.urgency must be in [0.0, 1.0]; got {self.urgency!r}"
            )
        if self.is_duplicate and self.duplicate_of is None:
            raise ValueError(
                "NLPResult.duplicate_of must be set when is_duplicate is True"
            )


__all__ = [
    "Event",
    "Article",
    "NLPResult",
    "VALID_DIRECTIONS",
    "ASSET_CLASSES",
    "NON_NEWS_EVENT_TYPES",
    "EVENT_TYPES",
]
