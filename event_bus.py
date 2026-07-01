"""Trade-Dash's Redis event bus — Step 6 of the Phase 1 build.

Implements CLAUDE.md AD-07 (Redis as event bus + cache) and the Phase 1
pieces of §5's `event_bus.py` spec:

    EventBus            -- thin async wrapper over `redis.asyncio` pub/sub,
                           rolling 500-event buffer, per-asset alert cache,
                           feed-health heartbeat/read.
    FeedPoller          -- abstract poll-loop base class.
    FinnhubNewsPoller   -- news articles -> Article -> UnifiedPipeline ->
                           Event(event_type="news").
    CoinGlassFundingPoller -- funding-rate readings -> SignalFactory.funding_extreme.
    PolygonOptionsPoller   -- options flow -> SignalFactory.options_flow.
    AlertRouter         -- subscribes to `alerts`, dispatches high/critical
                           severity to Slack/Telegram (FR-13/FR-20).

Explicitly NOT this module's job (per CLAUDE.md §5/§9 and the event-bus
skill):
    - Event/Article/NLPResult construction logic (owned by event_types.py /
      nlp_pipeline.py / unified_pipeline.py) -- pollers only call into those,
      never reimplement dedup/sentiment/ticker-extraction here.
    - CorrelationScorer invocation -- pollers publish raw Events onto
      `events`; scoring is triggered by a separate scoring-loop consumer of
      that channel (not built in this step). A poller must NEVER call
      CorrelationScorer directly.
    - WebSocket broadcast -- `api_service.py` (Step 7) subscribes to
      `alerts` itself for that; `AlertRouter` here only handles the
      Slack/Telegram severity-gated side of FR-13/FR-20.

Redis channel/key contract (AD-07, NFR-06):
    Channel "events"        -- every ingested Event, JSON-serialized, published
                               here regardless of source.
    Key     "events:recent" -- Redis LIST, capped at 500 entries (LPUSH +
                               LTRIM) so late-joining consumers get recent
                               context (AD-07).
    Channel "alerts"        -- alert_payloads()-shaped dicts, JSON-serialized,
                               post-CorrelationScorer.
    Key     "alerts:asset:{asset}" -- per-asset alert cache, TTL 1 hour.
    Key     "feed_health:{source}" -- heartbeat key, TTL EXACTLY 5 minutes
                               (300 seconds) -- NFR-06 is a hard requirement,
                               not a suggestion. A longer TTL means a
                               genuinely dead feed reports healthy for too
                               long; a shorter TTL would cause false-stale
                               flapping between legitimate poll cycles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from event_types import Article, Event
from unified_pipeline import SignalFactory, UnifiedPipeline

logger = logging.getLogger("event_bus")

# ---------------------------------------------------------------------------
# Redis channel / key constants (AD-07, NFR-06)
# ---------------------------------------------------------------------------

EVENTS_CHANNEL = "events"
ALERTS_CHANNEL = "alerts"

#: Rolling buffer of raw Events for late-joining consumers (AD-07).
EVENTS_BUFFER_KEY = "events:recent"
EVENTS_BUFFER_MAX_LEN = 500

#: Per-asset scored-alert cache TTL (AD-07): 1 hour.
ALERT_CACHE_TTL_SECONDS = 60 * 60

#: Feed-health heartbeat TTL (NFR-06): EXACTLY 5 minutes. Hard requirement --
#: a longer TTL means a genuinely dead feed reports healthy for too long.
FEED_HEALTH_TTL_SECONDS = 5 * 60

#: Key prefix for per-source feed-health heartbeats.
FEED_HEALTH_KEY_PREFIX = "feed_health:"


def _feed_health_key(source: str) -> str:
    return f"{FEED_HEALTH_KEY_PREFIX}{source}"


def _alert_cache_key(asset: str) -> str:
    return f"alerts:asset:{asset}"


def _event_to_json(event: Event) -> str:
    """Serialize an `Event` to a JSON string.

    `timestamp` -> ISO 8601 string (JSON has no native datetime type);
    `meta` is a `FrozenDict` (dict subclass) so `dict(event.meta)` round-trips
    it through `json.dumps` cleanly without needing a custom encoder for that
    field specifically. Uses a plain dict literal rather than
    `dataclasses.asdict(event)` because `asdict` would recurse into `meta`
    trying to find nested dataclasses/deep-copy it, which is unnecessary
    (meta is already a flat JSON-safe dict of primitives per AD-01) and would
    fight with `FrozenDict`'s deliberately-blocked mutation methods during
    `copy.deepcopy`.
    """
    payload = {
        "asset": event.asset,
        "asset_class": event.asset_class,
        "event_type": event.event_type,
        "direction": event.direction,
        "confidence": event.confidence,
        "timestamp": event.timestamp.isoformat(),
        "source": event.source,
        "meta": dict(event.meta),
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """Thin async wrapper over `redis.asyncio` pub/sub + feed-health cache.

    Accepts an already-constructed Redis client (any object exposing the
    `redis.asyncio.Redis` surface this class actually calls: `publish`,
    `lpush`, `ltrim`, `setex`, `keys`, `exists`, `get`, `pubsub`) rather than
    constructing one itself from `REDIS_URL` internally -- this is what makes
    the class trivially testable against a fake/in-memory Redis substitute
    without monkeypatching a module-level connection. Production callers
    construct the real client themselves (see `build_event_bus_from_env`)
    and pass it in.
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    # -- events channel + rolling buffer (AD-07) ---------------------------

    async def publish_event(self, event: Event) -> None:
        """Publish an `Event` to the `events` channel and push it onto the
        rolling 500-event buffer (LPUSH + LTRIM) for late-joining consumers.
        """
        payload = _event_to_json(event)
        await self._redis.publish(EVENTS_CHANNEL, payload)
        await self._redis.lpush(EVENTS_BUFFER_KEY, payload)
        # LTRIM keeps indices [0, EVENTS_BUFFER_MAX_LEN - 1] -- i.e. caps the
        # list at EVENTS_BUFFER_MAX_LEN entries, newest first (LPUSH prepends).
        await self._redis.ltrim(EVENTS_BUFFER_KEY, 0, EVENTS_BUFFER_MAX_LEN - 1)

    async def get_recent_events(self) -> list[dict]:
        """Return the rolling buffer's contents (newest first) as parsed
        dicts -- what a late-joining consumer would replay for context.
        """
        raw = await self._redis.lrange(EVENTS_BUFFER_KEY, 0, -1)
        return [json.loads(item) for item in raw]

    # -- alerts channel + per-asset cache (AD-07) --------------------------

    async def publish_alert(self, alert_payload: dict) -> None:
        """Publish an `alert_payloads()`-shaped dict to `alerts`, and cache
        it per-asset with a 1-hour TTL (AD-07).
        """
        payload_json = json.dumps(alert_payload)
        await self._redis.publish(ALERTS_CHANNEL, payload_json)

        asset = alert_payload.get("asset")
        if asset:
            await self._redis.setex(
                _alert_cache_key(asset), ALERT_CACHE_TTL_SECONDS, payload_json
            )

    async def subscribe_alerts(self) -> AsyncIterator[dict]:
        """Subscribe to the `alerts` channel; yield parsed payloads as they
        arrive. Runs until the caller stops iterating (e.g. via `break` or
        task cancellation) -- callers own the pubsub's lifetime.
        """
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(ALERTS_CHANNEL)
        try:
            async for message in pubsub.listen():
                if message is None:
                    continue
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if data is None:
                    continue
                if isinstance(data, (bytes, bytearray)):
                    data = data.decode("utf-8")
                yield json.loads(data)
        finally:
            await pubsub.unsubscribe(ALERTS_CHANNEL)

    # -- feed health (NFR-06) ----------------------------------------------

    async def heartbeat(self, source: str) -> None:
        """Write/refresh a feed-health heartbeat key for `source`.

        TTL is EXACTLY `FEED_HEALTH_TTL_SECONDS` (300s / 5 min) -- NFR-06 is
        a hard requirement. The value is an ISO-8601 UTC timestamp (useful
        for `get_feed_health()`'s `last_seen`), but the exact value matters
        far less than the TTL: a stale-but-present key would still count as
        "healthy" until Redis expires it, which is exactly the intended
        behavior (a poller only needs to prove liveness at heartbeat time).
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._redis.setex(_feed_health_key(source), FEED_HEALTH_TTL_SECONDS, now_iso)

    async def get_feed_health(self) -> dict[str, dict]:
        """Return `{source: {"last_seen": <iso str or None>, "is_stale": bool}}`
        for every feed-health key currently present.

        Staleness is determined purely by Redis TTL auto-expiry (NFR-06): if
        a key is gone, that source is stale (or has never reported at all).
        This method only reports on sources that currently have (or very
        recently had) a key -- callers who need to assert a *specific*
        expected source list should diff against their own registry (see
        `freshness_guard.py`, which does exactly that).
        """
        keys = await self._redis.keys(f"{FEED_HEALTH_KEY_PREFIX}*")
        health: dict[str, dict] = {}
        for key in keys:
            key_str = key.decode("utf-8") if isinstance(key, (bytes, bytearray)) else key
            source = key_str[len(FEED_HEALTH_KEY_PREFIX):]
            value = await self._redis.get(key)
            if value is None:
                # Expired between `keys()` and `get()` -- treat as stale.
                health[source] = {"last_seen": None, "is_stale": True}
                continue
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8")
            health[source] = {"last_seen": value, "is_stale": False}
        return health

    async def is_source_fresh(self, source: str) -> bool:
        """Convenience check for a single source: True iff its feed-health
        key currently exists (has not expired).
        """
        return bool(await self._redis.exists(_feed_health_key(source)))


def build_event_bus_from_env() -> EventBus:
    """Construct an `EventBus` backed by a real `redis.asyncio.Redis` client,
    configured from the `REDIS_URL` env var (NFR-05 -- never hardcoded).

    Not used by the test suite (which injects a fake client directly), but
    this is the production entry point `run_all(...)` (or any future
    `if __name__ == "__main__"` block) should call.
    """
    import redis.asyncio as redis_asyncio

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = redis_asyncio.from_url(redis_url, decode_responses=True)
    return EventBus(client)


# ---------------------------------------------------------------------------
# FeedPoller base class
# ---------------------------------------------------------------------------


class FeedPoller(ABC):
    """Base class for every feed poller.

    Subclasses implement `poll_once()` (fetch from the source API, convert
    raw payloads into `Event`s -- via `SignalFactory` or `UnifiedPipeline`,
    never reimplementing that construction logic here). `run()` owns the
    interval loop and is the same for every poller:

        1. call `poll_once()`
        2. publish each returned Event via `event_bus.publish_event()`
        3. call `event_bus.heartbeat(self.source_name)` UNCONDITIONALLY,
           every cycle -- even when `poll_once()` returns zero new Events.
           A poll cycle that finds nothing new is still positive evidence
           the feed is alive (e.g. overnight crypto funding sitting inside
           normal bounds with no extreme reading to report) and MUST refresh
           the TTL. Making step 3 conditional on "did we get new data" is
           exactly the bug class the event-bus skill calls out by name --
           don't reintroduce it.
        4. sleep `interval_seconds`, repeat.

    `poll_once()` is wrapped in try/except inside `run()`: one poller's
    exception is logged and the loop backs off to the next interval rather
    than propagating and crashing the whole `event_bus.py` process -- one
    dead poller must never take down ingestion for other sources.
    """

    #: Subclasses must set this to a stable per-source identifier used as
    #: both the feed-health key suffix and the `Event.source` value.
    source_name: str = "unset"

    @abstractmethod
    async def poll_once(self) -> list[Event]:
        """Fetch from this poller's API and return newly observed `Event`s
        (empty list if nothing new this cycle -- that is a valid, expected
        result, not an error).
        """
        raise NotImplementedError

    async def run_one_cycle(self, event_bus: EventBus) -> list[Event]:
        """Run exactly one poll+publish+heartbeat cycle.

        `poll_once()` is wrapped in try/except: on an exception, it is
        logged and treated as zero Events for this cycle -- but the
        heartbeat is STILL written unconditionally afterward. Per the task
        spec (and the event-bus skill's explicit warning about the
        "heartbeat-only-on-new-data" bug class): every successful poll
        cycle -- including one that raised and was caught, and one that
        legitimately found zero new items -- is evidence of poller liveness
        (the loop is still running and will retry next interval) and MUST
        refresh the feed-health TTL. Only a process-level crash (which this
        try/except is specifically here to prevent) should ever cause a
        heartbeat to be missed.

        Returns whatever `poll_once()` produced (empty list on failure/no
        new data). This is the unit of work `run()` repeats on an interval;
        it also exists standalone so tests can exercise "one cycle" directly
        without fighting `run()`'s infinite loop / real `asyncio.sleep`.
        """
        try:
            events = await self.poll_once()
        except Exception:
            logger.exception(
                "%s.poll_once() raised; backing off and continuing "
                "(one dead poller must not crash ingestion for other sources)",
                type(self).__name__,
            )
            events = []

        for event in events:
            await event_bus.publish_event(event)

        # Unconditional -- see docstring above.
        await event_bus.heartbeat(self.source_name)
        return events

    async def run(self, event_bus: EventBus, interval_seconds: float) -> None:
        """Run the poll loop until cancelled.

        Runs indefinitely (`while True`) -- callers control lifetime via
        `asyncio.Task` cancellation (e.g. `asyncio.gather(...)` in
        `run_all()`, or a test harness that runs exactly one cycle by
        calling `run_one_cycle()` directly instead of `run()` -- see
        `tests/test_event_bus.py`).
        """
        while True:
            await self.run_one_cycle(event_bus)
            await asyncio.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# FinnhubNewsPoller (FR-01)
# ---------------------------------------------------------------------------


class FinnhubNewsPoller(FeedPoller):
    """Polls Finnhub's news endpoint, converts raw articles to `Event`s via
    `UnifiedPipeline.ingest_articles()` (NLP dedup/sentiment/ticker-extraction
    is Steps 3/5's job -- this class never reimplements it).

    Finnhub rate limits (per Finnhub's published API docs): the free tier is
    60 API calls/minute; paid tiers raise this ceiling but 60/min is the
    documented floor every plan supports. A 60-second poll interval is the
    safe default here -- comfortably under even the free tier's limit for a
    single endpoint polled by a single poller instance. Callers on a paid
    plan wanting tighter latency can pass a shorter `interval_seconds` to
    `run()`, but should not go below what their actual Finnhub plan allows.

    `_fetch_raw()` is the sole network call site and is deliberately a thin,
    separately-overridable method so tests can subclass/monkeypatch it to
    return canned fixture data without needing a live API key or a real
    aiohttp session.
    """

    source_name = "finnhub_news"

    #: Finnhub's real endpoint shape (category-based market news). Not
    #: guaranteed byte-perfect since this can't be tested against the live
    #: API in this environment -- see class docstring.
    API_URL = "https://finnhub.io/api/v1/news"

    def __init__(
        self,
        api_key: Optional[str] = None,
        category: str = "general",
        session: Optional[Any] = None,
        pipeline: Optional[UnifiedPipeline] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("FINNHUB_API_KEY")
        self.category = category
        self._session = session  # injected aiohttp.ClientSession, or None
        self.pipeline = pipeline if pipeline is not None else UnifiedPipeline()

    async def _fetch_raw(self) -> list[dict]:
        """Fetch raw article dicts from Finnhub. Real network call -- tests
        should monkeypatch/subclass this method rather than exercise it
        directly (no live Finnhub credentials in this sandbox).
        """
        import aiohttp

        params = {"category": self.category, "token": self.api_key}
        session = self._session
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession()
        try:
            async with session.get(self.API_URL, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()
        finally:
            if owns_session:
                await session.close()

    def _raw_to_articles(self, raw_items: list[dict]) -> list[Article]:
        """Convert Finnhub's raw article shape into `Article` instances.

        Finnhub news items look roughly like:
            {"headline": "...", "summary": "...", "url": "...",
             "datetime": <unix seconds>, "source": "..."}
        Fields are read defensively (`.get`) since this can't be verified
        against a live payload in this sandbox.
        """
        articles: list[Article] = []
        for item in raw_items:
            headline = item.get("headline") or item.get("title")
            if not headline:
                continue
            url = item.get("url") or ""
            if not url:
                continue
            ts = item.get("datetime")
            if isinstance(ts, (int, float)):
                published_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                published_at = datetime.now(timezone.utc)
            articles.append(
                Article(
                    headline=headline,
                    body=item.get("summary") or headline,
                    url=url,
                    published_at=published_at,
                    source="finnhub",
                )
            )
        return articles

    async def poll_once(self) -> list[Event]:
        raw_items = await self._fetch_raw()
        articles = self._raw_to_articles(raw_items)
        if not articles:
            return []
        return self.pipeline.ingest_articles(articles)


# ---------------------------------------------------------------------------
# CoinGlassFundingPoller (FR-02)
# ---------------------------------------------------------------------------


class CoinGlassFundingPoller(FeedPoller):
    """Polls CoinGlass's funding-rate endpoint, converts extreme readings to
    `Event`s via `SignalFactory.funding_extreme(...)`.

    CoinGlass rate limits: per CoinGlass's published API plans, the
    Hobbyist/Standard tiers are throttled per-minute (documented in the
    response headers of a live account, which this sandbox has no access
    to) -- a conservative 60-second poll interval is used as the safe
    default, matching funding-rate data's own natural update cadence (most
    perp venues settle funding every 1-8 hours; polling once a minute is
    already far more frequent than the underlying data changes, so this is
    bounded by data freshness as much as by rate limits).

    Threshold: a funding rate reading is "extreme" if
    `abs(funding_rate) > EXTREME_FUNDING_THRESHOLD` (default 0.0075 = 0.75%
    per 8h interval -- roughly the top few percent of historical BTC/ETH
    perp funding readings across major venues; well above the ~0.01%/8h
    "normal" range and comfortably below true blow-off extremes of 1%+ seen
    during squeezes). Documented here rather than derived from a live
    percentile calculation (which would need historical funding data this
    poller doesn't retain) -- a fixed threshold is the simplest correct
    Phase 1 behavior; Phase 2+ could compute a rolling percentile instead.
    """

    source_name = "coinglass_funding"

    API_URL = "https://open-api.coinglass.com/public/v2/funding"

    #: See class docstring for rationale.
    EXTREME_FUNDING_THRESHOLD = 0.0075

    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("COINGLASS_API_KEY")
        self._session = session

    async def _fetch_raw(self) -> list[dict]:
        """Fetch raw funding-rate reading dicts from CoinGlass. Real network
        call -- tests should monkeypatch/subclass this method.
        """
        import aiohttp

        headers = {"coinglassSecret": self.api_key} if self.api_key else {}
        session = self._session
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession()
        try:
            async with session.get(self.API_URL, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("data", []) if isinstance(data, dict) else data
        finally:
            if owns_session:
                await session.close()

    def _raw_to_events(self, raw_items: list[dict]) -> list[Event]:
        """Convert raw CoinGlass funding readings into `funding_extreme`
        Events for any reading beyond `EXTREME_FUNDING_THRESHOLD`.

        Expected raw shape (roughly):
            {"symbol": "BTC", "exchangeName": "Binance",
             "rate": 0.012, "openInterest": 1234567.0}
        """
        events: list[Event] = []
        now = datetime.now(timezone.utc)
        for item in raw_items:
            symbol = item.get("symbol")
            rate = item.get("rate")
            if symbol is None or rate is None:
                continue
            if abs(rate) <= self.EXTREME_FUNDING_THRESHOLD:
                continue

            direction = 1 if rate < 0 else -1
            # Rationale for the direction flip: extremely positive funding
            # means longs are crowded/paying shorts -- historically a
            # contrarian bearish signal (over-leveraged longs get squeezed
            # out); extremely negative funding means shorts are crowded --
            # a contrarian bullish signal. This mirrors how funding_extreme
            # is documented as a leading/contrarian indicator elsewhere in
            # CLAUDE.md's signal weight rationale (AD-05).
            confidence = min(1.0, abs(rate) / (self.EXTREME_FUNDING_THRESHOLD * 4))

            events.append(
                SignalFactory.funding_extreme(
                    asset=symbol,
                    asset_class="crypto",
                    direction=direction,
                    confidence=confidence,
                    timestamp=now,
                    source="coinglass",
                    funding_rate=rate,
                    exchange=item.get("exchangeName"),
                    open_interest=item.get("openInterest"),
                )
            )
        return events

    async def poll_once(self) -> list[Event]:
        raw_items = await self._fetch_raw()
        return self._raw_to_events(raw_items)


# ---------------------------------------------------------------------------
# PolygonOptionsPoller (FR-02)
# ---------------------------------------------------------------------------


class PolygonOptionsPoller(FeedPoller):
    """Polls Polygon's options endpoint, converts unusual options activity
    into `Event`s via `SignalFactory.options_flow(...)`.

    Polygon rate limits: Polygon's free/Starter tiers are throttled to 5
    API calls/minute per their published pricing page; a 60-second poll
    interval keeps a single poller comfortably within even the free tier,
    while still being frequent enough to be useful for intraday options
    flow. Paid "Options Starter"/"Options Developer" plans raise this
    ceiling substantially -- callers on a paid plan can safely shorten
    `interval_seconds`, but the default here targets the lowest common
    denominator per NFR-05's "never guess an interval" guidance.
    """

    source_name = "polygon_options"

    API_URL = "https://api.polygon.io/v3/snapshot/options"

    #: Minimum total premium (USD) for a trade to be treated as "unusual"
    #: options flow worth emitting as an Event. Documented, not derived from
    #: a live percentile (same rationale as CoinGlassFundingPoller's fixed
    #: threshold) -- $100k is a common informal "sweep" threshold used by
    #: retail options-flow tools as a floor for "worth paying attention to".
    MIN_PREMIUM_USD = 100_000.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("POLYGON_API_KEY")
        self._session = session

    async def _fetch_raw(self) -> list[dict]:
        """Fetch raw options-flow dicts from Polygon. Real network call --
        tests should monkeypatch/subclass this method.
        """
        import aiohttp

        params = {"apiKey": self.api_key}
        session = self._session
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession()
        try:
            async with session.get(self.API_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("results", []) if isinstance(data, dict) else data
        finally:
            if owns_session:
                await session.close()

    def _raw_to_events(self, raw_items: list[dict]) -> list[Event]:
        """Convert raw Polygon options-flow items into `options_flow` Events
        for any trade at/above `MIN_PREMIUM_USD`.

        Expected raw shape (roughly):
            {"underlying": "NVDA", "contract_type": "call", "strike": 150.0,
             "expiry": "2026-08-21", "premium": 250000.0, "side": "buy"}
        """
        events: list[Event] = []
        now = datetime.now(timezone.utc)
        for item in raw_items:
            underlying = item.get("underlying")
            premium = item.get("premium")
            if underlying is None or premium is None:
                continue
            if premium < self.MIN_PREMIUM_USD:
                continue

            contract_type = item.get("contract_type", "call")
            side = item.get("side", "buy")
            # call+buy or put+sell => bullish; put+buy or call+sell => bearish.
            bullish = (contract_type == "call") == (side == "buy")
            direction = 1 if bullish else -1
            confidence = min(1.0, premium / (self.MIN_PREMIUM_USD * 10))

            events.append(
                SignalFactory.options_flow(
                    asset=underlying,
                    asset_class="equity",
                    direction=direction,
                    confidence=confidence,
                    timestamp=now,
                    source="polygon",
                    contract_type=contract_type,
                    strike=item.get("strike"),
                    expiry=item.get("expiry"),
                    premium=premium,
                    side=side,
                )
            )
        return events

    async def poll_once(self) -> list[Event]:
        raw_items = await self._fetch_raw()
        return self._raw_to_events(raw_items)


# ---------------------------------------------------------------------------
# AlertRouter (FR-13, FR-20)
# ---------------------------------------------------------------------------

#: Severities that route to push-style channels (Slack/Telegram/Discord/
#: email/SMS) per FR-13/FR-20. All severities still broadcast over WebSocket
#: (api_service.py's job, Step 7) -- that gating does not apply here.
_ROUTABLE_SEVERITIES = frozenset({"high", "critical"})

#: A handler is `(name, callable, severity_gate)`. `severity_gate` is a
#: predicate over the alert payload's severity string -- kept per-handler
#: (not a single module-level constant) so Phase 2 channels (Discord email,
#: and ESPECIALLY Twilio SMS, which costs money per message) can each define
#: their own gate without touching this class's dispatch loop. Twilio's gate
#: should stay at least as strict as `_ROUTABLE_SEVERITIES` -- never widen it
#: to fire on every alert (explicit CLAUDE.md instruction).
AlertHandler = Callable[[dict], Awaitable[None]]


class AlertRouter:
    """Subscribes to the `alerts` channel; dispatches high/critical severity
    alerts to registered handlers (Slack/Telegram now; Discord/Twilio/email
    in Phase 2, FR-13/FR-20).

    Structured as a list of `(name, handler, severity_gate)` registrations
    rather than two hardcoded method calls so adding a Phase 2 channel is
    "register another handler", not "restructure this class". Each
    registration's `severity_gate` is independent -- e.g. Twilio SMS in
    Phase 2 could use an even stricter gate (`{"critical"}` only) than
    Slack/Telegram's `{"high", "critical"}", but must never be looser.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self._handlers: list[tuple[str, AlertHandler, frozenset[str]]] = []

    def register_handler(
        self,
        name: str,
        handler: AlertHandler,
        severity_gate: frozenset[str] = _ROUTABLE_SEVERITIES,
    ) -> None:
        """Register a channel handler with its own severity gate.

        `handler` must be an async callable taking a single `alert_payload`
        dict argument (already bound to its webhook URL/token/chat_id via
        `functools.partial` or a closure by the caller -- this class does
        not know about env vars itself, see `slack_handler`/`telegram_handler`
        module-level functions below).
        """
        self._handlers.append((name, handler, frozenset(severity_gate)))

    def register_default_handlers(
        self,
        slack_webhook_url: Optional[str] = None,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
    ) -> None:
        """Convenience wiring for the Phase 1 pair (Slack + Telegram),
        reading URL/token/chat_id from env vars if not passed explicitly
        (NFR-05). No-ops for a channel whose credentials aren't configured
        (so a partial env-var setup doesn't crash the router, it just skips
        that channel).
        """
        slack_url = slack_webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
        if slack_url:
            self.register_handler(
                "slack",
                lambda payload: slack_handler(payload, slack_url),
            )

        tg_token = telegram_token or os.environ.get("TELEGRAM_TOKEN")
        tg_chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        if tg_token and tg_chat_id:
            self.register_handler(
                "telegram",
                lambda payload: telegram_handler(payload, tg_token, tg_chat_id),
            )

    async def dispatch(self, alert_payload: dict) -> None:
        """Route a single alert payload to every registered handler whose
        severity gate matches this payload's severity.

        Handler exceptions are logged and swallowed per-handler (mirrors
        `FeedPoller`'s "one dead consumer must not break the others"
        philosophy) -- a Slack outage must not prevent Telegram delivery.
        """
        severity = alert_payload.get("severity")
        for name, handler, severity_gate in self._handlers:
            if severity not in severity_gate:
                continue
            try:
                await handler(alert_payload)
            except Exception:
                logger.exception(
                    "AlertRouter handler %r raised while dispatching alert for %r",
                    name,
                    alert_payload.get("asset"),
                )

    async def run(self) -> None:
        """Consume `alerts` forever, dispatching each payload. Callers
        control lifetime via task cancellation.
        """
        async for alert_payload in self.event_bus.subscribe_alerts():
            await self.dispatch(alert_payload)


def _format_alert_text(alert_payload: dict) -> str:
    """Shared human-readable alert text for Slack/Telegram."""
    asset = alert_payload.get("asset", "?")
    score = alert_payload.get("confluence_score", 0.0)
    severity = alert_payload.get("severity", "unknown")
    signal_count = alert_payload.get("signal_count", 0)
    return (
        f"[{severity.upper()}] {asset}: confluence={score:.2f} "
        f"({signal_count} signals)"
    )


async def slack_handler(alert_payload: dict, webhook_url: str) -> None:
    """POST a Slack-formatted message to `webhook_url` (Slack Incoming
    Webhooks need no auth token, per NFR-05/FR-20). `webhook_url` is always
    passed explicitly (never read from env inside this function) so tests
    can inject a fake URL and production callers read `SLACK_WEBHOOK_URL`
    themselves (see `AlertRouter.register_default_handlers`).
    """
    import aiohttp

    body = {"text": _format_alert_text(alert_payload)}
    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json=body) as resp:
            resp.raise_for_status()


async def telegram_handler(alert_payload: dict, bot_token: str, chat_id: str) -> None:
    """POST to Telegram Bot API's `sendMessage`, using `bot_token`/`chat_id`
    passed explicitly (never read from env inside this function -- same
    rationale as `slack_handler`).
    """
    import aiohttp

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = {"chat_id": chat_id, "text": _format_alert_text(alert_payload)}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body) as resp:
            resp.raise_for_status()


__all__ = [
    "EventBus",
    "build_event_bus_from_env",
    "FeedPoller",
    "FinnhubNewsPoller",
    "CoinGlassFundingPoller",
    "PolygonOptionsPoller",
    "AlertRouter",
    "slack_handler",
    "telegram_handler",
    "EVENTS_CHANNEL",
    "ALERTS_CHANNEL",
    "EVENTS_BUFFER_KEY",
    "EVENTS_BUFFER_MAX_LEN",
    "ALERT_CACHE_TTL_SECONDS",
    "FEED_HEALTH_TTL_SECONDS",
    "FEED_HEALTH_KEY_PREFIX",
]
