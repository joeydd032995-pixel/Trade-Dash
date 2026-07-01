---
name: database-schema
description: Reference knowledge for Trade-Dash's PostgreSQL/TimescaleDB/pgvector schema (schema.sql) — the 9 core tables, hypertables, compression policy, and vector indexes.
when_to_use: writing migrations, adding indexes, wiring TimescaleDB compression, debugging slow correlation-score queries, adding a pgvector similarity index
---

Trade-Dash's schema lives in `schema.sql` (see CLAUDE.md §4 AD-08, §5, §8
OQ-01/06, NFR-03).

## Tables
`assets`, `articles` (fingerprint dedup + `vector(384)` embedding),
`events` (hypertable on `ts`, JSONB `meta` with GIN index),
`correlation_scores`, `alerts` (severity, channel, ACK), `positions`,
`trade_journal` (R-multiple, setup×regime), `greeks_snapshots`,
`onchain_flows`, `feed_health`.

## TimescaleDB (AD-08)
`SELECT create_hypertable('events', 'ts');`. Compression policy (OQ-06
verdict: store all scores, compress after 7 days) — segment by `asset` for
good ratio: `add_compression_policy('correlation_scores', INTERVAL '7 days')`.

## pgvector (AD-08)
`CREATE EXTENSION IF NOT EXISTS vector;` (extension name is `vector`, not
`pgvector` — common mistake). `articles.embedding vector(384)` must match
`all-MiniLM-L6-v2`'s output dimension. Index with `ivfflat`/`hnsw` +
`vector_cosine_ops` — a sequential scan defeats the purpose.

## Append-only enforcement (NFR-03)
`events`/`alerts` are insert-only in the normal path — corrections are new
rows referencing the original, never in-place `UPDATE`/`DELETE`. Consider
`REVOKE UPDATE, DELETE` at the DB role level. `alerts.acknowledged` is the
one legitimate mutable field.

## Migration hygiene
Every new table/column needs an FR/AD citation in the migration comment.
Never drop a column still read elsewhere in the same migration — split into
add→backfill→cutover→drop steps.

## Delegate
- Deep implementation work → `database-schema-expert` agent.
- Pre-merge review → `database-schema-reviewer` agent.
