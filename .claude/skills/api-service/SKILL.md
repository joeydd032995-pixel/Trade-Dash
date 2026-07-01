---
name: api-service
description: Reference knowledge for Trade-Dash's FastAPI REST + WebSocket service (api_service.py) — route contracts, DB-backed event store migration, and NFR compliance (latency, concurrency, secrets).
when_to_use: adding or modifying FastAPI routes, wiring the WebSocket alert broadcast, migrating in-memory state to Postgres, debugging WS reconnect/broadcast issues
---

Trade-Dash's API service lives in `api_service.py` (see CLAUDE.md §5, §2
FR-19..22, §7 Phase 1 TODOs, NFR-01/02/05).

## Route surface (don't rename without updating CLAUDE.md §5 + frontend)
`POST /ingest/articles`, `POST /ingest/signals`, `GET /correlate/{asset}`,
`GET /correlate`, `GET /alerts`, `GET /feed-health`, `POST /demo/seed`,
`WS /ws/alerts`.

## Stack conventions
`fastapi` + `uvicorn`, `pydantic` v2 models on every route (never raw
dicts), `asyncpg` (no ORM), `redis.asyncio`. Fully async — no blocking calls
in a handler; push sync-only SDKs to a thread pool.

## Hard NFR gates
- **NFR-01**: ingest → WS alert latency < 5s — profile anything added
  between ingest and broadcast.
- **NFR-02**: ≥10 concurrent WS clients, no dropped messages — broadcast via
  per-client send queues; a slow client must never block delivery to
  others.
- **NFR-05**: zero hardcoded keys/URLs/credentials — everything from env
  vars (see CLAUDE.md §6 Quick Start).

## DB-backed event store migration (current Phase 1 TODO)
Writes go to Postgres (append-only) **and** publish to the Redis `events`
channel — don't make this a silent Redis bypass. Reads check the Redis hot
cache (AD-07, 1h TTL) before falling back to Postgres. Zero data loss on
restart is an MMP acceptance criterion.

## WS payload shape
Severity, confluence score, signal count, top-4 contributing signals —
don't change this without coordinating with the frontend.

## Delegate
- Deep implementation work → `api-service-expert` agent.
- Pre-merge review → `api-service-reviewer` agent.
