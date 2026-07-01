"""Minimal Phase 1 feed-freshness guard — CLAUDE.md §7 Phase 1 TODO.

Per the task spec: this is deliberately the MINIMAL Phase 1 version. The
full Phase 4 vision (`intelligence_brief_service.py` integration,
per-source-group thresholds, scheduled + event-triggered briefing gates —
CLAUDE.md OQ-14) is explicitly OUT of scope here. This module only answers
one question: "before a scoring job runs, are the sources it depends on
still reporting a live feed-health heartbeat?" (NFR-06).

Two calling conventions are supported so callers can pick warn-vs-block
without this module needing to know which one is appropriate for a given
context (that policy decision belongs to the caller, e.g. a cron scoring
job might warn-and-continue while a critical briefing job in Phase 4 might
hard-block):

    check_feed_freshness(event_bus, required_sources)
        -> {source: True/False}, never raises. Soft/informational use.

    assert_feed_freshness(event_bus, required_sources)
        -> same dict, but raises FreshnessGuardError if ANY required source
           is stale. Hard-block use.

Wire `check_feed_freshness` (or `assert_feed_freshness`, if hard-blocking is
desired) in front of `UnifiedPipeline.correlate_all()` / `alert_payloads()`
once a scoring-loop consumer exists (that consumer itself is not built in
this step — see `event_bus.py`'s module docstring: pollers publish raw
Events, a separate scoring-loop consumer of the `events` channel is
responsible for invoking `CorrelationScorer`).
"""

from __future__ import annotations

from typing import Iterable

from event_bus import EventBus


class FreshnessGuardError(Exception):
    """Raised by `assert_feed_freshness` when one or more required sources
    are stale (their feed-health key has expired or was never written).

    Carries the full freshness map (not just the failing sources) on
    `.freshness` so a caller that catches this can still log/report which
    sources WERE fresh, not just which ones failed.
    """

    def __init__(self, stale_sources: list[str], freshness: dict[str, bool]) -> None:
        self.stale_sources = stale_sources
        self.freshness = freshness
        super().__init__(
            "Feed freshness check failed — stale/missing feed-health for: "
            + ", ".join(sorted(stale_sources))
        )


async def check_feed_freshness(
    event_bus: EventBus, required_sources: Iterable[str]
) -> dict[str, bool]:
    """Return `{source: is_fresh}` for every source in `required_sources`.

    A source is fresh iff its feed-health heartbeat key currently exists in
    Redis (NFR-06: staleness is determined purely by TTL auto-expiry — a
    missing key means either the key expired or the poller never reported,
    both of which are indistinguishable "stale" states from a caller's
    perspective, and both are treated identically here).

    Never raises on staleness — this is the soft/warning-style check. Use
    `assert_feed_freshness` for hard-blocking behavior.
    """
    freshness: dict[str, bool] = {}
    for source in required_sources:
        freshness[source] = await event_bus.is_source_fresh(source)
    return freshness


async def assert_feed_freshness(
    event_bus: EventBus, required_sources: Iterable[str]
) -> dict[str, bool]:
    """Like `check_feed_freshness`, but raises `FreshnessGuardError` if any
    required source is stale.

    Returns the same `{source: is_fresh}` mapping on success (all fresh) so
    callers that want the hard-block behavior don't have to call
    `check_feed_freshness` a second time to get the detail dict.
    """
    freshness = await check_feed_freshness(event_bus, required_sources)
    stale_sources = [source for source, fresh in freshness.items() if not fresh]
    if stale_sources:
        raise FreshnessGuardError(stale_sources, freshness)
    return freshness


__all__ = [
    "FreshnessGuardError",
    "check_feed_freshness",
    "assert_feed_freshness",
]
