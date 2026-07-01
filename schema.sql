-- =============================================================================
-- Trade-Dash — PostgreSQL / TimescaleDB / pgvector schema
-- =============================================================================
-- Phase 1 (MMP v1.0) schema. See CLAUDE.md §5 "Database (SQL)", §4 AD-08,
-- §8 OQ-01/OQ-06, and NFR-03 for the requirements this file implements.
--
-- Tables (9 core + feed_health, per CLAUDE.md §5's schema.sql description):
--   assets, articles, events, correlation_scores, alerts, positions,
--   trade_journal, greeks_snapshots, onchain_flows, feed_health
--
-- This is Phase 1 scope only. Do NOT add Phase 2+ tables here (e.g. no
-- blackout_calendar (OQ-03), no dedicated backtest run-card tables) —
-- those land with their owning phase's migration.
--
-- Apply with:
--   psql "$DATABASE_URL" -f schema.sql
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Extensions
-- -----------------------------------------------------------------------------
-- pgvector: NOTE the extension is named `vector`, not `pgvector` — a common
-- copy-paste mistake (AD-08, database-schema-expert notes).
--
-- Guarded via DO block, NOT a bare `CREATE EXTENSION IF NOT EXISTS vector;`.
-- Reviewer finding: `psql -f` does not stop on error by default (no
-- `ON_ERROR_STOP`), so an unguarded `CREATE EXTENSION vector` on a plain
-- Postgres without the extension binary would error, print to scrollback,
-- and keep going — silently dropping the ENTIRE `articles` table and every
-- index on it (confirmed empirically against a real Postgres 16 with no
-- vector extension installed: exit code 0, `articles` simply missing). Since
-- `articles` is required Phase 1 infrastructure (FR-01/FR-03/FR-04), not an
-- optional forward-compat feature, this must degrade gracefully rather than
-- silently corrupt the schema. The `embedding` column/index (Phase 2,
-- semantic dedup) are added conditionally further below, only if this
-- extension actually installs.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
    ) THEN
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS vector';
    ELSE
        RAISE NOTICE 'vector extension not available — articles.embedding and its ivfflat index will be skipped (Phase 2 semantic dedup will be unavailable until pgvector is installed)';
    END IF;
END
$$;

-- TimescaleDB: required for hypertables (events.ts) and compression policies
-- (OQ-06). Installed via the timescale/timescaledb-ha Docker image per
-- CLAUDE.md §6 Infrastructure. Guarded below with pg_extension checks so this
-- script does not hard-fail in environments where TimescaleDB isn't present
-- yet (e.g. plain PostgreSQL during early local development).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'
    ) THEN
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS timescaledb';
    ELSE
        RAISE NOTICE 'timescaledb extension not available — hypertable/compression setup below will be skipped';
    END IF;
END
$$;


-- -----------------------------------------------------------------------------
-- assets — symbol registry (class, sector, exchange, tags)
-- -----------------------------------------------------------------------------
-- Rationale: AD-01 requires every Event to carry `asset` + `asset_class`;
-- this table is the canonical registry those fields are validated/enriched
-- against (sector/exchange/tags support dashboard filtering — FR-50, FR-52).
CREATE TABLE IF NOT EXISTS assets (
    id            BIGSERIAL PRIMARY KEY,
    symbol        TEXT NOT NULL,
    asset_class   TEXT NOT NULL,              -- e.g. 'equity', 'crypto', 'forex', 'future'
    sector        TEXT,
    exchange      TEXT,
    tags          TEXT[] NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, asset_class)
);

CREATE INDEX IF NOT EXISTS idx_assets_symbol ON assets (symbol);
CREATE INDEX IF NOT EXISTS idx_assets_asset_class ON assets (asset_class);
CREATE INDEX IF NOT EXISTS idx_assets_tags ON assets USING GIN (tags);


-- -----------------------------------------------------------------------------
-- articles — raw ingested news (FR-01, FR-03, FR-04)
-- -----------------------------------------------------------------------------
-- Rationale: FR-03 requires MD5 fingerprint dedup; FR-04 requires per-article
-- ticker/sentiment/topic/urgency extraction results to be persisted.
--
-- NOTE: `embedding vector(384)` is intentionally NOT in this CREATE TABLE.
-- It's added conditionally below (only if the `vector` extension actually
-- installed) so that table creation for `articles` — required Phase 1
-- infrastructure — never depends on pgvector being present. See the
-- Extensions section above for why an unconditional `vector` column here
-- was a real bug (silently dropped the whole table on plain Postgres).
CREATE TABLE IF NOT EXISTS articles (
    id              BIGSERIAL PRIMARY KEY,
    fingerprint     TEXT NOT NULL,               -- MD5 hash for exact-dedup (FR-03)
    source          TEXT NOT NULL,
    url             TEXT,
    headline        TEXT NOT NULL,
    body            TEXT,
    published_at    TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    tickers         TEXT[] NOT NULL DEFAULT '{}',   -- extracted tickers (FR-04)
    asset_class     TEXT,
    sentiment       DOUBLE PRECISION,               -- normalized sentiment score
    topic           TEXT,
    urgency         DOUBLE PRECISION,
    raw_payload     JSONB,                           -- NFR-03: store raw payload for audit/replay
    UNIQUE (fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_tickers ON articles USING GIN (tickers);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles (source);

-- `embedding vector(384)` for forward-compatibility with the Phase 2
-- semantic-dedup swap (AD-06: sentence-transformers/all-MiniLM-L6-v2,
-- 384-dim output) — the column exists once pgvector is installed, but Phase
-- 1 application code does not populate or query it yet. Added + indexed only
-- if the `vector` extension actually installed above (pg_extension, not just
-- pg_available_extensions — we want "did it install," not "could it").
-- ivfflat requires the table to have rows to build an efficient index in
-- production; safe to create against an empty table too. vector_cosine_ops
-- per AD-08 / database-schema-expert guidance.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        EXECUTE 'ALTER TABLE articles ADD COLUMN IF NOT EXISTS embedding vector(384)';
        EXECUTE '
            CREATE INDEX IF NOT EXISTS idx_articles_embedding_ivfflat
                ON articles USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
        ';
    ELSE
        RAISE NOTICE 'vector extension not installed — articles.embedding column and its ivfflat index were skipped';
    END IF;
END
$$;


-- -----------------------------------------------------------------------------
-- events — canonical event stream (AD-01, AD-08). THIS IS THE HYPERTABLE.
-- -----------------------------------------------------------------------------
-- Rationale: AD-01 — single canonical Event schema for every signal type.
-- `meta` JSONB carries type-specific fields without schema branching.
-- NFR-03: raw payloads stored for audit/replay; events are INSERT-only in
-- the normal application path (see "Append-only enforcement" section below).
CREATE TABLE IF NOT EXISTS events (
    id            BIGSERIAL,
    asset         TEXT NOT NULL,
    asset_class   TEXT NOT NULL,
    event_type    TEXT NOT NULL,     -- e.g. 'news', 'whale_transfer', 'options_flow', ...
    direction     SMALLINT NOT NULL DEFAULT 0,   -- +1 bullish, -1 bearish, 0 neutral
    confidence    DOUBLE PRECISION NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),   -- hypertable partitioning column
    source        TEXT NOT NULL,
    meta          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, ts)   -- ts must be part of the key for hypertable partitioning
);

-- Composite index for the dominant query shape: "recent events for asset X."
CREATE INDEX IF NOT EXISTS idx_events_asset_ts ON events (asset, ts DESC);

-- GIN index on JSONB meta for `meta @>` containment queries.
CREATE INDEX IF NOT EXISTS idx_events_meta_gin ON events USING GIN (meta);

CREATE INDEX IF NOT EXISTS idx_events_event_type ON events (event_type);

-- Literal form referenced in CLAUDE.md §5 (kept commented, as written there):
-- SELECT create_hypertable('events', 'ts');
--
-- Active, guarded hypertable creation: only runs if the timescaledb
-- extension is actually installed, so this script does not hard-fail against
-- plain PostgreSQL. `if_not_exists => TRUE` makes it safe to re-run.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('events', 'ts', if_not_exists => TRUE);
    ELSE
        RAISE NOTICE 'timescaledb extension not installed — skipping create_hypertable(events)';
    END IF;
END
$$;


-- -----------------------------------------------------------------------------
-- correlation_scores — persisted confluence scores per scoring run
-- -----------------------------------------------------------------------------
-- Rationale: OQ-01 verdict — append-only event store + materialized view,
-- not live-query-only, for correlation scores; Redis is the hot-read cache
-- (AD-07), this table is the durable history. OQ-06 verdict — store ALL
-- scores (not just threshold crossings), with compression for rows older
-- than 7 days. FR-09 requires the full per-signal audit trail
-- (weight/decay/contribution) to be returned with every score — that trail
-- is captured in `weighted_signals` JSONB. FR-10 requires agreement_ratio /
-- conflict_ratio per asset.
CREATE TABLE IF NOT EXISTS correlation_scores (
    id                  BIGSERIAL,
    asset               TEXT NOT NULL,
    confluence_score    DOUBLE PRECISION NOT NULL CHECK (confluence_score >= -1.0 AND confluence_score <= 1.0),
    agreement_ratio     DOUBLE PRECISION,
    conflict_ratio      DOUBLE PRECISION,
    signal_count        INTEGER NOT NULL DEFAULT 0,
    weighted_signals     JSONB NOT NULL DEFAULT '[]'::jsonb,   -- per-signal audit trail (FR-09)
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, computed_at)
);

CREATE INDEX IF NOT EXISTS idx_correlation_scores_asset_computed_at
    ON correlation_scores (asset, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_correlation_scores_weighted_signals_gin
    ON correlation_scores USING GIN (weighted_signals);

-- Hypertable + compression policy (OQ-06). Guarded the same way as `events`.
-- correlation_scores becomes a hypertable "once write volume justifies it"
-- per database-schema-expert guidance — enabling it now is cheap and keeps
-- the compression policy below meaningful from day one.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('correlation_scores', 'computed_at', if_not_exists => TRUE);
    ELSE
        RAISE NOTICE 'timescaledb extension not installed — skipping create_hypertable(correlation_scores)';
    END IF;
END
$$;

-- Compression policy (OQ-06 verdict): compress correlation_scores chunks
-- older than 7 days, segmented by asset for good compression ratio on
-- repeated-asset scans. This block requires TimescaleDB compression support
-- (not available on plain PostgreSQL, and not on all TimescaleDB tiers), so
-- it is guarded and will simply emit a NOTICE and skip if unavailable rather
-- than aborting the whole script.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        BEGIN
            ALTER TABLE correlation_scores SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'asset'
            );
            PERFORM add_compression_policy('correlation_scores', INTERVAL '7 days');
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Skipping correlation_scores compression policy: %', SQLERRM;
        END;
    ELSE
        RAISE NOTICE 'timescaledb extension not installed — skipping compression policy on correlation_scores';
    END IF;
END
$$;

-- `events` is also a likely future compression target per database-schema-
-- expert guidance ("and likely events") — left undone in Phase 1 since
-- events is still being actively iterated on; revisit once ingest volume is
-- characterized. Documented here rather than applied, to avoid premature
-- optimization against an unknown access pattern.


-- -----------------------------------------------------------------------------
-- alerts — fired alerts (FR-19 .. FR-22)
-- -----------------------------------------------------------------------------
-- Rationale: FR-22 — persist alerts with severity, delivery channel, and
-- acknowledgement tracking. NFR-03 / design principle 4 — append-only;
-- `acknowledged` is the one legitimate mutable field (ACK tracking, FR-22).
CREATE TABLE IF NOT EXISTS alerts (
    id                  BIGSERIAL PRIMARY KEY,
    asset               TEXT NOT NULL,
    severity            TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    confluence_score    DOUBLE PRECISION,
    signal_count        INTEGER,
    delivery_channel    TEXT[] NOT NULL DEFAULT '{}',   -- e.g. {'slack','telegram','websocket'}
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,   -- full alert payload (FR-21 WS broadcast body)
    acknowledged        BOOLEAN NOT NULL DEFAULT FALSE,       -- the one mutable field (FR-22)
    acknowledged_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alerts_asset_created_at ON alerts (asset, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts (severity);
CREATE INDEX IF NOT EXISTS idx_alerts_acknowledged ON alerts (acknowledged) WHERE acknowledged = FALSE;
CREATE INDEX IF NOT EXISTS idx_alerts_payload_gin ON alerts USING GIN (payload);


-- -----------------------------------------------------------------------------
-- positions — live + paper positions (setup/regime/catalyst tags)
-- -----------------------------------------------------------------------------
-- Rationale: FR-29 — every simulated/live trade tagged by setup, regime,
-- catalyst, venue for backtesting and journal analysis. AD-10/NFR-04 —
-- this table records position state as reported by paper/live execution;
-- it is not written to directly by the correlation/alert layer.
CREATE TABLE IF NOT EXISTS positions (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('long', 'short')),
    quantity        NUMERIC NOT NULL,
    entry_price     NUMERIC NOT NULL,
    entry_time      TIMESTAMPTZ NOT NULL,
    exit_price      NUMERIC,
    exit_time       TIMESTAMPTZ,
    account_mode    TEXT NOT NULL CHECK (account_mode IN ('paper', 'live')),
    setup           TEXT,       -- e.g. 'breakout', 'mean_reversion' (FR-29)
    regime          TEXT,       -- e.g. 'trending', 'chop', 'high_vol' (FR-29)
    catalyst        TEXT,       -- linked catalyst / event description (FR-29)
    venue           TEXT,       -- exchange/broker (FR-29)
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions (symbol);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);
CREATE INDEX IF NOT EXISTS idx_positions_account_mode ON positions (account_mode);


-- -----------------------------------------------------------------------------
-- trade_journal — closed-trade review (R-multiple, setup x regime scoring)
-- -----------------------------------------------------------------------------
-- Rationale: FR-30 — compute expectancy E = (W*R_win) - (L*R_loss) per
-- setup x regime combination; this table is the per-trade record that
-- expectancy rollups are computed FROM. References positions (one journal
-- entry per closed position).
CREATE TABLE IF NOT EXISTS trade_journal (
    id                  BIGSERIAL PRIMARY KEY,
    position_id         BIGINT NOT NULL REFERENCES positions (id),
    symbol              TEXT NOT NULL,
    setup               TEXT,
    regime              TEXT,
    catalyst            TEXT,
    r_multiple          NUMERIC,          -- realized R-multiple for this trade
    win                 BOOLEAN,
    notes               TEXT,
    reviewed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trade_journal_position_id ON trade_journal (position_id);
CREATE INDEX IF NOT EXISTS idx_trade_journal_setup_regime ON trade_journal (setup, regime);


-- -----------------------------------------------------------------------------
-- greeks_snapshots — Delta/Gamma/Theta/Vega/Rho/IV per symbol/expiry/strike
-- -----------------------------------------------------------------------------
-- Rationale: FR-23/FR-24 (Greeks & options panel). Phase 2 will populate
-- this via Tradier/Deribit pollers (see event-bus-expert / ta-engine-expert
-- Phase 2 scope); the table is created now, forward-compatible, so Phase 2
-- work is additive rather than requiring a schema migration alongside code.
-- No Phase 1 application code writes to this table.
CREATE TABLE IF NOT EXISTS greeks_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    expiry          DATE NOT NULL,
    strike          NUMERIC NOT NULL,
    option_type     TEXT CHECK (option_type IN ('call', 'put')),
    delta           DOUBLE PRECISION,
    gamma           DOUBLE PRECISION,
    theta           DOUBLE PRECISION,
    vega            DOUBLE PRECISION,
    rho             DOUBLE PRECISION,
    iv              DOUBLE PRECISION,     -- implied volatility
    iv_rank         DOUBLE PRECISION,
    iv_percentile   DOUBLE PRECISION,
    open_interest   BIGINT,
    volume          BIGINT,
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_greeks_snapshots_symbol_expiry_strike
    ON greeks_snapshots (symbol, expiry, strike);
CREATE INDEX IF NOT EXISTS idx_greeks_snapshots_snapshot_at
    ON greeks_snapshots (snapshot_at DESC);


-- -----------------------------------------------------------------------------
-- onchain_flows — whale transfers, exchange flows, mints/burns
-- -----------------------------------------------------------------------------
-- Rationale: FR-02 lists `whale_transfer` / `on_chain_move` as ingested
-- signal event types; this table is the durable, source-of-truth record
-- those Events are derived from (mirrors NFR-03: raw payloads stored for
-- audit/replay, separate from the scored `events` stream).
CREATE TABLE IF NOT EXISTS onchain_flows (
    id              BIGSERIAL PRIMARY KEY,
    asset           TEXT NOT NULL,
    flow_type       TEXT NOT NULL,   -- e.g. 'whale_transfer', 'exchange_inflow',
                                      -- 'exchange_outflow', 'mint', 'burn'
    amount          NUMERIC NOT NULL,
    amount_usd      NUMERIC,
    from_label      TEXT,            -- e.g. 'Binance', 'unknown wallet' (if available)
    to_label        TEXT,
    tx_hash         TEXT,
    source          TEXT NOT NULL,   -- e.g. 'whale_alert', 'nansen', 'arkham'
    ts              TIMESTAMPTZ NOT NULL,
    raw_payload     JSONB,           -- NFR-03: raw payload retained for audit/replay
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_onchain_flows_asset_ts ON onchain_flows (asset, ts DESC);
CREATE INDEX IF NOT EXISTS idx_onchain_flows_flow_type ON onchain_flows (flow_type);


-- -----------------------------------------------------------------------------
-- feed_health — heartbeat hash per source, staleness audit log
-- -----------------------------------------------------------------------------
-- Rationale: FR-05 — monitor feed freshness, mark sources stale after a
-- configurable TTL. NFR-06 — feed health keys auto-expire after 5 minutes;
-- that TTL enforcement is native to Redis (AD-07) and NOT reproduced here —
-- Postgres has no native per-row TTL. This table is therefore an audit log
-- of heartbeats over time, not the live staleness source of truth; live
-- staleness checks should read Redis (see event-bus-expert). Written on the
-- event-bus-expert side each time a poller heartbeats.
CREATE TABLE IF NOT EXISTS feed_health (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    heartbeat_hash  TEXT NOT NULL,
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'stale', 'down')),
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_feed_health_source_last_seen ON feed_health (source, last_seen DESC);


-- =============================================================================
-- Append-only enforcement (NFR-03, design principle 4)
-- =============================================================================
-- `events` and `alerts` must never be UPDATEd or DELETEd by application code
-- in the normal path — only INSERT. Corrections are new rows referencing the
-- original (e.g. via `meta` or a foreign key), never in-place mutations. The
-- one legitimate exception is `alerts.acknowledged` (+ `acknowledged_at`),
-- which is a status update, not a rewrite of the alert's content (FR-22).
--
-- This is enforced at the DB level via REVOKE, not just convention, at
-- deployment time. The exact application role name is environment-specific
-- (set via DATABASE_URL / infra config — see infrastructure-ops), so it is
-- intentionally NOT hardcoded here. Apply something like the following once
-- the application's least-privilege DB role is provisioned:
--
--   REVOKE UPDATE, DELETE ON events  FROM <app_role>;
--   REVOKE UPDATE, DELETE ON alerts  FROM <app_role>;
--   -- Then grant back only the narrow UPDATE needed for ACK tracking:
--   GRANT UPDATE (acknowledged, acknowledged_at) ON alerts TO <app_role>;
--
-- Coordinate the actual role name and GRANT/REVOKE statements with
-- infrastructure-ops when the role is provisioned (docker-compose /
-- Kubernetes secrets, per CLAUDE.md §7 TODOs).
