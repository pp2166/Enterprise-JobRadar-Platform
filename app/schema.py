"""Idempotent DDL bootstrap.

Keeps the project free of Alembic for MVP — `init_schema()` creates tables and
installs the tsvector trigger. Run once at API and worker startup.
"""
from __future__ import annotations

from sqlalchemy import text

from app.database import engine
from app.models import Base  # noqa: F401  (ensures models register on Base)


TSV_TRIGGER_SQL = """
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

DROP TRIGGER IF EXISTS jobs_search_vector_trg ON jobs;
CREATE TRIGGER jobs_search_vector_trg
  BEFORE INSERT OR UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION jobs_search_vector_update();
"""


async def init_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(TSV_TRIGGER_SQL))
