"""Tests for event_bus.py (Step 6) and freshness_guard.py.

No live Redis, no live Finnhub/CoinGlass/Polygon/Slack/Telegram credentials
are available in this sandbox (see task spec) — everything here runs
against:

    - `FakeRedis`: a small in-memory stand-in implementing exactly the
      subset of the `redis.asyncio.Redis` surface `EventBus` calls
      (`publish`, `lpush`, `ltrim`, `lrange`, `setex`, `get`, `keys`,
      `exists`, `pubsub`) plus a `FakePubSub`/fake channel fan-out so
      `subscribe_alerts()` can be exercised without a socket.
    - `aioresponses` for mocking the Slack/Telegram webhook POSTs and the
      Finnhub/CoinGlass/Polygon HTTP fetches made through real `aiohttp`
      sessions.
    - Poller `_fetch_raw()` overrides (per the class docstrings' documented
      testing seam) for the three concrete pollers, so canned fixture data
      exercises the real `_raw_to_articles`/`_raw_to_events` conversion
      logic without needing `aioresponses` for every poller test.

CLAUDE.md sections exercised: AD-07 (rolling buffer, alert cache, channels),
NFR-06 (5-minute feed-health TTL, hard requirement), FR-13/FR-20 (severity
gating to Slack/Telegram), the event-bus skill's explicit
heartbeat-on-zero-new-events and one-poller-exception-does-not-crash-others
warnings.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from aioresponses import aioresponses

from event_types import Event
from unified_pipeline import SignalFactory
import event_bus as eb
from event_bus import (
    ALERT_CACHE_TTL_SECONDS,
    ALERTS_CHANNEL,
    EVENTS_BUFFER_KEY,
    EVENTS_BUFFER_MAX_LEN,
    EVENTS_CHANNEL,
    FEED_HEALTH_TTL_SECONDS,
    AlertRouter,
    CoinGlassFundingPoller,
    EventBus,
    FeedPoller,
    FinnhubNewsPoller,
    PolygonOptionsPoller,
    slack_handler,
    telegram_handler,
)
from freshness_guard import (
    FreshnessGuardError,
    assert_feed_freshness,
    check_feed_freshness,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# FakeRedis — minimal in-memory stand-in for redis.asyncio.Redis
# ---------------------------------------------------------------------------


class _FakePubSub:
    """Fake pubsub handle: `listen()` yields whatever was pushed to the
    channel's queue via `FakeRedis.publish()` after this pubsub subscribed.
    """

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
    `EventBus` calls. Values are stored as `str` (mirrors
    `decode_responses=True` production configuration) so tests don't need to
    juggle bytes vs. str.
    """

    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._expiry: dict[str, float] = {}  # key -> monotonic expiry time
        self._lists: dict[str, list[str]] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    # -- internal TTL handling ------------------------------------------

    def _is_expired(self, key: str) -> bool:
        expiry = self._expiry.get(key)
        return expiry is not None and time.monotonic() >= expiry

    def _purge_if_expired(self, key: str) -> None:
        if key in self._strings and self._is_expired(key):
            del self._strings[key]
            self._expiry.pop(key, None)

    # -- pub/sub ----------------------------------------------------------

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

    # -- strings / TTL ------------------------------------------------------

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
        # Only supports the "prefix*" shape EventBus actually uses.
        assert pattern.endswith("*")
        prefix = pattern[:-1]
        return [
            k
            for k in list(self._strings.keys())
            if k.startswith(prefix) and not self._is_expired(k)
        ]

    def force_expire(self, name: str) -> None:
        """Test helper: simulate TTL expiry immediately."""
        self._expiry[name] = time.monotonic() - 1

    def get_ttl(self, name: str) -> Optional[float]:
        """Test helper: seconds remaining until expiry (None if no TTL set)."""
        expiry = self._expiry.get(name)
        if expiry is None:
            return None
        return expiry - time.monotonic()

    # -- lists --------------------------------------------------------------

    async def lpush(self, name: str, *values: str) -> int:
        lst = self._lists.setdefault(name, [])
        for v in values:
            lst.insert(0, v)
        return len(lst)

    async def ltrim(self, name: str, start: int, end: int) -> bool:
        lst = self._lists.get(name, [])
        # Python slicing handles the -1 "to the end" case identically to Redis.
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


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def bus(fake_redis: FakeRedis) -> EventBus:
    return EventBus(fake_redis)


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


# ---------------------------------------------------------------------------
# EventBus.publish_event — rolling buffer (AD-07)
# ---------------------------------------------------------------------------


async def test_publish_event_publishes_and_buffers(bus: EventBus, fake_redis: FakeRedis):
    event = make_event()
    await bus.publish_event(event)

    recent = await bus.get_recent_events()
    assert len(recent) == 1
    assert recent[0]["asset"] == "BTC"
    assert recent[0]["event_type"] == "whale_transfer"
    assert recent[0]["timestamp"] == NOW.isoformat()


async def test_rolling_buffer_caps_at_500(bus: EventBus):
    for i in range(520):
        await bus.publish_event(make_event(asset=f"A{i}"))

    recent = await bus.get_recent_events()
    assert len(recent) == EVENTS_BUFFER_MAX_LEN

    # LPUSH means most-recently-published ends up at index 0.
    assert recent[0]["asset"] == "A519"
    # Oldest surviving entry should be the 20th published (indices 20..519
    # survive when 520 are pushed and capped at 500).
    assert recent[-1]["asset"] == "A20"


# ---------------------------------------------------------------------------
# EventBus.publish_alert / subscribe_alerts — alerts channel + cache (AD-07)
# ---------------------------------------------------------------------------


async def test_publish_alert_caches_per_asset_with_1h_ttl(bus: EventBus, fake_redis: FakeRedis):
    payload = {"asset": "NVDA", "confluence_score": 0.9, "severity": "critical"}
    await bus.publish_alert(payload)

    cached = await fake_redis.get("alerts:asset:NVDA")
    assert cached is not None
    assert json.loads(cached) == payload

    ttl = fake_redis.get_ttl("alerts:asset:NVDA")
    assert ttl is not None
    assert ttl == pytest.approx(ALERT_CACHE_TTL_SECONDS, abs=2)


async def test_subscribe_alerts_yields_published_payloads(bus: EventBus):
    payload = {"asset": "BTC", "confluence_score": 0.8, "severity": "high"}

    async def publisher():
        # Give subscribe_alerts a moment to subscribe before publishing.
        await asyncio.sleep(0.01)
        await bus.publish_alert(payload)

    received = []

    async def consumer():
        async for alert in bus.subscribe_alerts():
            received.append(alert)
            break

    await asyncio.gather(consumer(), publisher())
    assert received == [payload]


# ---------------------------------------------------------------------------
# EventBus.heartbeat / get_feed_health — NFR-06 (hard 5-minute TTL)
# ---------------------------------------------------------------------------


async def test_heartbeat_sets_ttl_to_exactly_300_seconds(bus: EventBus, fake_redis: FakeRedis):
    await bus.heartbeat("finnhub_news")

    ttl = fake_redis.get_ttl("feed_health:finnhub_news")
    assert ttl is not None
    assert ttl == pytest.approx(FEED_HEALTH_TTL_SECONDS, abs=1)
    assert FEED_HEALTH_TTL_SECONDS == 300


async def test_get_feed_health_reports_fresh_source(bus: EventBus):
    await bus.heartbeat("coinglass_funding")

    health = await bus.get_feed_health()
    assert "coinglass_funding" in health
    assert health["coinglass_funding"]["is_stale"] is False
    assert health["coinglass_funding"]["last_seen"] is not None


async def test_get_feed_health_omits_expired_source(bus: EventBus, fake_redis: FakeRedis):
    await bus.heartbeat("polygon_options")
    fake_redis.force_expire("feed_health:polygon_options")

    health = await bus.get_feed_health()
    assert "polygon_options" not in health


async def test_is_source_fresh(bus: EventBus, fake_redis: FakeRedis):
    await bus.heartbeat("finnhub_news")
    assert await bus.is_source_fresh("finnhub_news") is True

    fake_redis.force_expire("feed_health:finnhub_news")
    assert await bus.is_source_fresh("finnhub_news") is False

    assert await bus.is_source_fresh("never_reported") is False


# ---------------------------------------------------------------------------
# FeedPoller correctness — heartbeat-on-zero-new-events, exception isolation
# ---------------------------------------------------------------------------


class _StubPoller(FeedPoller):
    source_name = "stub_source"

    def __init__(self, events_to_return: Optional[list[Event]] = None, raise_error: bool = False):
        self._events_to_return = events_to_return or []
        self._raise_error = raise_error
        self.poll_once_calls = 0

    async def poll_once(self) -> list[Event]:
        self.poll_once_calls += 1
        if self._raise_error:
            raise RuntimeError("simulated upstream API failure")
        return self._events_to_return


async def test_heartbeat_updates_on_zero_new_events(bus: EventBus, fake_redis: FakeRedis):
    poller = _StubPoller(events_to_return=[])
    result = await poller.run_one_cycle(bus)

    assert result == []
    assert poller.poll_once_calls == 1
    assert await bus.is_source_fresh("stub_source") is True


async def test_heartbeat_updates_even_when_poll_once_raises(bus: EventBus):
    poller = _StubPoller(raise_error=True)
    result = await poller.run_one_cycle(bus)

    assert result == []
    # The critical NFR-06 assertion: heartbeat must still fire even though
    # poll_once() blew up, since the loop itself is still alive and will
    # retry next interval.
    assert await bus.is_source_fresh("stub_source") is True


async def test_single_poller_exception_does_not_crash_others(bus: EventBus):
    bad_poller = _StubPoller(raise_error=True)
    bad_poller.source_name = "bad_source"
    good_poller = _StubPoller(events_to_return=[make_event(asset="ETH")])
    good_poller.source_name = "good_source"

    # Simulates one iteration of a run_all()-style gather -- the bad
    # poller's exception must not propagate and prevent the good poller's
    # cycle (or the test itself) from completing.
    bad_result, good_result = await asyncio.gather(
        bad_poller.run_one_cycle(bus),
        good_poller.run_one_cycle(bus),
    )

    assert bad_result == []
    assert len(good_result) == 1
    assert await bus.is_source_fresh("bad_source") is True
    assert await bus.is_source_fresh("good_source") is True

    recent = await bus.get_recent_events()
    assert len(recent) == 1
    assert recent[0]["asset"] == "ETH"


async def test_run_publishes_events_returned_by_poll_once(bus: EventBus):
    events = [make_event(asset="SOL"), make_event(asset="AVAX")]
    poller = _StubPoller(events_to_return=events)
    result = await poller.run_one_cycle(bus)

    assert result == events
    recent = await bus.get_recent_events()
    assert {e["asset"] for e in recent} == {"SOL", "AVAX"}


# ---------------------------------------------------------------------------
# AlertRouter — severity gating (FR-13/FR-20)
# ---------------------------------------------------------------------------


def _make_alert(severity: str) -> dict:
    return {
        "asset": "NVDA",
        "confluence_score": 0.9 if severity in ("high", "critical") else 0.3,
        "signal_count": 4,
        "severity": severity,
        "agreement_ratio": 1.0,
        "conflict_ratio": 0.0,
        "top_contributors": [],
    }


async def test_low_medium_severity_does_not_dispatch(bus: EventBus):
    router = AlertRouter(bus)
    calls = []

    async def fake_handler(payload):
        calls.append(payload)

    router.register_handler("slack", fake_handler)
    router.register_handler("telegram", fake_handler)

    await router.dispatch(_make_alert("low"))
    await router.dispatch(_make_alert("medium"))

    assert calls == []


async def test_high_critical_severity_dispatches_to_all_handlers(bus: EventBus):
    router = AlertRouter(bus)
    slack_calls = []
    telegram_calls = []

    async def fake_slack(payload):
        slack_calls.append(payload)

    async def fake_telegram(payload):
        telegram_calls.append(payload)

    router.register_handler("slack", fake_slack)
    router.register_handler("telegram", fake_telegram)

    high_alert = _make_alert("high")
    critical_alert = _make_alert("critical")
    await router.dispatch(high_alert)
    await router.dispatch(critical_alert)

    assert slack_calls == [high_alert, critical_alert]
    assert telegram_calls == [high_alert, critical_alert]


async def test_handler_exception_does_not_block_other_handlers(bus: EventBus):
    router = AlertRouter(bus)
    telegram_calls = []

    async def broken_slack(payload):
        raise RuntimeError("webhook down")

    async def fake_telegram(payload):
        telegram_calls.append(payload)

    router.register_handler("slack", broken_slack)
    router.register_handler("telegram", fake_telegram)

    await router.dispatch(_make_alert("critical"))
    assert telegram_calls  # telegram still received it despite slack raising


async def test_custom_severity_gate_can_be_stricter(bus: EventBus):
    """Simulates a Phase-2-style Twilio handler with a stricter gate than
    the Slack/Telegram default -- must never widen beyond {"high","critical"}."""
    router = AlertRouter(bus)
    sms_calls = []

    async def fake_sms(payload):
        sms_calls.append(payload)

    router.register_handler("twilio_sms", fake_sms, severity_gate=frozenset({"critical"}))

    await router.dispatch(_make_alert("high"))
    assert sms_calls == []

    await router.dispatch(_make_alert("critical"))
    assert len(sms_calls) == 1


# ---------------------------------------------------------------------------
# slack_handler / telegram_handler — correct POST shape (mocked HTTP)
# ---------------------------------------------------------------------------


async def test_slack_handler_posts_to_webhook_url():
    webhook_url = "https://hooks.slack.com/services/T000/B000/xxx"
    payload = _make_alert("critical")

    with aioresponses() as mocked:
        mocked.post(webhook_url, status=200, payload={"ok": True})
        await slack_handler(payload, webhook_url)

        mocked.assert_called_once()
        request = list(mocked.requests.values())[0][0]
        assert request.kwargs["json"]["text"]
        assert "NVDA" in request.kwargs["json"]["text"]
        assert "CRITICAL" in request.kwargs["json"]["text"]


async def test_telegram_handler_posts_to_send_message_endpoint():
    bot_token = "123456:fake-token"
    chat_id = "-100999"
    payload = _make_alert("high")
    expected_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    with aioresponses() as mocked:
        mocked.post(expected_url, status=200, payload={"ok": True})
        await telegram_handler(payload, bot_token, chat_id)

        mocked.assert_called_once()
        request = list(mocked.requests.values())[0][0]
        assert request.kwargs["json"]["chat_id"] == chat_id
        assert "NVDA" in request.kwargs["json"]["text"]


# ---------------------------------------------------------------------------
# freshness_guard.py
# ---------------------------------------------------------------------------


async def test_check_feed_freshness_flags_missing_key_as_stale(bus: EventBus):
    await bus.heartbeat("finnhub_news")
    # "coinglass_funding" never heartbeats -> stale.

    result = await check_feed_freshness(bus, ["finnhub_news", "coinglass_funding"])
    assert result == {"finnhub_news": True, "coinglass_funding": False}


async def test_check_feed_freshness_flags_expired_key_as_stale(bus: EventBus, fake_redis: FakeRedis):
    await bus.heartbeat("polygon_options")
    fake_redis.force_expire("feed_health:polygon_options")

    result = await check_feed_freshness(bus, ["polygon_options"])
    assert result == {"polygon_options": False}


async def test_assert_feed_freshness_raises_when_any_source_stale(bus: EventBus):
    await bus.heartbeat("finnhub_news")

    with pytest.raises(FreshnessGuardError) as excinfo:
        await assert_feed_freshness(bus, ["finnhub_news", "coinglass_funding"])

    assert excinfo.value.stale_sources == ["coinglass_funding"]
    assert excinfo.value.freshness["finnhub_news"] is True


async def test_assert_feed_freshness_passes_when_all_fresh(bus: EventBus):
    await bus.heartbeat("finnhub_news")
    await bus.heartbeat("coinglass_funding")

    result = await assert_feed_freshness(bus, ["finnhub_news", "coinglass_funding"])
    assert result == {"finnhub_news": True, "coinglass_funding": True}


# ---------------------------------------------------------------------------
# FinnhubNewsPoller / CoinGlassFundingPoller / PolygonOptionsPoller —
# well-formed Events from canned fixture data
# ---------------------------------------------------------------------------


class _FixtureFinnhubPoller(FinnhubNewsPoller):
    def __init__(self, fixture: list[dict], **kwargs):
        super().__init__(api_key="fake-key", **kwargs)
        self._fixture = fixture

    async def _fetch_raw(self) -> list[dict]:
        return self._fixture


async def test_finnhub_poller_produces_news_events_from_fixture():
    fixture = [
        {
            "headline": "NVDA surges on record earnings beat",
            "summary": "Shares of NVDA surged after beating expectations.",
            "url": "https://example.com/nvda-earnings",
            "datetime": int(NOW.timestamp()),
            "source": "finnhub",
        }
    ]
    poller = _FixtureFinnhubPoller(fixture)
    events = await poller.poll_once()

    assert len(events) >= 1
    for event in events:
        assert event.event_type == "news"
        assert event.source == "finnhub"
        assert event.asset  # ticker was extracted


async def test_finnhub_poller_zero_articles_returns_zero_events():
    poller = _FixtureFinnhubPoller([])
    events = await poller.poll_once()
    assert events == []


class _FixtureCoinGlassPoller(CoinGlassFundingPoller):
    def __init__(self, fixture: list[dict], **kwargs):
        super().__init__(api_key="fake-key", **kwargs)
        self._fixture = fixture

    async def _fetch_raw(self) -> list[dict]:
        return self._fixture


async def test_coinglass_poller_produces_funding_extreme_events_above_threshold():
    fixture = [
        {"symbol": "BTC", "exchangeName": "Binance", "rate": 0.015, "openInterest": 5e9},
        {"symbol": "ETH", "exchangeName": "Bybit", "rate": 0.001, "openInterest": 1e9},  # below threshold
    ]
    poller = _FixtureCoinGlassPoller(fixture)
    events = await poller.poll_once()

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "funding_extreme"
    assert event.asset == "BTC"
    assert event.source == "coinglass"
    assert event.meta["funding_rate"] == 0.015
    # Positive extreme funding => contrarian bearish direction.
    assert event.direction == -1


async def test_coinglass_poller_negative_extreme_is_bullish():
    fixture = [{"symbol": "BTC", "exchangeName": "Binance", "rate": -0.02, "openInterest": 5e9}]
    poller = _FixtureCoinGlassPoller(fixture)
    events = await poller.poll_once()

    assert len(events) == 1
    assert events[0].direction == 1


async def test_coinglass_poller_no_extreme_readings_returns_zero_events():
    fixture = [{"symbol": "BTC", "exchangeName": "Binance", "rate": 0.0001, "openInterest": 5e9}]
    poller = _FixtureCoinGlassPoller(fixture)
    events = await poller.poll_once()
    assert events == []


class _FixturePolygonPoller(PolygonOptionsPoller):
    def __init__(self, fixture: list[dict], **kwargs):
        super().__init__(api_key="fake-key", **kwargs)
        self._fixture = fixture

    async def _fetch_raw(self) -> list[dict]:
        return self._fixture


async def test_polygon_poller_produces_options_flow_events_above_threshold():
    fixture = [
        {
            "underlying": "NVDA",
            "contract_type": "call",
            "strike": 150.0,
            "expiry": "2026-08-21",
            "premium": 250_000.0,
            "side": "buy",
        },
        {
            "underlying": "AAPL",
            "contract_type": "put",
            "strike": 190.0,
            "expiry": "2026-08-21",
            "premium": 5_000.0,  # below threshold
            "side": "buy",
        },
    ]
    poller = _FixturePolygonPoller(fixture)
    events = await poller.poll_once()

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "options_flow"
    assert event.asset == "NVDA"
    assert event.source == "polygon"
    assert event.meta["premium"] == 250_000.0
    # call+buy => bullish.
    assert event.direction == 1


async def test_polygon_poller_put_buy_is_bearish():
    fixture = [
        {
            "underlying": "TSLA",
            "contract_type": "put",
            "strike": 200.0,
            "expiry": "2026-08-21",
            "premium": 300_000.0,
            "side": "buy",
        }
    ]
    poller = _FixturePolygonPoller(fixture)
    events = await poller.poll_once()

    assert len(events) == 1
    assert events[0].direction == -1


async def test_polygon_poller_no_flow_above_threshold_returns_zero_events():
    fixture = [
        {
            "underlying": "AAPL",
            "contract_type": "call",
            "strike": 200.0,
            "expiry": "2026-08-21",
            "premium": 1_000.0,
            "side": "buy",
        }
    ]
    poller = _FixturePolygonPoller(fixture)
    events = await poller.poll_once()
    assert events == []


# ---------------------------------------------------------------------------
# Poller HTTP fetch mocked end-to-end via aioresponses (exercises the real
# aiohttp _fetch_raw code path, not just the fixture override).
# ---------------------------------------------------------------------------


def _url_pattern(base_url: str) -> re.Pattern:
    """Match `base_url` with any (or no) query string -- `_fetch_raw()`
    always appends query params (`?token=...`/`?apiKey=...`), and
    `aioresponses` matches registered plain-string URLs exactly (including
    query string), so a regex anchored on the base URL is used instead of
    trying to predict/duplicate the exact param encoding here.
    """
    return re.compile(rf"^{re.escape(base_url)}(\?.*)?$")


async def test_coinglass_poller_fetch_raw_via_mocked_http():
    poller = CoinGlassFundingPoller(api_key="fake-key")
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern(poller.API_URL),
            status=200,
            payload={"data": [{"symbol": "BTC", "exchangeName": "Binance", "rate": 0.02, "openInterest": 1e9}]},
        )
        raw = await poller._fetch_raw()

    assert raw == [{"symbol": "BTC", "exchangeName": "Binance", "rate": 0.02, "openInterest": 1e9}]


async def test_polygon_poller_fetch_raw_via_mocked_http():
    poller = PolygonOptionsPoller(api_key="fake-key")
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern(poller.API_URL),
            status=200,
            payload={"results": [{"underlying": "NVDA", "premium": 200000.0}]},
        )
        raw = await poller._fetch_raw()

    assert raw == [{"underlying": "NVDA", "premium": 200000.0}]


async def test_finnhub_poller_fetch_raw_via_mocked_http():
    poller = FinnhubNewsPoller(api_key="fake-key")
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern(poller.API_URL),
            status=200,
            payload=[{"headline": "Test headline", "url": "https://x.com/1", "datetime": int(NOW.timestamp())}],
        )
        raw = await poller._fetch_raw()

    assert raw[0]["headline"] == "Test headline"
