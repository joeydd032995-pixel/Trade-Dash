---
name: event-bus
description: Reference knowledge for Trade-Dash's Redis event bus, feed pollers, and alert router (event_bus.py) — EventBus, FeedPoller subclasses (Finnhub/CoinGlass/Polygon), AlertRouter, and Slack/Telegram/Discord/Twilio/email delivery.
when_to_use: wiring a new poller, debugging feed-health staleness, adding an alert delivery channel, tuning poll intervals against rate limits
---

Trade-Dash's event bus lives in `event_bus.py` (see CLAUDE.md §4 AD-07, §5,
§2 FR-01/02/05/13/20, NFR-06).

## Architecture (AD-07)
Redis channels: `events` (raw stream, 500-event rolling buffer for late
joiners) and `alerts` (scored payloads). Per-asset alert cache TTL 1h.
**Feed-health keys TTL 5 min — hard requirement (NFR-06).**

## Pollers
`FinnhubNewsPoller`, `CoinGlassFundingPoller`, `PolygonOptionsPoller` (base:
`FeedPoller`). Each: fetch → convert to typed Events via `SignalFactory`/
`NLPPipeline` → publish to `events` → **heartbeat the feed-health key on
every successful poll cycle, including zero-new-item polls**. Heartbeat-only-
on-new-data is the most common bug here — it makes quiet-but-healthy feeds
falsely report stale.

- Respect each provider's actual documented rate limits — don't guess an
  interval.
- One poller's exception must not crash the process for other pollers —
  wrap each in its own try/except with backoff.

## AlertRouter (FR-13, FR-20)
Subscribes to `alerts`, dispatches to `slack_handler`/`telegram_handler`
(Phase 1) and Discord/Twilio/email (Phase 2). Only high/critical severity
goes to paid/interruptive channels (Twilio SMS especially — never fire SMS
on every alert). All alerts broadcast over WebSocket regardless of
severity.

## Secrets
Webhook URLs/bot tokens/API keys from env vars only (NFR-05) — never
literal in code.

## Delegate
- Deep implementation work → `event-bus-expert` agent.
- Pre-merge review → `event-bus-reviewer` agent.
