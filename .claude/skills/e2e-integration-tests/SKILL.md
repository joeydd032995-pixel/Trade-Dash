---
name: e2e-integration-tests
description: Reference knowledge for Trade-Dash's Phase 1 end-to-end integration test — the article/signal ingest → NLP → CorrelationScorer → WebSocket → Slack/Telegram → Postgres chain that spans all 6 Phase 1 modules, and the MMP acceptance criteria it must satisfy.
when_to_use: writing the full E2E test, verifying an MMP acceptance criterion, mocking external APIs for CI, diagnosing which module in the chain broke an end-to-end run
---

Trade-Dash's Phase 1 E2E test is a cross-cutting concern spanning 6 module
skills — it doesn't belong to any single one. See CLAUDE.md §7 ("Full
end-to-end integration test: article ingest → NLP → scorer → WebSocket
panel → Slack/Telegram") and §3 MMP Acceptance Criteria.

## The chain under test
1. Article/signal ingest (`POST /ingest/articles`, `POST /ingest/signals` —
   see `api-service` skill) →
2. NLP dedup/sentiment/ticker extraction (see `nlp-pipeline` skill) or
   `SignalFactory` typed construction (see `event-pipeline` skill) →
3. `CorrelationScorer` confluence computation (see `correlation-scorer`
   skill) →
4. `alert_payloads()` threshold gate → persistence to Postgres (see
   `database-schema` skill) and publish to Redis `alerts` channel (see
   `event-bus` skill) →
5. WebSocket broadcast (`WS /ws/alerts`) →
6. `AlertRouter` → Slack/Telegram delivery.

## MMP acceptance criteria (§3) — write these as literal test assertions
1. Pipeline ingests ≥3 live article sources and ≥2 signal sources.
2. Confluence score fires for BTC and ≥1 equity within 60s of a real event.
3. WebSocket panel renders an alert within 5s of score computation.
4. Slack or Telegram notification delivered within 10s of alert.
5. All events and alerts persisted to PostgreSQL with zero data loss on
   restart (kill and restart the service mid-test, confirm no gap).
6. Feed health monitor accurately reports source staleness (stop a poller,
   confirm `GET /feed-health` reflects it within the 5-min TTL).

## Test doubles for CI
Mock Finnhub/CoinGlass/Polygon responses and Slack/Telegram webhook
endpoints so the suite runs without live third-party credentials — real
credentials are for manual/staging verification only, never required for
CI to pass. Use fixture payloads that match each provider's actual response
shape (not hand-simplified JSON) so the NLP/SignalFactory conversion layer
is exercised faithfully.

## When a run fails
Isolate which of the 6 stages broke, then delegate the fix to that
module's `-expert` agent and get it reviewed by the matching `-reviewer`
agent — this skill owns test orchestration, not the fix itself.

## Delegate
No dedicated expert/reviewer agent pair exists for this skill — it's a
cross-cutting test concern, not a code module in CLAUDE.md §5. Route actual
defects to the owning module's agent pair (`event-pipeline-expert`,
`nlp-pipeline-expert`, `correlation-scorer-expert`, `api-service-expert`,
`event-bus-expert`, or `database-schema-expert`, plus their `-reviewer`
counterparts).
