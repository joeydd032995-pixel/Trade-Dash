"""
Static + (optionally) live tests for schema.sql.

Per CLAUDE.md §5 "Database (SQL)", §4 AD-08, §8 OQ-01/OQ-06, and NFR-03,
schema.sql must define the 9 core Phase 1 tables plus `feed_health`, wire up
pgvector + TimescaleDB, and enforce append-only semantics on `events` /
`alerts`.

A full TimescaleDB + pgvector Postgres is not guaranteed to be reachable in
every environment this test runs in (that end-to-end verification is a later
step in the overall build-out plan). This test is therefore split in two:

1. Static structural checks (always run, never skipped): parse schema.sql
   with sqlparse and assert the expected CREATE TABLE statements, the
   `vector` extension, and `create_hypertable` calls are present.

2. Live apply check (conditionally run): if `DATABASE_URL` is set and a
   Postgres instance is actually reachable, attempt to apply schema.sql via
   asyncpg and confirm all expected tables exist afterward. If unreachable,
   this part is skipped (not failed) via pytest.mark.skipif logic evaluated
   at runtime inside the test.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import sqlparse

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

EXPECTED_TABLES = {
    "assets",
    "articles",
    "events",
    "correlation_scores",
    "alerts",
    "positions",
    "trade_journal",
    "greeks_snapshots",
    "onchain_flows",
    "feed_health",
}


@pytest.fixture(scope="module")
def schema_sql() -> str:
    assert SCHEMA_PATH.exists(), f"schema.sql not found at {SCHEMA_PATH}"
    return SCHEMA_PATH.read_text()


@pytest.fixture(scope="module")
def parsed_statements(schema_sql: str):
    return sqlparse.parse(schema_sql)


def _extract_create_table_names(schema_sql: str) -> set[str]:
    """Regex-based extraction of table names from CREATE TABLE statements.

    Handles `CREATE TABLE IF NOT EXISTS <name>` and `CREATE TABLE <name>`,
    case-insensitively, tolerating the schema's formatting.
    """
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
        re.IGNORECASE,
    )
    return {m.group(1).lower() for m in pattern.finditer(schema_sql)}


class TestStaticSchemaStructure:
    """These checks require no database connection and must always pass."""

    def test_schema_file_is_valid_sql_tokenizable(self, parsed_statements):
        # sqlparse.parse() never raises on malformed SQL (it best-effort
        # tokenizes), so the meaningful assertion is just that we got a
        # non-trivial number of statements back, confirming the file isn't
        # empty/garbage.
        assert len(parsed_statements) > 5

    def test_all_expected_tables_present(self, schema_sql):
        found_tables = _extract_create_table_names(schema_sql)
        missing = EXPECTED_TABLES - found_tables
        assert not missing, f"Missing CREATE TABLE statements for: {missing}"

    def test_no_unexpected_phase2_plus_tables(self, schema_sql):
        """Guard against scope creep: Phase 1 schema.sql should not define
        tables that belong to later phases (e.g. blackout_calendar per
        OQ-03, or dedicated backtest run-card tables)."""
        found_tables = _extract_create_table_names(schema_sql)
        out_of_scope = {"blackout_calendar", "run_cards", "backtest_runs"}
        overlap = found_tables & out_of_scope
        assert not overlap, f"Found out-of-scope Phase 2+ tables: {overlap}"

    def test_vector_extension_referenced_correctly(self, schema_sql):
        assert re.search(
            r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+vector\s*;",
            schema_sql,
            re.IGNORECASE,
        ), "Expected `CREATE EXTENSION IF NOT EXISTS vector;` (not `pgvector`)"
        # Guard against the common copy-paste mistake explicitly.
        assert not re.search(
            r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+pgvector",
            schema_sql,
            re.IGNORECASE,
        ), "Extension must be named `vector`, not `pgvector`"

    def test_embedding_column_is_384_dim_vector(self, schema_sql):
        assert re.search(r"embedding\s+vector\(384\)", schema_sql, re.IGNORECASE), (
            "articles.embedding must be vector(384) to match "
            "sentence-transformers/all-MiniLM-L6-v2 output dimension"
        )

    def test_embedding_vector_index_present(self, schema_sql):
        assert re.search(
            r"USING\s+ivfflat\s*\(\s*embedding\s+vector_cosine_ops\s*\)",
            schema_sql,
            re.IGNORECASE,
        ) or re.search(
            r"USING\s+hnsw\s*\(\s*embedding\s+vector_cosine_ops\s*\)",
            schema_sql,
            re.IGNORECASE,
        ), "Expected an ivfflat or hnsw index on articles.embedding using vector_cosine_ops"

    def test_create_hypertable_referenced_for_events(self, schema_sql):
        assert re.search(
            r"create_hypertable\s*\(\s*'events'\s*,\s*'ts'",
            schema_sql,
            re.IGNORECASE,
        ), "Expected a create_hypertable('events', 'ts', ...) call"

    def test_create_hypertable_guarded_against_missing_extension(self, schema_sql):
        """The active create_hypertable call for events must be guarded so
        this script doesn't hard-fail when timescaledb isn't installed."""
        assert "pg_extension" in schema_sql
        assert "timescaledb" in schema_sql.lower()

    def test_correlation_scores_compression_policy_present(self, schema_sql):
        assert re.search(
            r"add_compression_policy\s*\(\s*'correlation_scores'\s*,\s*INTERVAL\s*'7 days'\s*\)",
            schema_sql,
            re.IGNORECASE,
        ), "Expected add_compression_policy('correlation_scores', INTERVAL '7 days')"
        assert re.search(
            r"timescaledb\.compress_segmentby\s*=\s*'asset'", schema_sql, re.IGNORECASE
        ), "Expected compress_segmentby = 'asset' for correlation_scores compression"

    def test_events_meta_gin_index_present(self, schema_sql):
        assert re.search(
            r"USING\s+GIN\s*\(\s*meta\s*\)", schema_sql, re.IGNORECASE
        ), "Expected a GIN index on events.meta"

    def test_events_asset_ts_composite_index_present(self, schema_sql):
        assert re.search(
            r"events\s*\(\s*asset\s*,\s*ts\s+DESC\s*\)", schema_sql, re.IGNORECASE
        ), "Expected a composite index on events (asset, ts DESC)"

    def test_alerts_acknowledged_column_present(self, schema_sql):
        assert re.search(
            r"acknowledged\s+BOOLEAN", schema_sql, re.IGNORECASE
        ), "alerts.acknowledged must exist as the one mutable field (FR-22)"

    def test_append_only_documented_for_events_and_alerts(self, schema_sql):
        """NFR-03 / design principle 4: append-only enforcement must at
        least be documented (REVOKE UPDATE, DELETE pattern), even though the
        exact application role name is deployment-specific."""
        assert re.search(r"REVOKE\s+UPDATE,\s*DELETE", schema_sql, re.IGNORECASE)
        assert "append-only" in schema_sql.lower() or "append only" in schema_sql.lower()

    def test_positions_has_setup_regime_catalyst_tags(self, schema_sql):
        for col in ("setup", "regime", "catalyst"):
            assert re.search(rf"\b{col}\s+TEXT", schema_sql, re.IGNORECASE), (
                f"positions must include a `{col}` column (FR-29)"
            )

    def test_trade_journal_has_r_multiple_and_references_positions(self, schema_sql):
        assert re.search(r"r_multiple\s+NUMERIC", schema_sql, re.IGNORECASE)
        assert re.search(
            r"REFERENCES\s+positions\s*\(\s*id\s*\)", schema_sql, re.IGNORECASE
        ), "trade_journal must reference positions"

    def test_greeks_snapshots_has_all_greeks(self, schema_sql):
        for col in ("delta", "gamma", "theta", "vega", "rho", "iv"):
            assert re.search(rf"\b{col}\b", schema_sql, re.IGNORECASE), (
                f"greeks_snapshots must include `{col}`"
            )


# -----------------------------------------------------------------------------
# Live apply check — only runs if DATABASE_URL is set AND reachable.
# -----------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")


def _can_connect(dsn: str) -> bool:
    """Best-effort synchronous reachability probe using asyncpg in a throwaway
    event loop, so we can decide whether to skip before entering an async
    test (keeps this test file usable without pytest-asyncio)."""
    import asyncio

    async def _probe():
        import asyncpg

        conn = await asyncio.wait_for(asyncpg.connect(dsn=dsn), timeout=3)
        await conn.close()

    try:
        asyncio.run(_probe())
        return True
    except Exception:
        return False


_live_db_available = bool(DATABASE_URL) and _can_connect(DATABASE_URL)


@pytest.mark.skipif(
    not _live_db_available,
    reason="DATABASE_URL not set or Postgres not reachable — skipping live schema apply check",
)
class TestLiveSchemaApply:
    def test_apply_schema_and_verify_tables(self, schema_sql):
        import asyncio

        async def _run():
            import asyncpg

            conn = await asyncpg.connect(dsn=DATABASE_URL)
            try:
                # `CREATE EXTENSION IF NOT EXISTS vector` requires the
                # extension binary to actually be installed on the Postgres
                # server (unlike TimescaleDB-dependent statements further
                # down, which are self-guarded inside DO blocks in
                # schema.sql). A reachable Postgres that isn't running the
                # project's target image (timescale/timescaledb-ha with
                # pgvector — see CLAUDE.md §6 Infrastructure) is a valid,
                # expected environment gap here, not a schema defect — skip
                # rather than fail in that case.
                try:
                    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                except Exception as exc:
                    pytest.skip(
                        f"pgvector extension not installed on reachable Postgres — "
                        f"skipping live apply check ({exc})"
                    )

                await conn.execute(schema_sql)
                rows = await conn.fetch(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                    """
                )
                found = {r["table_name"] for r in rows}
                missing = EXPECTED_TABLES - found
                assert not missing, f"Tables missing after live apply: {missing}"
            finally:
                await conn.close()

        asyncio.run(_run())
