---
name: infrastructure-ops
description: Reference knowledge for Trade-Dash's Phase 1 infrastructure setup — docker-compose for Postgres/TimescaleDB/pgvector/Redis, env-var and secrets wiring, schema migration running, and local-vs-Kubernetes deployment parity.
when_to_use: setting up local dev infrastructure, writing docker-compose.yml, wiring poller API keys/webhook secrets, running schema.sql migrations, deciding between docker-compose and Kubernetes for a deployment
---

Trade-Dash's infrastructure/ops layer is a cross-cutting concern spanning
`database-schema` and `event-bus` — it doesn't own module logic, only how
those modules get deployed and configured. See CLAUDE.md §6 Quick Start, §7
Phase 1 TODOs, NFR-05/06/08.

## docker-compose services (local dev)
Postgres (with `timescaledb-ha` image so TimescaleDB + pgvector extensions
are available out of the box), Redis, and the API service. Wire them so
`docker-compose up` gets a developer to the point where
`psql $DATABASE_URL -f schema.sql` and `uvicorn api_service:app --host
0.0.0.0 --port 8000 --reload` both work against the composed services,
matching CLAUDE.md §6's exact Quick Start sequence.

## Env vars (NFR-05 — never hardcode, ever)
Full required set: `REDIS_URL`, `DATABASE_URL`, `FINNHUB_API_KEY`,
`COINGLASS_API_KEY`, `POLYGON_API_KEY`, `SLACK_WEBHOOK_URL`,
`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `OPENROUTER_API_KEY`,
`OLLAMA_BASE_URL`. Provide an `.env.example` with placeholder values (never
real keys) so onboarding doesn't require guessing the variable names.

## Migrations
`schema.sql` (see `database-schema` skill for the schema itself) is applied
via `psql $DATABASE_URL -f schema.sql` today; as the schema grows past a
single file, sequence migrations additively (add → backfill → cutover →
drop across separate steps, never a single destructive migration) — same
discipline the `database-schema` skill enforces for the schema design
itself.

## Poller credential wiring
The three Phase 1 pollers (`FinnhubNewsPoller`, `CoinGlassFundingPoller`,
`PolygonOptionsPoller` — see `event-bus` skill) each need their API key
sourced from env vars, not passed as literals in any compose file or
manifest committed to the repo. Rate-limit-aware poll intervals belong to
the pollers themselves; this skill only owns getting credentials safely
into the running process.

## Feed-health TTL at the ops layer
NFR-06 requires feed-health keys to auto-expire after 5 minutes — this is
enforced by Redis TTL configuration, not application retry logic; confirm
whatever Redis config/image is used doesn't override or disable key
expiry.

## Local vs. Kubernetes (NFR-08)
Both docker-compose (local/small-scale) and Kubernetes (distributed
backtest workers, later phases) must work from the same env-var/secrets
contract — don't build Kubernetes manifests that assume a different
config-injection mechanism than the docker-compose path uses.

## Delegate
No dedicated expert/reviewer agent pair exists for this skill — it's
operational glue, not a code module in CLAUDE.md §5. For schema design
questions, use `database-schema-expert`/`-reviewer`; for poller logic,
use `event-bus-expert`/`-reviewer`.
