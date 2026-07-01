"""tests/test_e2e_phase1.py — Step 11 of Trade-Dash's Phase 1 build.

Full-chain end-to-end test spanning every Phase 1 module named in the
`e2e-integration-tests` skill: article/signal ingest -> NLP dedup/
sentiment/ticker-extraction (or `SignalFactory` typed construction) ->
`CorrelationScorer` confluence -> Postgres persistence + Redis publish ->
`WS /ws/alerts` broadcast -> `AlertRouter` -> Slack/Telegram delivery.

Every external dependency is faked/mocked: `FakeEventRepository` stands in
for Postgres, `FakeRedis` (wrapped in a real `EventBus`) stands in for
Redis, and `aioresponses` mocks Finnhub/CoinGlass/Polygon HTTP responses
plus the Slack/Telegram webhook endpoints. No live credentials are
required for this suite to pass (the skill's explicit CI-gate
requirement) — real credentials are for manual/staging verification only
(see Step 10's local apt-Postgres/Redis spike for that separate,
non-committed verification pass).

One test per MMP acceptance criterion (CLAUDE.md §3):
  1. test_ingests_at_least_three_article_sources_and_two_signal_sources
  2. test_confluence_score_fires_for_btc_and_an_equity_within_60s
  3. test_websocket_panel_receives_alert_within_5s_of_score_computation
  4. test_slack_and_telegram_notified_within_10s_of_alert
  5. test_zero_data_loss_across_simulated_restart
  6. test_feed_health_reports_staleness_within_ttl

NOTE on criterion 1's wording ("ingests >=3 live article sources"): Phase
1 ships exactly one dedicated article poller (`FinnhubNewsPoller`), per
CLAUDE.md's own Phase 1 TODO list ("Wire FinnhubNewsPoller,
CoinGlassFundingPoller, PolygonOptionsPoller"). `UnifiedPipeline.
ingest_articles()` is source-agnostic (any `Article.source` string is
accepted and flows straight into `Event.source`), so this test
demonstrates the >=3-distinct-source requirement by combining the real
`FinnhubNewsPoller` with two directly-constructed `Article`s tagged
"reuters_rss"/"benzinga" (both named as future sources in CLAUDE.md's
Data Sources Mapped section) through the same ingest path, rather than
overclaiming three dedicated poller classes exist. A gap between the
acceptance-criteria wording and the built poller surface is a roadmap
matter (Phase 2's RSS pollers), not something this test should paper
over or silently reinterpret away.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from aioresponses import aioresponses
from fastapi.testclient import TestClient

from event_types import Article, Event
from unified_pipeline import SignalFactory, UnifiedPipeline
from event_bus import (
    AlertRouter,
    CoinGlassFundingPoller,
    EventBus,
    FinnhubNewsPoller,
    PolygonOptionsPoller,
    slack_handler,
    telegram_handler,
)
import api_service as api

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes — deliberately NOT imported from tests/test_api_service.py or
# tests/test_event_bus.py, matching this repo's established convention that
# each test module owns its own copy so no test module depends on another's
# internals (see those files' own docstrings for the same rationale).
# ---------------------------------------------------------------------------


class FakeEventRepository:
    """In-memory stand-in for `api_service.EventRepository`."""

    def __init__(self) -> None:
        self.inserted: list[Event] = []

    async def insert_event(self, event: Event) -> None:
        self.inserted.append(event)

    async def load_all_events(self) -> list[Event]:
        return list(self.inserted)


class _FakePubSub:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._channels: set[str] = set()
        self._queue: asyncio.Queue = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:
        self._channels.add(channel)
        self._redis._register_subscriber(channel, self._queue)

    async def unsubscribe(self, channel: str) -> None:
        self._channels.discard(channel)
        self._redis._unregister_subscriber(channel, self._queue)

    async def listen(self):
        while True:
            message = await self._queue.get()
            yield message


class FakeRedis:
    """In-memory fake implementing the `redis.asyncio.Redis` surface
    `EventBus` calls, including real TTL bookkeeping via `time.monotonic()`
    (mirrors tests/test_event_bus.py's canonical version) plus a
    `force_expire` test helper so feed-health staleness can be exercised
    deterministically instead of sleeping past the real 5-minute TTL.
    """

    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._lists: dict[str, list[str]] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def _is_expired(self, key: str) -> bool:
        expiry = self._expiry.get(key)
        return expiry is not None and time.monotonic() >= expiry

    def _purge_if_expired(self, key: str) -> None:
        if key in self._strings and self._is_expired(key):
            del self._strings[key]
            self._expiry.pop(key, None)

    def _register_subscriber(self, channel: str, queue: asyncio.Queue) -> None:
        self._subscribers.setdefault(channel, []).append(queue)

    def _unregister_subscriber(self, channel: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(channel, [])
        if queue in subs:
            subs.remove(queue)

    async def publish(self, channel: str, data: str) -> int:
        subs = self._subscribers.get(channel, [])
        message = {"type": "message", "channel": channel, "data": data}
        for queue in subs:
            queue.put_nowait(message)
        return len(subs)

    def pubsub(self, **kwargs: Any) -> _FakePubSub:
        return _FakePubSub(self)

    async def setex(self, name: str, time_seconds: int, value: str) -> bool:
        self._strings[name] = value
        self._expiry[name] = time.monotonic() + time_seconds
        return True

    async def get(self, name: str) -> Optional[str]:
        self._purge_if_expired(name)
        return self._strings.get(name)

    async def exists(self, name: str) -> int:
        self._purge_if_expired(name)
        return 1 if name in self._strings else 0

    async def keys(self, pattern: str) -> list[str]:
        assert pattern.endswith("*")
        prefix = pattern[:-1]
        return [
            k for k in list(self._strings.keys())
            if k.startswith(prefix) and not self._is_expired(k)
        ]

    async def lpush(self, name: str, *values: str) -> int:
        lst = self._lists.setdefault(name, [])
        for v in values:
            lst.insert(0, v)
        return len(lst)

    async def ltrim(self, name: str, start: int, end: int) -> bool:
        lst = self._lists.get(name, [])
        if end == -1:
            self._lists[name] = lst[start:]
        else:
            self._lists[name] = lst[start : end + 1]
        return True

    async def lrange(self, name: str, start: int, end: int) -> list[str]:
        lst = self._lists.get(name, [])
        if end == -1:
            return lst[start:]
        return lst[start : end + 1]

    def force_expire(self, name: str) -> None:
        """Test helper: simulate TTL expiry immediately, without waiting
        out the real 300s NFR-06 TTL."""
        self._expiry[name] = time.monotonic() - 1


def _url_pattern(base_url: str) -> re.Pattern:
    """Match `base_url` with any (or no) query string, mirroring
    tests/test_event_bus.py's helper of the same name."""
    return re.compile(rf"^{re.escape(base_url)}(\?.*)?$")


def make_app(
    *,
    pipeline: Optional[UnifiedPipeline] = None,
    repository: Optional[FakeEventRepository] = None,
    event_bus: Optional[EventBus] = None,
    start_background_tasks: bool = True,
):
    repository = repository if repository is not None else FakeEventRepository()
    event_bus = event_bus if event_bus is not None else EventBus(FakeRedis())
    pipeline = pipeline if pipeline is not None else UnifiedPipeline()
    app = api.build_app(
        pipeline=pipeline,
        repository=repository,
        event_bus=event_bus,
        start_background_tasks=start_background_tasks,
    )
    return app, repository, event_bus, pipeline


# ---------------------------------------------------------------------------
# Criterion 1: pipeline ingests >=3 article sources and >=2 signal sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingests_at_least_three_article_sources_and_two_signal_sources():
    pipeline = UnifiedPipeline()

    # -- Article source #1: the real FinnhubNewsPoller, HTTP mocked -------
    finnhub_poller = FinnhubNewsPoller(api_key="fake-key", pipeline=pipeline)
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern(finnhub_poller.API_URL),
            status=200,
            payload=[
                {
                    "headline": "NVDA soars after blowout earnings beat",
                    "summary": "Nvidia posted record datacenter revenue.",
                    "url": "https://example.com/e2e/nvda-earnings",
                    "datetime": int(NOW.timestamp()),
                }
            ],
        )
        finnhub_events = await finnhub_poller.poll_once()

    # -- Article sources #2/#3: directly-constructed Articles tagged with
    # future-Phase-2 RSS source names, run through the same source-agnostic
    # ingest_articles() path (see module docstring's note on criterion 1).
    extra_articles = [
        Article(
            headline="SPY rallies on bullish macro data",
            body="Broad-based bullish sentiment lifted the S&P 500 ETF.",
            url="https://example.com/e2e/spy-rally",
            published_at=NOW,
            source="reuters_rss",
        ),
        Article(
            headline="SPY extends gains as bulls take control",
            body="Momentum bullish traders pushed SPY to new highs.",
            url="https://example.com/e2e/spy-momentum",
            published_at=NOW,
            source="benzinga",
        ),
    ]
    extra_events = pipeline.ingest_articles(extra_articles)

    article_events = finnhub_events + extra_events
    article_sources = {e.source for e in article_events}
    assert len(article_sources) >= 3, article_sources
    assert {"finnhub", "reuters_rss", "benzinga"}.issubset(article_sources)

    # -- Signal source #1: CoinGlassFundingPoller, HTTP mocked ------------
    coinglass_poller = CoinGlassFundingPoller(api_key="fake-key")
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern(coinglass_poller.API_URL),
            status=200,
            payload={"data": [{"symbol": "BTC", "exchangeName": "binance", "rate": -0.012, "openInterest": 1.5e9}]},
        )
        coinglass_events = await coinglass_poller.poll_once()
    assert len(coinglass_events) >= 1
    assert coinglass_events[0].source == "coinglass"

    # -- Signal source #2: PolygonOptionsPoller, HTTP mocked --------------
    polygon_poller = PolygonOptionsPoller(api_key="fake-key")
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern(polygon_poller.API_URL),
            status=200,
            payload={"results": [{"underlying": "NVDA", "contract_type": "call", "side": "buy", "premium": 2_000_000.0, "strike": 560.0, "expiry": "2026-08-21"}]},
        )
        polygon_events = await polygon_poller.poll_once()
    assert len(polygon_events) >= 1
    assert polygon_events[0].source == "polygon"

    signal_sources = {e.source for e in (coinglass_events + polygon_events)}
    assert len(signal_sources) >= 2, signal_sources

    pipeline.ingest_signal_events(coinglass_events + polygon_events)


# ---------------------------------------------------------------------------
# Criterion 2: confluence score fires for BTC and >=1 equity within 60s
# ---------------------------------------------------------------------------


def test_confluence_score_fires_for_btc_and_an_equity_within_60s():
    started = time.monotonic()

    pipeline = UnifiedPipeline()

    # BTC: 3 agreeing bullish signals from 3 distinct sources/types.
    pipeline.ingest_signal_events(
        [
            SignalFactory.whale_transfer(
                asset="BTC", asset_class="crypto", direction=1, confidence=0.9,
                timestamp=NOW, source="arkham", amount_usd=50_000_000.0,
                from_label="unknown_wallet", to_label="coinbase_prime",
            ),
            SignalFactory.funding_extreme(
                asset="BTC", asset_class="crypto", direction=1, confidence=0.85,
                timestamp=NOW, source="coinglass", funding_rate=-0.012,
                exchange="binance", open_interest=1.5e9,
            ),
            SignalFactory.on_chain_move(
                asset="BTC", asset_class="crypto", direction=1, confidence=0.8,
                timestamp=NOW, source="glassnode", metric="exchange_netflow",
                value=-12_000.0, z_score=3.2,
            ),
        ]
    )

    # NVDA (equity): 3 agreeing bullish signals.
    pipeline.ingest_signal_events(
        [
            SignalFactory.options_flow(
                asset="NVDA", asset_class="equity", direction=1, confidence=0.9,
                timestamp=NOW, source="polygon", contract_type="call",
                strike=560.0, expiry="2026-08-21", premium=2_000_000.0,
            ),
            SignalFactory.greek_anomaly(
                asset="NVDA", asset_class="equity", direction=1, confidence=0.85,
                timestamp=NOW, source="tradier", greek="gamma", z_score=4.1,
                option_symbol="NVDA260821C00600000",
            ),
            SignalFactory.insider_trade(
                asset="NVDA", asset_class="equity", direction=1, confidence=0.75,
                timestamp=NOW, source="sec_edgar", insider_name="Jane Doe",
                title="CFO", shares=10_000, transaction_type="buy",
            ),
        ]
    )

    payloads = pipeline.alert_payloads(threshold=0.65, min_signals=3)
    elapsed = time.monotonic() - started

    assert elapsed < 60.0, f"confluence computation took {elapsed}s, expected <60s"

    fired_assets = {p["asset"] for p in payloads}
    assert "BTC" in fired_assets, payloads
    equities_fired = fired_assets & {"NVDA", "SPY", "AAPL", "MSFT", "TSLA"}
    assert len(equities_fired) >= 1, payloads

    btc_result = next(p for p in payloads if p["asset"] == "BTC")
    assert abs(btc_result["confluence_score"]) >= 0.65
    assert btc_result["signal_count"] >= 3


# ---------------------------------------------------------------------------
# Criterion 3: WebSocket panel receives an alert within 5s of computation
# ---------------------------------------------------------------------------


def test_websocket_panel_receives_alert_within_5s_of_score_computation():
    pipeline = UnifiedPipeline()
    pipeline.ingest_signal_events(
        [
            SignalFactory.whale_transfer(
                asset="ETH", asset_class="crypto", direction=1, confidence=0.9,
                timestamp=NOW, source="arkham", amount_usd=30_000_000.0,
                from_label="unknown_wallet", to_label="binance",
            ),
            SignalFactory.funding_extreme(
                asset="ETH", asset_class="crypto", direction=1, confidence=0.85,
                timestamp=NOW, source="coinglass", funding_rate=-0.011,
                exchange="bybit", open_interest=8e8,
            ),
            SignalFactory.on_chain_move(
                asset="ETH", asset_class="crypto", direction=1, confidence=0.8,
                timestamp=NOW, source="glassnode", metric="exchange_netflow",
                value=-9_000.0, z_score=3.0,
            ),
        ]
    )
    payloads = pipeline.alert_payloads()
    assert len(payloads) >= 1

    app, repository, event_bus, _ = make_app(pipeline=pipeline)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/alerts") as ws:
            started = time.monotonic()
            asyncio.run(event_bus.publish_alert(payloads[0]))
            received = ws.receive_json()
            elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"WS delivery took {elapsed}s, expected <5s (NFR-01/criterion 3)"
    assert received["asset"] == "ETH"
    assert received["signal_count"] == payloads[0]["signal_count"]


# ---------------------------------------------------------------------------
# Criterion 4: Slack or Telegram notification delivered within 10s of alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_and_telegram_notified_within_10s_of_alert():
    event_bus = EventBus(FakeRedis())
    router = AlertRouter(event_bus)

    webhook_url = "https://hooks.slack.com/services/T000/B000/fake"
    bot_token = "fake-bot-token"
    chat_id = "fake-chat-id"
    router.register_handler("slack", lambda payload: slack_handler(payload, webhook_url))
    router.register_handler(
        "telegram", lambda payload: telegram_handler(payload, bot_token, chat_id)
    )

    alert_payload = {
        "asset": "BTC",
        "confluence_score": 0.88,
        "signal_count": 3,
        "severity": "critical",
        "agreement_ratio": 1.0,
        "conflict_ratio": 0.0,
        "top_contributors": [],
    }

    telegram_url_pattern = re.compile(
        rf"^https://api\.telegram\.org/bot{re.escape(bot_token)}/sendMessage$"
    )

    with aioresponses() as mocked:
        mocked.post(webhook_url, status=200, payload={"ok": True})
        mocked.post(telegram_url_pattern, status=200, payload={"ok": True})

        started = time.monotonic()
        await router.dispatch(alert_payload)
        elapsed = time.monotonic() - started

        # AlertRouter.dispatch() catches and logs (not raises) any
        # individual handler's exception, so a wrong URL/silently-swallowed
        # failure would NOT surface as a test failure via elapsed time or a
        # raised exception alone -- assert the mocked endpoints were
        # actually hit, so a broken handler can't pass this test by doing
        # nothing.
        called_urls = [str(url) for _, url in mocked.requests.keys()]
        assert any(webhook_url == u for u in called_urls), called_urls
        assert any(telegram_url_pattern.match(u) for u in called_urls), called_urls

    assert elapsed < 10.0, f"Slack/Telegram dispatch took {elapsed}s, expected <10s (criterion 4)"


# ---------------------------------------------------------------------------
# Criterion 5: zero data loss on restart (MMP acceptance criterion 5)
# ---------------------------------------------------------------------------


def test_zero_data_loss_across_simulated_restart():
    shared_repository = FakeEventRepository()

    app1, repository, event_bus, pipeline1 = make_app(repository=shared_repository)
    with TestClient(app1) as client:
        resp = client.post(
            "/ingest/signals",
            json={
                "signals": [
                    {
                        "event_type": "whale_transfer",
                        "asset": "BTC",
                        "asset_class": "crypto",
                        "direction": 1,
                        "confidence": 0.9,
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "arkham",
                        "meta_fields": {"amount_usd": 40_000_000.0},
                    }
                ]
            },
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    assert len(shared_repository.inserted) == 1

    # Simulate a full process restart: brand-new pipeline/app, SAME
    # underlying repository (the durable store) -- only lifespan's
    # rehydrate_from_repository() call should repopulate the pipeline.
    app2, repository2, event_bus2, pipeline2 = make_app(repository=shared_repository)
    with TestClient(app2) as client2:
        result = client2.get("/correlate/BTC")
        assert result.status_code == 200
        body = result.json()

    assert body["signal_count"] == 1, "restart-durability FAILED: event lost after restart"
    assert pipeline2 is not pipeline1  # genuinely a fresh in-memory pipeline


# ---------------------------------------------------------------------------
# Criterion 6: feed health monitor accurately reports source staleness
# ---------------------------------------------------------------------------


def test_feed_health_reports_staleness_within_ttl():
    from event_bus import _feed_health_key
    from freshness_guard import check_feed_freshness

    fake_redis = FakeRedis()
    event_bus = EventBus(fake_redis)
    app, repository, _, _ = make_app(event_bus=event_bus)

    async def _heartbeat() -> None:
        await event_bus.heartbeat("finnhub_news")

    asyncio.run(_heartbeat())

    with TestClient(app) as client:
        resp = client.get("/feed-health")
        assert resp.status_code == 200
        sources = resp.json()["sources"]
        assert "finnhub_news" in sources
        assert sources["finnhub_news"]["is_stale"] is False

    # Simulate the poller having stopped: force the heartbeat key to have
    # already expired, without waiting out the real 300s NFR-06 TTL --
    # `get_feed_health()`'s underlying `keys()` call already filters
    # expired keys out (see EventBus.get_feed_health's docstring: a caller
    # diffs the returned sources against its own expected registry, same
    # as freshness_guard.py does below), so the source's absence from
    # `sources` IS the staleness signal here, not a `is_stale: True` entry.
    fake_redis.force_expire(_feed_health_key("finnhub_news"))

    with TestClient(app) as client:
        resp_after = client.get("/feed-health")
        assert resp_after.status_code == 200
        sources_after = resp_after.json()["sources"]
        assert "finnhub_news" not in sources_after, (
            "GET /feed-health still reports finnhub_news as present after "
            "its heartbeat key expired -- staleness not reflected"
        )

    # Cross-check with freshness_guard.py's explicit True/False signal
    # (the same mechanism a future scoring-job gate would use).
    async def _check() -> dict:
        return await check_feed_freshness(event_bus, ["finnhub_news"])

    result = asyncio.run(_check())
    assert result == {"finnhub_news": False}
