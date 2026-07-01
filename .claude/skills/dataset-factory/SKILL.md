---
name: dataset-factory
description: Reference knowledge for Trade-Dash's algorithm-ready dataset factory (dataset_factory.py) — minute/daily/options/news dataset compilation, Redis/S3/Minio publishing, and Celery/Kubernetes workers.
when_to_use: building dataset extraction jobs, wiring Celery tasks, setting up replay from archived datasets, joining OHLCV with news sentiment
---

Trade-Dash's dataset factory lives in `dataset_factory.py` (see CLAUDE.md
§5, §2 FR-33/34, §8 OQ-10, NFR-08).

## Pipeline
normalize → align timestamps (UTC, explicit bar-close convention) → join
news sentiment onto price bars → publish to Redis (hot cache, short TTL),
file (Parquet preferred over CSV), and S3/Minio (durable archive,
versioned path e.g. `s3://datasets/{asset_class}/{granularity}/{run_date}/`).

## Batch only (OQ-10 — don't build streaming)
Celery + Kubernetes Jobs. **No Kafka/Flink** unless explicitly asked — the
resolved decision defers streaming to a future sub-second-feature-pipeline
need.

## Idempotency
A retried Celery task must not duplicate/corrupt output — tasks should
overwrite/upsert deterministically, never blind-append.

## Replay
Must reconstruct exact chronological order — this feeds lookahead-sensitive
consumers (backtesting harness, ML training). Any reshuffling silently
corrupts them.

## NFR-08
Distributed workers must work via both Kubernetes and docker-compose —
don't build a Kubernetes-only path.

## Delegate
- Deep implementation work → `dataset-factory-expert` agent.
- Pre-merge review → `dataset-factory-reviewer` agent.
