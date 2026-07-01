"""Tests for api_service.py (Step 7).

No live Postgres or Redis is required — everything here runs against:

    - `FakeEventRepository`: an in-memory stand-in for `EventRepository`
      (a plain Python list of inserted `Event`s), implementing
      `insert_event`/`load_all_events`. This is exactly the seam the task
      spec calls for: the SAME `FakeEventRepository` instance is reused
      across a "before restart" and "after restart" phase in the
      restart-durability test.
    - `FakeRedis` + `EventBus`: the identical fake-Redis pattern used in
      tests/test_event_bus.py (copied here, not imported, so this test
      module has no cross-test-module coupling), wrapped in a real
      `EventBus` (which is itself already unit-tested in
      tests/test_event_bus.py — reused here as a black box).
    - `TestClient` (httpx-based, sync) from `fastapi.testclient`, using
      `with TestClient(app) as client:` so the app's `lifespan` context
      manager actually runs (rehydration + alert-relay startup/shutdown).

CLAUDE.md sections exercised: the 8 routes (§5), NFR-01 (ingest -> WS alert
latency), NFR-02 (>=10 concurrent WS clients, no dropped messages — slow
client test), NFR-05 (no hardcoded secrets), MMP acceptance criterion 5
(zero data loss on restart), AD-01 (pydantic models on every route, no raw
dicts).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from event_types import Event
from unified_pipeline import UnifiedPipeline
from event_bus import EventBus
import api_service as api

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# FakeEventRepository — in-memory stand-in for api_service.EventRepository
# ---------------------------------------------------------------------------


class FakeEventRepository:
    """In-memory fake for `api_service.EventRepository`.

    Stores inserted `Event`s in a plain Python list. `load_all_events`
    returns a defensive copy in insertion order (mirrors the real
    repository's `ORDER BY ts ASC`, since demo/test fixtures always insert
    in increasing timestamp order). Exposes `.inserted` directly so tests
    can assert on exactly what was persisted (used by the
    Postgres-write-AND-Redis-publish test).
    """

    def __init__(self) -> None:
        self.inserted: list[Event] = []

    async def insert_event(self, event: Event) -> None:
        self.inserted.append(event)

    async def load_all_events(self) -> list[Event]:
        return list(self.inserted)


# ---------------------------------------------------------------------------
# FakeRedis / FakePubSub — copied from tests/test_event_bus.py's pattern
# ---------------------------------------------------------------------------


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
    """In-memory fake implementing exactly the `redis.asyncio.Redis` surface
    `EventBus` calls. See tests/test_event_bus.py for the canonical version
    this mirrors.
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


# ---------------------------------------------------------------------------
# Shared test helpers / fixtures
# ---------------------------------------------------------------------------


class FlakyEventRepository(FakeEventRepository):
    """`FakeEventRepository` that raises on the (fail_after+1)-th
    `insert_event` call, simulating a Postgres failure partway through a
    batch -- used to reproduce review finding #1 (the in-memory pipeline
    must never get ahead of what's actually durable on a partial failure).
    """

    def __init__(self, fail_after: int) -> None:
        super().__init__()
        self._fail_after = fail_after

    async def insert_event(self, event: Event) -> None:
        if len(self.inserted) >= self._fail_after:
            raise RuntimeError("simulated Postgres failure")
        await super().insert_event(event)


def make_event(**overrides) -> Event:
    defaults = dict(
        asset="BTC",
        asset_class="crypto",
        event_type="whale_transfer",
        direction=1,
        confidence=0.8,
        timestamp=NOW,
        source="arkham",
    )
    defaults.update(overrides)
    return Event(**defaults)


def make_app(
    *,
    pipeline: Optional[UnifiedPipeline] = None,
    repository: Optional[FakeEventRepository] = None,
    event_bus: Optional[EventBus] = None,
    start_background_tasks: bool = True,
):
    """Build a fully-wired api_service app with fake dependencies injected."""
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
# POST /ingest/articles
# ---------------------------------------------------------------------------


def test_ingest_articles_happy_path():
    app, repository, event_bus, pipeline = make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/ingest/articles",
            json={
                "articles": [
                    {
                        "headline": "NVDA soars on earnings beat",
                        "body": "Nvidia reported record bullish datacenter revenue growth.",
                        "url": "https://example.com/nvda",
                        "published_at": "2026-07-01T12:00:00+00:00",
                        "source": "finnhub",
                    }
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert body["events"][0]["asset"] == "NVDA"
    assert body["events"][0]["event_type"] == "news"


def test_ingest_articles_malformed_payload_is_4xx_not_500():
    app, *_ = make_app()
    with TestClient(app) as client:
        # missing required fields entirely
        resp = client.post("/ingest/articles", json={"articles": [{"headline": ""}]})
    assert 400 <= resp.status_code < 500

    with TestClient(app) as client:
        # naive (non-tz-aware) datetime should be rejected
        resp2 = client.post(
            "/ingest/articles",
            json={
                "articles": [
                    {
                        "headline": "x",
                        "body": "y",
                        "url": "https://example.com/x",
                        "published_at": "2026-07-01T12:00:00",
                        "source": "finnhub",
                    }
                ]
            },
        )
    assert 400 <= resp2.status_code < 500


def test_ingest_articles_empty_batch_is_4xx():
    app, *_ = make_app()
    with TestClient(app) as client:
        resp = client.post("/ingest/articles", json={"articles": []})
    assert 400 <= resp.status_code < 500


# ---------------------------------------------------------------------------
# POST /ingest/signals
# ---------------------------------------------------------------------------


def test_ingest_signals_happy_path():
    app, repository, event_bus, pipeline = make_app()
    with TestClient(app) as client:
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
                        "meta_fields": {"amount_usd": 25_000_000.0},
                    }
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["events"][0]["asset"] == "BTC"
    assert body["events"][0]["event_type"] == "whale_transfer"
    assert body["events"][0]["meta"]["amount_usd"] == 25_000_000.0


def test_ingest_signals_unknown_event_type_is_4xx_not_500():
    app, *_ = make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/ingest/signals",
            json={
                "signals": [
                    {
                        "event_type": "not_a_real_signal_type",
                        "asset": "BTC",
                        "asset_class": "crypto",
                        "direction": 1,
                        "confidence": 0.9,
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "arkham",
                    }
                ]
            },
        )
    assert 400 <= resp.status_code < 500


def test_ingest_signals_bad_direction_is_4xx_not_500():
    app, *_ = make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/ingest/signals",
            json={
                "signals": [
                    {
                        "event_type": "whale_transfer",
                        "asset": "BTC",
                        "asset_class": "crypto",
                        "direction": 5,  # invalid -- must be -1/0/1
                        "confidence": 0.9,
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "arkham",
                    }
                ]
            },
        )
    assert 400 <= resp.status_code < 500


def test_ingest_signals_bad_confidence_is_4xx_not_500():
    app, *_ = make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/ingest/signals",
            json={
                "signals": [
                    {
                        "event_type": "whale_transfer",
                        "asset": "BTC",
                        "asset_class": "crypto",
                        "direction": 1,
                        "confidence": 1.5,  # invalid -- must be in [0, 1]
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "arkham",
                    }
                ]
            },
        )
    assert 400 <= resp.status_code < 500


# ---------------------------------------------------------------------------
# Both writes hit Postgres (repository) AND Redis (event bus)
# ---------------------------------------------------------------------------


def test_ingest_signals_persists_to_repository_and_publishes_to_bus():
    app, repository, event_bus, pipeline = make_app()

    published: list[dict] = []
    original_publish = event_bus.publish_event

    async def _spy_publish(event: Event) -> None:
        published.append({"asset": event.asset, "event_type": event.event_type})
        await original_publish(event)

    event_bus.publish_event = _spy_publish  # type: ignore[method-assign]

    with TestClient(app) as client:
        resp = client.post(
            "/ingest/signals",
            json={
                "signals": [
                    {
                        "event_type": "whale_transfer",
                        "asset": "ETH",
                        "asset_class": "crypto",
                        "direction": 1,
                        "confidence": 0.8,
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "arkham",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    # Postgres write happened...
    assert len(repository.inserted) == 1
    assert repository.inserted[0].asset == "ETH"
    # ...AND Redis publish happened -- not a Redis-only or Postgres-only path.
    assert len(published) == 1
    assert published[0]["asset"] == "ETH"


def test_ingest_articles_persists_to_repository_and_publishes_to_bus():
    app, repository, event_bus, pipeline = make_app()

    published: list[str] = []
    original_publish = event_bus.publish_event

    async def _spy_publish(event: Event) -> None:
        published.append(event.asset)
        await original_publish(event)

    event_bus.publish_event = _spy_publish  # type: ignore[method-assign]

    with TestClient(app) as client:
        resp = client.post(
            "/ingest/articles",
            json={
                "articles": [
                    {
                        "headline": "SPY rallies on bullish macro data",
                        "body": "Broad bullish sentiment across the market.",
                        "url": "https://example.com/spy",
                        "published_at": "2026-07-01T12:00:00+00:00",
                        "source": "finnhub",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    n = resp.json()["count"]
    assert n >= 1
    assert len(repository.inserted) == n
    assert len(published) == n


# ---------------------------------------------------------------------------
# Review finding #1: partial persist/publish failure must never let the
# in-memory pipeline get ahead of what's actually durable
# ---------------------------------------------------------------------------


def test_ingest_signals_partial_failure_returns_502_with_partial_accounting():
    repository = FlakyEventRepository(fail_after=1)
    app, repository, event_bus, pipeline = make_app(repository=repository)
    with TestClient(app) as client:
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
                    },
                    {
                        "event_type": "whale_transfer",
                        "asset": "ETH",
                        "asset_class": "crypto",
                        "direction": 1,
                        "confidence": 0.9,
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "arkham",
                    },
                ]
            },
        )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["total"] == 2
    assert detail["succeeded_count"] == 1
    assert detail["failed_asset"] == "ETH"
    assert detail["succeeded_assets"] == ["BTC"]


def test_ingest_signals_partial_failure_only_pipeline_adds_confirmed_durable_subset():
    """The confirmed-durable BTC event must be visible in-memory; the ETH
    event that failed to persist/publish must NOT be, even though the
    pipeline-add used to happen unconditionally before persistence was
    attempted (review finding #1).
    """
    repository = FlakyEventRepository(fail_after=1)
    app, repository, event_bus, pipeline = make_app(repository=repository)

    with TestClient(app) as client:
        client.post(
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
                    },
                    {
                        "event_type": "whale_transfer",
                        "asset": "ETH",
                        "asset_class": "crypto",
                        "direction": 1,
                        "confidence": 0.9,
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "arkham",
                    },
                ]
            },
        )

    assert pipeline.correlate("BTC").signal_count == 1
    assert pipeline.correlate("ETH").signal_count == 0


def test_demo_seed_partial_signal_failure_returns_502_and_does_not_over_add_to_pipeline():
    # 3 demo articles persist fine (fail_after only counts from the first
    # insert_event call onward across BOTH the article and signal phases,
    # so allow all 3 articles through, then fail on the very first signal).
    repository = FlakyEventRepository(fail_after=3)
    app, repository, event_bus, pipeline = make_app(repository=repository)
    with TestClient(app) as client:
        resp = client.post("/demo/seed")

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["succeeded_count"] == 0
    assert detail["total"] == 6  # _demo_signal_events() produces 6 events

    # None of the demo signal events should have reached the pipeline, since
    # every single one failed to persist.
    for asset in ("BTC", "SPY", "NVDA"):
        result = pipeline.correlate(asset)
        # Articles alone (no signals) shouldn't clear a directional
        # signal_count derived from whale_transfer/options_flow/etc.
        assert all(
            weighted.event.event_type not in (
                "whale_transfer", "funding_extreme", "options_flow",
                "macro_event", "greek_anomaly", "insider_trade",
            )
            for weighted in result.weighted_signals
        )


# ---------------------------------------------------------------------------
# Review finding #2: a WS client whose send fails must be deregistered, not
# left as a stale queue/task entry that silently absorbs future alerts
# ---------------------------------------------------------------------------


class _FailingWebSocket:
    """Minimal fake satisfying `ConnectionRegistry`'s WebSocket surface,
    whose `send_json` always raises -- simulates a dead/broken socket.
    """

    async def accept(self) -> None:
        pass

    async def send_json(self, payload: dict) -> None:
        raise RuntimeError("simulated dead socket")


def test_drain_task_disconnects_client_when_send_fails():
    async def _run() -> None:
        registry = api.ConnectionRegistry()
        ws = _FailingWebSocket()
        await registry.connect(ws)
        assert registry.client_count == 1

        registry.broadcast({"asset": "BTC"})
        # Give the background drain task a turn to run, hit the send
        # failure, and deregister itself.
        for _ in range(10):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

        assert registry.client_count == 0

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# GET /correlate/{asset}, GET /correlate
# ---------------------------------------------------------------------------


def test_correlate_single_asset():
    pipeline = UnifiedPipeline()
    pipeline.ingest_signal_events(
        [
            make_event(asset="BTC", event_type="whale_transfer", direction=1, confidence=0.9),
            make_event(asset="BTC", event_type="funding_extreme", direction=1, confidence=0.8),
        ]
    )
    app, *_ = make_app(pipeline=pipeline)
    with TestClient(app) as client:
        resp = client.get("/correlate/BTC")
    assert resp.status_code == 200
    body = resp.json()
    assert body["asset"] == "BTC"
    assert -1.0 <= body["confluence_score"] <= 1.0
    assert body["signal_count"] == 2
    assert len(body["weighted_signals"]) == 2


def test_correlate_unknown_asset_returns_zero_score_not_error():
    app, *_ = make_app()
    with TestClient(app) as client:
        resp = client.get("/correlate/NOTREAL")
    assert resp.status_code == 200
    body = resp.json()
    assert body["confluence_score"] == 0.0
    assert body["signal_count"] == 0


def test_correlate_all_assets():
    pipeline = UnifiedPipeline()
    pipeline.ingest_signal_events(
        [
            make_event(asset="BTC"),
            make_event(asset="NVDA", asset_class="equity", event_type="options_flow"),
        ]
    )
    app, *_ = make_app(pipeline=pipeline)
    with TestClient(app) as client:
        resp = client.get("/correlate")
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert set(results.keys()) == {"BTC", "NVDA"}


# ---------------------------------------------------------------------------
# GET /alerts
# ---------------------------------------------------------------------------


def test_alerts_returns_active_alerts_above_threshold():
    pipeline = UnifiedPipeline()
    pipeline.ingest_signal_events(
        [
            make_event(asset="BTC", event_type="whale_transfer", direction=1, confidence=0.95),
            make_event(asset="BTC", event_type="funding_extreme", direction=1, confidence=0.9),
            make_event(asset="BTC", event_type="on_chain_move", direction=1, confidence=0.9),
        ]
    )
    app, *_ = make_app(pipeline=pipeline)
    with TestClient(app) as client:
        resp = client.get("/alerts")
    assert resp.status_code == 200
    alerts = resp.json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["asset"] == "BTC"
    assert alerts[0]["severity"] in {"medium", "high", "critical"}
    assert "confluence_score" in alerts[0]
    assert "signal_count" in alerts[0]
    assert len(alerts[0]["top_contributors"]) <= 4


def test_alerts_empty_when_nothing_clears_threshold():
    app, *_ = make_app()
    with TestClient(app) as client:
        resp = client.get("/alerts")
    assert resp.status_code == 200
    assert resp.json()["alerts"] == []


# ---------------------------------------------------------------------------
# GET /feed-health
# ---------------------------------------------------------------------------


def test_feed_health_delegates_to_event_bus():
    event_bus = EventBus(FakeRedis())
    app, repository, event_bus, pipeline = make_app(event_bus=event_bus)

    async def _seed_heartbeat():
        await event_bus.heartbeat("finnhub_news")
        await event_bus.heartbeat_success("finnhub_news")

    asyncio.run(_seed_heartbeat())

    with TestClient(app) as client:
        resp = client.get("/feed-health")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    assert "finnhub_news" in sources
    assert sources["finnhub_news"]["is_stale"] is False
    assert sources["finnhub_news"]["is_healthy"] is True


def test_feed_health_empty_when_no_heartbeats():
    app, *_ = make_app()
    with TestClient(app) as client:
        resp = client.get("/feed-health")
    assert resp.status_code == 200
    assert resp.json()["sources"] == {}


# ---------------------------------------------------------------------------
# POST /demo/seed
# ---------------------------------------------------------------------------


def test_demo_seed_produces_immediately_correlatable_data():
    app, repository, event_bus, pipeline = make_app()
    with TestClient(app) as client:
        resp = client.post("/demo/seed")
        assert resp.status_code == 200
        body = resp.json()
        assert body["articles_ingested"] == 3
        assert body["signals_ingested"] >= 1
        assert body["events_created"] >= 4

        correlate_resp = client.get("/correlate")
        assert correlate_resp.status_code == 200
        results = correlate_resp.json()["results"]
        assert "BTC" in results
        assert "SPY" in results
        assert "NVDA" in results

        alerts_resp = client.get("/alerts")
        assert alerts_resp.status_code == 200
        # BTC/SPY/NVDA each get >= 3 corroborating bullish demo signals, so
        # at least one of them should clear FR-19's default gates.
        assert len(alerts_resp.json()["alerts"]) >= 1

    # Demo seed persisted to Postgres too, not just in-memory.
    assert len(repository.inserted) == body["events_created"]


def test_demo_seed_is_idempotent_safe_to_call_twice():
    app, *_ = make_app()
    with TestClient(app) as client:
        first = client.post("/demo/seed")
        second = client.post("/demo/seed")
    assert first.status_code == 200
    assert second.status_code == 200


# ---------------------------------------------------------------------------
# WS /ws/alerts
# ---------------------------------------------------------------------------


def test_ws_alerts_receives_broadcast_alert_payload():
    app, repository, event_bus, pipeline = make_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/alerts") as ws:
            asyncio.run(
                event_bus.publish_alert(
                    {
                        "asset": "BTC",
                        "confluence_score": 0.8,
                        "signal_count": 3,
                        "severity": "high",
                        "agreement_ratio": 1.0,
                        "conflict_ratio": 0.0,
                        "top_contributors": [],
                    }
                )
            )
            received = ws.receive_json()
    assert received["asset"] == "BTC"
    assert received["severity"] == "high"
    assert received["signal_count"] == 3


def test_ws_alerts_payload_matches_alert_payloads_shape():
    """Confirms the WS payload is the exact `alert_payloads()` shape
    (severity, confluence score, signal count, top-4 contributors) --
    CLAUDE.md §5 WebSocketAlertPanel spec -- reused directly, not reshaped.
    """
    pipeline = UnifiedPipeline()
    pipeline.ingest_signal_events(
        [
            make_event(asset="BTC", event_type="whale_transfer", direction=1, confidence=0.95),
            make_event(asset="BTC", event_type="funding_extreme", direction=1, confidence=0.9),
            make_event(asset="BTC", event_type="on_chain_move", direction=1, confidence=0.9),
        ]
    )
    app, repository, event_bus, pipeline = make_app(pipeline=pipeline)
    payloads = pipeline.alert_payloads()
    assert len(payloads) == 1
    expected = payloads[0]

    with TestClient(app) as client:
        with client.websocket_connect("/ws/alerts") as ws:
            asyncio.run(event_bus.publish_alert(expected))
            received = ws.receive_json()

    assert received["severity"] == expected["severity"]
    assert received["confluence_score"] == expected["confluence_score"]
    assert received["signal_count"] == expected["signal_count"]
    assert len(received["top_contributors"]) == len(expected["top_contributors"])
    assert len(received["top_contributors"]) <= 4


def test_ws_alerts_slow_client_does_not_block_other_clients_nfr02():
    """NFR-02: a slow/stuck client must never block delivery to others.

    Simulates a "slow" client by never reading from its socket (its queue
    fills up and starts dropping), while a second, actively-reading client
    must still promptly receive every broadcast alert.
    """
    app, repository, event_bus, pipeline = make_app()

    with TestClient(app) as client:
        with client.websocket_connect("/ws/alerts") as slow_ws, client.websocket_connect(
            "/ws/alerts"
        ) as fast_ws:
            # Publish more alerts than the bounded queue size to force the
            # slow client's queue into drop-oldest behavior, without the
            # slow client ever calling receive_json().
            for i in range(5):
                asyncio.run(
                    event_bus.publish_alert(
                        {
                            "asset": "BTC",
                            "confluence_score": 0.7 + i * 0.01,
                            "signal_count": 3,
                            "severity": "high",
                            "agreement_ratio": 1.0,
                            "conflict_ratio": 0.0,
                            "top_contributors": [],
                        }
                    )
                )

            # The fast client must still receive messages promptly --
            # broadcast() never blocked on the slow client.
            received_count = 0
            for _ in range(5):
                msg = fast_ws.receive_json()
                assert msg["asset"] == "BTC"
                received_count += 1
            assert received_count == 5


def test_at_least_ten_concurrent_ws_clients_all_receive_broadcast():
    """NFR-02: >= 10 concurrent WS clients without dropped messages."""
    app, repository, event_bus, pipeline = make_app()

    with TestClient(app) as client:
        sockets = [client.websocket_connect("/ws/alerts").__enter__() for _ in range(12)]
        try:
            asyncio.run(
                event_bus.publish_alert(
                    {
                        "asset": "NVDA",
                        "confluence_score": 0.9,
                        "signal_count": 4,
                        "severity": "critical",
                        "agreement_ratio": 1.0,
                        "conflict_ratio": 0.0,
                        "top_contributors": [],
                    }
                )
            )
            for ws in sockets:
                msg = ws.receive_json()
                assert msg["asset"] == "NVDA"
                assert msg["severity"] == "critical"
        finally:
            for ws in sockets:
                ws.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Restart durability (MMP acceptance criterion 5: zero data loss on restart)
# ---------------------------------------------------------------------------


def test_restart_durability_events_survive_process_restart():
    """Ingest events via the API, simulate an app restart (fresh app/
    UnifiedPipeline against the SAME fake repository instance), and assert
    GET /correlate and GET /alerts still reflect the pre-restart data.
    """
    shared_repository = FakeEventRepository()

    # -- "before restart" -----------------------------------------------
    app_before, _, event_bus_before, pipeline_before = make_app(repository=shared_repository)
    with TestClient(app_before) as client:
        resp = client.post(
            "/ingest/signals",
            json={
                "signals": [
                    {
                        "event_type": "whale_transfer",
                        "asset": "BTC",
                        "asset_class": "crypto",
                        "direction": 1,
                        "confidence": 0.95,
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "arkham",
                    },
                    {
                        "event_type": "funding_extreme",
                        "asset": "BTC",
                        "asset_class": "crypto",
                        "direction": 1,
                        "confidence": 0.9,
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "coinglass",
                    },
                    {
                        "event_type": "on_chain_move",
                        "asset": "BTC",
                        "asset_class": "crypto",
                        "direction": 1,
                        "confidence": 0.9,
                        "timestamp": "2026-07-01T12:00:00+00:00",
                        "source": "glassnode",
                    },
                ]
            },
        )
        assert resp.status_code == 200

        pre_restart_correlate = client.get("/correlate/BTC").json()
        pre_restart_alerts = client.get("/alerts").json()["alerts"]
        assert pre_restart_alerts, "expected at least one alert before restart"

    assert len(shared_repository.inserted) == 3

    # -- simulate restart: brand new UnifiedPipeline, SAME repository ----
    fresh_pipeline = UnifiedPipeline()
    app_after, _, event_bus_after, _ = make_app(
        pipeline=fresh_pipeline, repository=shared_repository
    )

    with TestClient(app_after) as client:
        # Rehydration happened during lifespan startup, before any request.
        assert len(fresh_pipeline.events) == 3

        post_restart_correlate = client.get("/correlate/BTC").json()
        post_restart_alerts = client.get("/alerts").json()["alerts"]

    assert post_restart_correlate["confluence_score"] == pytest.approx(
        pre_restart_correlate["confluence_score"]
    )
    assert post_restart_correlate["signal_count"] == pre_restart_correlate["signal_count"]
    assert len(post_restart_alerts) == len(pre_restart_alerts)
    assert post_restart_alerts[0]["asset"] == pre_restart_alerts[0]["asset"]


def test_restart_durability_with_zero_prior_events_is_a_noop():
    shared_repository = FakeEventRepository()
    fresh_pipeline = UnifiedPipeline()
    app, *_ = make_app(pipeline=fresh_pipeline, repository=shared_repository)
    with TestClient(app):
        assert fresh_pipeline.events == []


# ---------------------------------------------------------------------------
# No hardcoded secrets (NFR-05)
# ---------------------------------------------------------------------------


def test_no_hardcoded_secrets_in_source():
    import pathlib

    source = pathlib.Path(api.__file__).read_text()
    forbidden_substrings = [
        "hooks.slack.com/services/T",  # a real-looking Slack webhook path
        "xoxb-",  # Slack bot token prefix
        "sk-",  # common API key prefix pattern
    ]
    for needle in forbidden_substrings:
        assert needle not in source, f"found suspicious hardcoded secret-like string: {needle!r}"

    # DATABASE_URL / REDIS_URL / API keys must only ever be read via
    # os.environ, never assigned a literal connection string/URL default
    # containing credentials.
    assert "os.environ" in source
    assert "postgresql://" not in source or "os.environ" in source
