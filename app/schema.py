"""Idempotent DDL bootstrap.

Keeps the project free of Alembic for MVP — `init_schema()` creates tables and
installs the tsvector trigger. Run once at API and worker startup.
"""

from __future__ import annotations

from sqlalchemy import text

from app.database import engine
from app.models import Base  # noqa: F401  (ensures models register on Base)


TSV_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION jobs_search_vector_update() RETURNS trigger AS $$
BEGIN
  NEW.search_vector :=
    setweight(to_tsvector('english', coalesce(NEW.title, '')),       'A') ||
    setweight(to_tsvector('english', coalesce(NEW.company, '')),     'B') ||
    setweight(to_tsvector('english', coalesce(NEW.tags, '')),        'B') ||
    setweight(to_tsvector('english', coalesce(NEW.location, '')),    'C') ||
    setweight(to_tsvector('english', coalesce(NEW.description, '')), 'D');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

DROP_TSV_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS jobs_search_vector_trg ON jobs;
"""

CREATE_TSV_TRIGGER_SQL = """
CREATE TRIGGER jobs_search_vector_trg
  BEFORE INSERT OR UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION jobs_search_vector_update();
"""

ADD_CRAWL_RUN_RETRY_OF_RUN_ID_SQL = """
ALTER TABLE crawl_runs
ADD COLUMN IF NOT EXISTS retry_of_run_id INTEGER NULL;
"""

ADD_CRAWL_RUN_TRIGGER_TYPE_SQL = """
ALTER TABLE crawl_runs
ADD COLUMN IF NOT EXISTS trigger_type VARCHAR(32) NOT NULL DEFAULT 'api';
"""

ADD_CRAWL_RUN_RETRY_OF_RUN_ID_FK_SQL = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'fk_crawl_runs_retry_of_run_id'
      AND conrelid = 'crawl_runs'::regclass
  ) THEN
    BEGIN
      ALTER TABLE crawl_runs
      ADD CONSTRAINT fk_crawl_runs_retry_of_run_id
      FOREIGN KEY (retry_of_run_id)
      REFERENCES crawl_runs(id)
      ON DELETE NO ACTION;
    EXCEPTION
      WHEN duplicate_object THEN NULL;
    END;
  END IF;
END $$;
"""

CREATE_CRAWL_RUN_RETRY_OF_RUN_ID_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_crawl_runs_retry_of_run_id
ON crawl_runs (retry_of_run_id);
"""


async def _upgrade_crawl_runs(conn) -> None:
    if conn.dialect.name != "postgresql":
        return

    await conn.execute(text(ADD_CRAWL_RUN_RETRY_OF_RUN_ID_SQL))
    await conn.execute(text(ADD_CRAWL_RUN_TRIGGER_TYPE_SQL))
    await conn.execute(text(ADD_CRAWL_RUN_RETRY_OF_RUN_ID_FK_SQL))
    await conn.execute(text(CREATE_CRAWL_RUN_RETRY_OF_RUN_ID_INDEX_SQL))


async def init_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _upgrade_crawl_runs(conn)
        await conn.execute(text(TSV_FUNCTION_SQL))
        await conn.execute(text(DROP_TSV_TRIGGER_SQL))
        await conn.execute(text(CREATE_TSV_TRIGGER_SQL))
