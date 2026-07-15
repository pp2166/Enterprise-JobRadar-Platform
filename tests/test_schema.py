"""Schema bootstrap coverage."""

from __future__ import annotations

import pytest

from app.schema import _upgrade_crawl_runs


class _Dialect:
    def __init__(self, name: str):
        self.name = name


class _Conn:
    def __init__(self, dialect_name: str):
        self.dialect = _Dialect(dialect_name)
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))


@pytest.mark.asyncio
async def test_crawl_run_upgrade_skips_sqlite():
    conn = _Conn("sqlite")

    await _upgrade_crawl_runs(conn)

    assert conn.statements == []


@pytest.mark.asyncio
async def test_crawl_run_upgrade_emits_postgresql_idempotent_sql():
    conn = _Conn("postgresql")

    await _upgrade_crawl_runs(conn)

    sql = "\n".join(conn.statements)
    assert "ADD COLUMN IF NOT EXISTS retry_of_run_id INTEGER NULL" in sql
    assert "ADD COLUMN IF NOT EXISTS trigger_type VARCHAR(32) NOT NULL DEFAULT 'api'" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_crawl_runs_retry_of_run_id" in sql
    assert "fk_crawl_runs_retry_of_run_id" in sql
    assert "pg_constraint" in sql
    assert "duplicate_object" in sql
