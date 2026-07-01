"""Trade-Dash's core pipeline orchestrator — Step 5 of the Phase 1 build.

Ties together the three already-built modules into the single spine every
other subsystem plugs into (CLAUDE.md §5 "Core Pipeline (Python)"):

    event_types.py        -> Event, Article, NLPResult (canonical shapes)
    nlp_pipeline.py        -> NLPPipeline (Article -> NLPResult, dedup)
    correlation_scorer.py  -> CorrelationScorer (weighted confluence formula)
    unified_pipeline.py     -> THIS FILE: SignalFactory + UnifiedPipeline

Re-export contract
-------------------
CLAUDE.md §5 describes `unified_pipeline.py` as containing `Event, Article,
NLPResult, NLPPipeline, SignalFactory, CorrelationScorer, UnifiedPipeline` —
that literal phrasing predates the circular-import split that moved the
shared dataclasses into `event_types.py` (see that module's docstring). To
keep `from unified_pipeline import Event` (etc.) working for downstream
consumers (`api_service.py` in Step 7, `event_bus.py` in Step 6, tests), this
module re-exports everything CLAUDE.md's file manifest names, without
redefining or wrapping any of it:

    from event_types import Event, Article, NLPResult
    from nlp_pipeline import NLPPipeline
    from correlation_scorer import CorrelationScorer, CorrelationResult

This module owns two new things only:
    - `SignalFactory`: one typed constructor per non-news signal category
      (FR-02), each producing a plain canonical `Event` — no new top-level
      fields, ever (AD-01). Type-specific data always goes in `meta`.
    - `UnifiedPipeline`: a thin orchestrator (append-only in-memory event
      store for Phase 1 — DB-backed persistence is explicitly deferred to
      `api_service.py`, Step 7, per CLAUDE.md §7's Phase 1 TODO list) that
      wires `NLPPipeline` output and `SignalFactory`/pre-built `Event`s into
      `CorrelationScorer`, and builds alert payloads per FR-19.

Nothing in this module contains scoring logic — that would be scope leak
into `correlation_scorer.py`'s territory. `UnifiedPipeline.correlate()` /
`correlate_all()` / `alert_payloads()` are pure delegation + thin
aggregation over `CorrelationScorer.correlate()` results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

# -- Re-exports (see module docstring "Re-export contract") -----------------
from event_types import Article, Event, NLPResult
from nlp_pipeline import NLPPipeline
from correlation_scorer import (
    CorrelationResult,
    CorrelationScorer,
    UnknownSignalTypeError,
    WeightedSignal,
)

# ---------------------------------------------------------------------------
# SignalFactory — one typed constructor per non-news signal category (FR-02)
# ---------------------------------------------------------------------------


class SignalFactory:
    """Typed `Event` constructors for the 11 non-news signal categories.

    Every constructor here is a thin, typed wrapper around `Event(...)` — it
    does not duplicate `Event.__post_init__`'s validation (asset/direction/
    confidence/timestamp checks), it just gives each signal category a
    named-parameter surface for the fields that make sense for that type,
    with type-specific extras folded into `meta` (AD-01: new signal-type
    data belongs in `meta`, never as a new top-level `Event` attribute).

    Each `event_type` string below matches an entry already registered in
    `correlation_scorer.SIGNAL_WEIGHTS` (Step 4) — these 11 are the complete
    set from CLAUDE.md FR-02 / `event_types.NON_NEWS_EVENT_TYPES`. Adding a
    12th constructor here without first adding its weight to CLAUDE.md §9's
    Signal Weight Reference (and `SIGNAL_WEIGHTS`) is explicitly out of
    scope for this step — see this module's task spec / CLAUDE.md's
    non-negotiable invariant #4.

    All constructors are `staticmethod`s: they hold no state, they just
    shape inputs into an `Event`. `**extra_meta` on every constructor lets
    callers attach genuinely one-off fields without needing a signature
    change for every new attribute a particular source happens to send.
    """

    # -- greek_anomaly --------------------------------------------------
    @staticmethod
    def greek_anomaly(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        greek: Optional[str] = None,
        z_score: Optional[float] = None,
        option_symbol: Optional[str] = None,
        **extra_meta: Any,
    ) -> Event:
        """An options Greek (Delta/Gamma/Theta/Vega/Rho) statistical outlier.

        `greek`: which Greek triggered the anomaly, e.g. "gamma", "vega".
        `z_score`: standard-deviations-from-normal magnitude of the outlier.
        `option_symbol`: the specific contract/OCC symbol, if applicable.
        """
        meta = {
            "greek": greek,
            "z_score": z_score,
            "option_symbol": option_symbol,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="greek_anomaly",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- options_flow -----------------------------------------------------
    @staticmethod
    def options_flow(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        contract_type: Optional[str] = None,
        strike: Optional[float] = None,
        expiry: Optional[str] = None,
        premium: Optional[float] = None,
        **extra_meta: Any,
    ) -> Event:
        """Unusual options activity (sweep/block, large premium flow).

        `contract_type`: "call" or "put".
        `strike`: strike price.
        `expiry`: ISO date string (or exchange-native expiry code).
        `premium`: total premium paid, in USD.
        """
        meta = {
            "contract_type": contract_type,
            "strike": strike,
            "expiry": expiry,
            "premium": premium,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="options_flow",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- strategy_break -----------------------------------------------------
    @staticmethod
    def strategy_break(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        strategy_name: Optional[str] = None,
        level: Optional[float] = None,
        break_type: Optional[str] = None,
        **extra_meta: Any,
    ) -> Event:
        """A price/level break of a defined strategy setup (e.g. a
        breakout/breakdown of a support/resistance or backtested rule level).

        `strategy_name`: identifier of the strategy/rule that fired.
        `level`: the price level that was broken.
        `break_type`: e.g. "breakout", "breakdown", "trendline_break".
        """
        meta = {
            "strategy_name": strategy_name,
            "level": level,
            "break_type": break_type,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="strategy_break",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- on_chain_move -----------------------------------------------------
    @staticmethod
    def on_chain_move(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        metric: Optional[str] = None,
        value: Optional[float] = None,
        change_pct: Optional[float] = None,
        **extra_meta: Any,
    ) -> Event:
        """An on-chain metric anomaly (exchange netflow, active addresses,
        MVRV, SOPR, etc. — Glassnode/Nansen/CryptoQuant-style signals).

        `metric`: metric name, e.g. "exchange_netflow", "mvrv_zscore".
        `value`: the metric's raw value at trigger time.
        `change_pct`: percentage change that triggered the signal.
        """
        meta = {
            "metric": metric,
            "value": value,
            "change_pct": change_pct,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="on_chain_move",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- funding_extreme -----------------------------------------------------
    @staticmethod
    def funding_extreme(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        funding_rate: Optional[float] = None,
        exchange: Optional[str] = None,
        open_interest: Optional[float] = None,
        **extra_meta: Any,
    ) -> Event:
        """An extreme perpetual-futures funding rate reading (CoinGlass-style).

        `funding_rate`: the funding rate (e.g. as a decimal, 0.01 = 1%).
        `exchange`: venue the reading came from, e.g. "binance", "bybit".
        `open_interest`: OI in USD at the time of the reading, if known.
        """
        meta = {
            "funding_rate": funding_rate,
            "exchange": exchange,
            "open_interest": open_interest,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="funding_extreme",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- whale_transfer -----------------------------------------------------
    @staticmethod
    def whale_transfer(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        amount_usd: Optional[float] = None,
        from_label: Optional[str] = None,
        to_label: Optional[str] = None,
        **extra_meta: Any,
    ) -> Event:
        """A large on-chain wallet-to-wallet/exchange transfer (Arkham/
        Whale Alert-style signal).

        `amount_usd`: transfer size in USD.
        `from_label`: origin wallet label, e.g. "binance_hot_wallet".
        `to_label`: destination wallet label, e.g. "unknown_wallet".
        """
        meta = {
            "amount_usd": amount_usd,
            "from_label": from_label,
            "to_label": to_label,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="whale_transfer",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- macro_event -----------------------------------------------------
    @staticmethod
    def macro_event(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        indicator: Optional[str] = None,
        actual: Optional[float] = None,
        forecast: Optional[float] = None,
        previous: Optional[float] = None,
        **extra_meta: Any,
    ) -> Event:
        """A macro data release or central-bank event (CPI, FOMC, jobs
        report, GDP, etc.).

        `indicator`: name of the macro release, e.g. "CPI", "FOMC_rate".
        `actual`/`forecast`/`previous`: the standard macro-release trio.
        """
        meta = {
            "indicator": indicator,
            "actual": actual,
            "forecast": forecast,
            "previous": previous,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="macro_event",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- political_trade -----------------------------------------------------
    @staticmethod
    def political_trade(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        politician: Optional[str] = None,
        committee: Optional[str] = None,
        transaction_type: Optional[str] = None,
        amount_range: Optional[str] = None,
        **extra_meta: Any,
    ) -> Event:
        """A disclosed congressional/political trade (STOCK Act filing).

        `politician`: filer's name.
        `committee`: relevant committee assignment, if known (feeds OQ-03's
            future earnings-blackout weight boost — stored here even though
            that boost isn't implemented until Phase 2).
        `transaction_type`: e.g. "buy", "sell", "exchange".
        `amount_range`: disclosed range string, e.g. "$15,001 - $50,000".
        """
        meta = {
            "politician": politician,
            "committee": committee,
            "transaction_type": transaction_type,
            "amount_range": amount_range,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="political_trade",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- insider_trade -----------------------------------------------------
    @staticmethod
    def insider_trade(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        insider_name: Optional[str] = None,
        title: Optional[str] = None,
        shares: Optional[float] = None,
        transaction_type: Optional[str] = None,
        **extra_meta: Any,
    ) -> Event:
        """A disclosed corporate insider trade (SEC Form 4).

        `insider_name`: filer's name.
        `title`: filer's role, e.g. "CEO", "Director", "10% Owner".
        `shares`: number of shares transacted.
        `transaction_type`: e.g. "buy", "sell", "option_exercise".
        """
        meta = {
            "insider_name": insider_name,
            "title": title,
            "shares": shares,
            "transaction_type": transaction_type,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="insider_trade",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- ml_forecast -----------------------------------------------------
    @staticmethod
    def ml_forecast(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        model_name: Optional[str] = None,
        predicted_price: Optional[float] = None,
        horizon_days: Optional[int] = None,
        **extra_meta: Any,
    ) -> Event:
        """A typed prediction emitted by `forecast_lab.py` (LSTM/RNN/ARIMA/
        sklearn — AD-12: model identity must never leak past this
        constructor into the scorer).

        `model_name`: e.g. "lstm_v3", "arima_1_1_1".
        `predicted_price`: point forecast.
        `horizon_days`: forecast horizon in days.
        """
        meta = {
            "model_name": model_name,
            "predicted_price": predicted_price,
            "horizon_days": horizon_days,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="ml_forecast",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )

    # -- ta_signal -----------------------------------------------------
    @staticmethod
    def ta_signal(
        asset: str,
        asset_class: str,
        direction: int,
        confidence: float,
        timestamp: datetime,
        source: str,
        *,
        indicator: Optional[str] = None,
        trigger_condition: Optional[str] = None,
        value: Optional[float] = None,
        **extra_meta: Any,
    ) -> Event:
        """A technical-analysis indicator trigger (`ta_engine.py`, Phase 2 —
        RSI/MACD/Ichimoku/SMA/EMA/MFI/OBV/VWAP/Momentum).

        `indicator`: e.g. "RSI", "MACD", "Ichimoku".
        `trigger_condition`: e.g. "oversold", "bullish_cross", "breakout".
        `value`: the indicator's raw value at trigger time.
        """
        meta = {
            "indicator": indicator,
            "trigger_condition": trigger_condition,
            "value": value,
            **extra_meta,
        }
        return Event(
            asset=asset,
            asset_class=asset_class,
            event_type="ta_signal",
            direction=direction,
            confidence=confidence,
            timestamp=timestamp,
            source=source,
            meta=meta,
        )


# ---------------------------------------------------------------------------
# UnifiedPipeline — thin orchestrator
# ---------------------------------------------------------------------------

#: Sentiment -> direction mapping thresholds (documented per task spec).
#: `NLPResult.sentiment` is a continuous score in [-1.0, 1.0]; `Event.direction`
#: must be a discrete {-1, 0, 1} (AD-01, never a raw float). A small deadband
#: around 0 is used (rather than a bare `sentiment > 0` / `< 0` split) so that
#: weak/ambiguous lexicon sentiment (e.g. a single incidental word match
#: producing sentiment ~= 0.05) is treated as genuinely neutral rather than
#: forced into a directional claim it doesn't actually support. +/-0.1 is a
#: deliberately small deadband — wide enough to absorb lexicon noise near
#: zero, narrow enough that any article with a real net bullish/bearish
#: lexicon signal (which, per `score_sentiment`'s own normalization, tends to
#: land well outside +/-0.1 whenever there's more than a single weak match)
#: still gets a directional Event.
_SENTIMENT_BULLISH_THRESHOLD = 0.1
_SENTIMENT_BEARISH_THRESHOLD = -0.1

#: Severity bands for `alert_payloads()`, keyed on `abs(confluence_score)`.
#: FR-19's alert threshold default is 0.65 — anything below that never
#: reaches `alert_payloads()` in the first place, so bands start at 0.65.
#: Three bands (medium/high/critical) mirror the severity vocabulary already
#: used elsewhere in CLAUDE.md (§5 WebSocketAlertPanel: "severity badge
#: (low/medium/high/critical)"; `alerts` table: "severity"). "low" is not
#: reachable here since anything below the 0.65 gate never becomes an alert
#: payload at all — "low" severity, if ever surfaced, belongs to a
#: below-threshold informational view, not this gated alert list.
#:   [0.65, 0.80)  -> "medium"
#:   [0.80, 0.92)  -> "high"
#:   [0.92, 1.00]  -> "critical"
_SEVERITY_BANDS: tuple[tuple[float, str], ...] = (
    (0.92, "critical"),
    (0.80, "high"),
    (0.0, "medium"),
)

#: Number of top contributing signals to surface per alert payload, per
#: CLAUDE.md §5's `WebSocketAlertPanel.tsx` spec: "top-4 contributing signals
#: per alert".
_TOP_CONTRIBUTORS_COUNT = 4


def _map_sentiment_to_direction(sentiment: float) -> int:
    """Map a continuous NLPResult.sentiment in [-1, 1] to a discrete
    Event.direction in {-1, 0, 1}, per the documented deadband thresholds.
    """
    if sentiment > _SENTIMENT_BULLISH_THRESHOLD:
        return 1
    if sentiment < _SENTIMENT_BEARISH_THRESHOLD:
        return -1
    return 0


def _map_to_confidence(sentiment: float, urgency: float) -> float:
    """Derive Event.confidence from NLPResult.sentiment/urgency.

    Documented mapping: confidence = 0.7 * |sentiment| + 0.3 * urgency,
    clamped to [0.0, 1.0]. Rationale: `|sentiment|` magnitude is the primary
    driver of how strongly this article should count toward confluence (a
    strongly bullish or bearish article is more actionable than a
    borderline one), while `urgency` (independent of direction per
    `score_urgency`'s own docstring) contributes a secondary boost — a
    breaking/fresh article is more actionable than stale news with
    identical sentiment magnitude, but urgency alone should never be able to
    manufacture strong confidence out of a near-neutral article (hence the
    0.7/0.3 split rather than an unweighted average). A neutral-sentiment,
    zero-urgency article still yields confidence == 0.0, which is a
    meaningful, valid "no signal strength" value (see
    `CorrelationScorer.correlate()`'s own documented zero-denominator edge
    case) rather than an error.
    """
    confidence = 0.7 * abs(sentiment) + 0.3 * urgency
    return max(0.0, min(1.0, confidence))


def _severity_for(abs_score: float) -> str:
    for band_floor, label in _SEVERITY_BANDS:
        if abs_score >= band_floor:
            return label
    return _SEVERITY_BANDS[-1][1]  # unreachable given the 0.0 floor, but safe


class UnifiedPipeline:
    """Thin orchestrator merging news-derived and non-news `Event` streams.

    Storage is a simple append-only in-memory list for Phase 1 (per
    CLAUDE.md §7's Phase 1 TODO "Replace in-memory UnifiedPipeline with
    DB-backed event store in api_service.py" — that replacement is
    explicitly Step 7's job, not this module's). Nothing here ever mutates
    or removes a previously stored `Event` (NFR-03) — `ingest_articles()`
    and `ingest_signal_events()` only ever append.

    KNOWN LIMITATION, tracked explicitly for Step 7 (review finding, not
    fixed here since the real fix is the DB-backed rewrite this class is
    already scheduled to be replaced by): `self._articles` grows unbounded
    for the lifetime of an instance, and every `ingest_articles()` call
    dedup-scans the FULL accumulated corpus (`NLPPipeline.process()`'s
    tier-2 near-dupe check re-extracts tickers per candidate). This is
    algorithmically O(n) per call / effectively O(n²) across a session, not
    just "needs a database" — moving storage to Postgres alone won't fix
    this unless the dedup query itself becomes bounded/windowed or indexed
    (e.g. only scan recent articles sharing a ticker, per the `nlp-pipeline`
    skill's own scoping guidance, rather than "every article ever seen").
    Step 7 should redesign the dedup-scope query, not just relocate this
    same full-scan loop to SQL.

    This class contains zero scoring logic — `correlate()`/`correlate_all()`/
    `alert_payloads()` all delegate to `CorrelationScorer`, which is the only
    place the confluence formula (AD-04) lives. If a future change requires
    computing anything about *how* a score is derived (not just aggregating/
    filtering already-computed `CorrelationResult`s), that belongs in
    `correlation_scorer.py`, not here.
    """

    def __init__(
        self,
        scorer: Optional[CorrelationScorer] = None,
        crypto_tickers: Optional[set[str]] = None,
    ) -> None:
        self._scorer = scorer if scorer is not None else CorrelationScorer()
        self._events: list[Event] = []
        # Accumulates every Article ever passed to ingest_articles (whether
        # or not it turned out to be a duplicate) so a session's dedup corpus
        # grows across calls without the caller having to re-thread it
        # themselves. Documented explicitly (per task spec) rather than left
        # as an implicit surprise: callers who want fully caller-managed
        # corpus-scoping can still pass their own `corpus=` argument each
        # call — that argument is *added to*, not replaced by, this internal
        # accumulation (see `ingest_articles` docstring).
        self._articles: list[Article] = []
        # Overridable crypto-symbol allowlist for `_infer_asset_class` (see
        # that function's docstring for why this needs to be overridable —
        # review finding: a fixed small default silently misclassifies any
        # crypto ticker outside it, corrupting half-life selection).
        self._crypto_tickers = (
            frozenset(crypto_tickers) if crypto_tickers is not None else None
        )

    # -- accessors --------------------------------------------------------

    @property
    def events(self) -> list[Event]:
        """A defensive copy of every `Event` stored so far (append-only).

        Returns a new list each call so callers can't accidentally mutate
        the pipeline's internal storage by holding onto and modifying the
        returned list; `Event` instances themselves are already immutable
        (frozen dataclass + `FrozenDict` meta).
        """
        return list(self._events)

    @property
    def articles(self) -> list[Article]:
        """A defensive copy of every `Article` ever ingested this session."""
        return list(self._articles)

    # -- ingestion ----------------------------------------------------------

    def ingest_articles(
        self,
        articles: list[Article],
        corpus: Optional[list[Article]] = None,
    ) -> list[Event]:
        """Run each `Article` through `NLPPipeline`, emit `Event`s for
        non-duplicate results.

        Dedup scope: each article is checked against `corpus` (if supplied)
        PLUS every `Article` this `UnifiedPipeline` instance has already
        ingested this session (accumulated internally in `self._articles`)
        PLUS every article already processed earlier *within this same
        call* (so duplicates inside one batch are also caught, not just
        duplicates against prior calls). Every article passed in — duplicate
        or not — is appended to `self._articles` for future calls'
        corpus-scoping, since a duplicate article is still a genuine
        observation that should suppress a future near-duplicate from
        re-firing (NFR-03: raw payloads are retained, never discarded).

        For each non-duplicate `NLPResult` with at least one extracted
        ticker, constructs one `Event` per ticker with `event_type="news"`.
        Articles with zero extracted tickers produce zero Events (nothing to
        correlate against an asset) but are still retained in
        `self._articles` for dedup purposes.

        Direction is derived from `NLPResult.sentiment` via
        `_map_sentiment_to_direction` (deadband thresholds documented on
        that function). Confidence is derived from `NLPResult.sentiment`
        magnitude and `NLPResult.urgency` via `_map_to_confidence` (weights
        documented on that function). `topic`, `urgency`, `sentiment`,
        `is_duplicate`/`duplicate_of`, and the source article's `url` are
        stored in `Event.meta` — never as new top-level `Event` fields
        (AD-01).

        Returns the list of newly created `Event`s (empty list if every
        article was a duplicate or had no extractable tickers). These
        Events are also appended to internal storage (append-only, NFR-03).
        """
        nlp = NLPPipeline()
        session_corpus: list[Article] = list(corpus) if corpus else []
        session_corpus.extend(self._articles)

        new_events: list[Event] = []

        for article in articles:
            result = nlp.process(article, corpus=session_corpus)

            # Every article is retained for future dedup scope, whether or
            # not it's a duplicate (NFR-03 — raw payloads are never
            # discarded) and whether or not it fires an Event.
            self._articles.append(article)
            session_corpus.append(article)

            if result.is_duplicate:
                continue
            if not result.tickers:
                continue

            direction = _map_sentiment_to_direction(result.sentiment)
            confidence = _map_to_confidence(result.sentiment, result.urgency)

            for ticker in result.tickers:
                event = Event(
                    asset=ticker,
                    asset_class=_infer_asset_class(ticker, self._crypto_tickers),
                    event_type="news",
                    direction=direction,
                    confidence=confidence,
                    timestamp=article.published_at,
                    source=article.source,
                    meta={
                        "headline": article.headline,
                        "url": article.url,
                        "topic": result.topic,
                        "sentiment": result.sentiment,
                        "urgency": result.urgency,
                        "is_duplicate": result.is_duplicate,
                        "duplicate_of": result.duplicate_of,
                    },
                )
                new_events.append(event)

        self._events.extend(new_events)
        return new_events

    def ingest_signal_events(self, events: list[Event]) -> list[Event]:
        """Append pre-constructed `Event`s (e.g. from `SignalFactory`) to
        internal storage.

        Every `Event` already self-validates via `Event.__post_init__`
        (immutable frozen dataclass with a frozen `meta`), so this method's
        job is purely storage bookkeeping: type-check that each item really
        is an `Event` (defends against a caller accidentally passing a dict
        or some other shape) and append it. Append-only (NFR-03) — this
        method never replaces or removes a previously stored `Event`.

        Returns the same list that was appended, for convenience/chaining.

        Raises:
            TypeError: if any item in `events` is not an `Event` instance.
        """
        validated: list[Event] = []
        for ev in events:
            if not isinstance(ev, Event):
                raise TypeError(
                    f"ingest_signal_events expects Event instances; got {type(ev)!r}"
                )
            validated.append(ev)

        self._events.extend(validated)
        return validated

    # -- correlation ----------------------------------------------------------

    def correlate(self, asset: str, now: Optional[datetime] = None) -> CorrelationResult:
        """Delegate to `CorrelationScorer.correlate()` over all stored Events
        for `asset`.

        `CorrelationScorer.correlate()` already filters its input down to
        `event.asset == asset` internally, so the full stored event list is
        passed through unfiltered here — no duplication of that filtering
        logic in this class.
        """
        return self._scorer.correlate(asset, self._events, now=now)

    def correlate_all(
        self, now: Optional[datetime] = None
    ) -> dict[str, CorrelationResult]:
        """`correlate()` for every distinct asset seen across stored Events.

        Returns a dict keyed by asset symbol. If no Events have been
        ingested yet, returns an empty dict (there is nothing to correlate,
        which is a valid, non-error state).
        """
        assets = sorted({ev.asset for ev in self._events})
        return {asset: self.correlate(asset, now=now) for asset in assets}

    # -- alerting ----------------------------------------------------------

    def alert_payloads(
        self,
        threshold: float = 0.65,
        min_signals: int = 3,
        now: Optional[datetime] = None,
    ) -> list[dict]:
        """Build alert payload dicts for every asset clearing FR-19's gates.

        Per FR-19: fires when BOTH
            `abs(confluence_score) >= threshold` (default 0.65)   AND
            `signal_count >= min_signals`        (default 3)
        are true — both conditions are ANDed, matching FR-19's exact
        wording; neither gate alone is sufficient.

        Each payload dict contains, at minimum:
            asset:              the asset symbol
            confluence_score:   `CorrelationResult.confluence_score`
            signal_count:       `CorrelationResult.signal_count`
            severity:           banded from `abs(confluence_score)` — see
                                 `_SEVERITY_BANDS` for the exact thresholds
                                 (documented above that constant)
            agreement_ratio:    passed through from `CorrelationResult`
            conflict_ratio:     passed through from `CorrelationResult`
            top_contributors:   up to `_TOP_CONTRIBUTORS_COUNT` (4) entries
                                 from `CorrelationResult.weighted_signals`,
                                 sorted by `abs(contribution)` descending —
                                 per CLAUDE.md §5's WebSocketAlertPanel spec
                                 ("top-4 contributing signals per alert").
                                 Each contributor entry is a small dict
                                 (event_type, asset, direction, confidence,
                                 weight, decay, contribution) rather than a
                                 raw `WeightedSignal`/`Event` object, so this
                                 payload is directly JSON-serializable for
                                 the WebSocket panel / Slack/Telegram alert
                                 routers (Step 6/7) without further
                                 transformation.

        Results are sorted by `abs(confluence_score)` descending (most
        urgent alerts first), mirroring `detect_dark_signals`'s own
        "highest-priority first" ordering convention.
        """
        results = self.correlate_all(now=now)

        payloads: list[dict] = []
        for asset, result in results.items():
            if abs(result.confluence_score) < threshold:
                continue
            if result.signal_count < min_signals:
                continue

            top_signals = sorted(
                result.weighted_signals,
                key=lambda ws: abs(ws.contribution),
                reverse=True,
            )[:_TOP_CONTRIBUTORS_COUNT]

            top_contributors = [
                {
                    "event_type": ws.event.event_type,
                    "asset": ws.event.asset,
                    "source": ws.event.source,
                    "direction": ws.direction,
                    "confidence": ws.confidence,
                    "weight": ws.weight,
                    "decay": ws.decay,
                    "contribution": ws.contribution,
                }
                for ws in top_signals
            ]

            payloads.append(
                {
                    "asset": asset,
                    "confluence_score": result.confluence_score,
                    "signal_count": result.signal_count,
                    "severity": _severity_for(abs(result.confluence_score)),
                    "agreement_ratio": result.agreement_ratio,
                    "conflict_ratio": result.conflict_ratio,
                    "top_contributors": top_contributors,
                }
            )

        payloads.sort(key=lambda p: abs(p["confluence_score"]), reverse=True)
        return payloads


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

#: Default crypto-symbol allowlist used to infer `asset_class` for
#: news-derived Events, since `NLPResult`/`Article` carry no asset_class of
#: their own (news articles mention tickers across asset classes freely).
#: Kept local (not imported from nlp_pipeline) since this is a
#: UnifiedPipeline-only concern (asset class inference for Event
#: construction), not an NLP concern.
#:
#: Review finding fixed here: a smaller 10-symbol version of this set
#: silently misclassified any crypto ticker outside it (e.g. SHIB, PEPE,
#: ARB, UNI, AAVE, LTC, ATOM, NEAR) as "equity", which routes the resulting
#: news Event through correlation_scorer.get_half_life_minutes("equity") ->
#: 180-minute half-life instead of the correct 90-minute crypto half-life
#: (AD-03/OQ-02) -- a real, demonstrated confluence-score distortion, not
#: just a cosmetic mislabel. Expanded to a much broader real-world set AND
#: made overridable per-instance (see `UnifiedPipeline.__init__`'s
#: `crypto_tickers` parameter) so a caller with a live, larger universe
#: (e.g. once Step 7 wires the `assets` table) isn't stuck with this
#: hardcoded default.
_CRYPTO_TICKERS = frozenset(
    {
        "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
        "MATIC", "SHIB", "PEPE", "ARB", "OP", "SUI", "APT", "TON", "BNB",
        "TRX", "LTC", "ATOM", "NEAR", "FIL", "ICP", "UNI", "AAVE", "MKR",
        "CRV", "LDO", "RUNE", "INJ", "TIA", "SEI", "STX", "IMX", "GRT",
        "SAND", "MANA", "AXS", "FTM", "ALGO", "XLM", "HBAR", "VET", "EGLD",
        "XMR", "ETC", "BCH", "USDT", "USDC", "DAI",
    }
)


def _infer_asset_class(
    ticker: str, crypto_tickers: Optional[frozenset[str]] = None
) -> str:
    """Infer `Event.asset_class` for a news-derived ticker.

    Defaults to "equity" for anything not in the known crypto set — a
    reasonable default given CLAUDE.md's demo assets (BTC, SPY, NVDA) skew
    equity-heavy, and equity is also the fallback half-life class in
    `correlation_scorer.get_half_life_minutes` for any asset_class it
    doesn't explicitly recognize as "crypto". In production this should be
    replaced by a lookup against the `assets` table (schema.sql), which
    carries a real `asset_class` column per symbol -- this remains a
    documented stopgap, not a permanent design, even after the expanded
    default set above.

    Args:
        ticker: The ticker symbol to classify.
        crypto_tickers: Override allowlist. Falls back to the module-level
            `_CRYPTO_TICKERS` default if omitted.
    """
    allowlist = crypto_tickers if crypto_tickers is not None else _CRYPTO_TICKERS
    return "crypto" if ticker in allowlist else "equity"


__all__ = [
    # Re-exports (see module docstring "Re-export contract")
    "Event",
    "Article",
    "NLPResult",
    "NLPPipeline",
    "CorrelationScorer",
    "CorrelationResult",
    "WeightedSignal",
    "UnknownSignalTypeError",
    # This module's own additions
    "SignalFactory",
    "UnifiedPipeline",
]
