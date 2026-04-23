"""Shared pytest fixtures.

The whole suite is SQLite-only (aiosqlite, in-memory). Postgres-specific
features — tsvector ranking, the jobs_search_vector trigger, full-text
ranking — are exercised by the docker-compose integration stack, not here.
These fixtures deliberately avoid app.schema.init_schema() because it runs
plpgsql that SQLite can't parse.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Job
from app.services.normalize import NormalizedJob


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def sqlite_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Job.__table__.create(c, checkfirst=True))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(sqlite_engine) -> AsyncIterator[AsyncSession]:
    Session = async_sessionmaker(sqlite_engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s


@pytest.fixture
def make_job():
    """Factory for NormalizedJob records used across ingest/search tests.

    Each call defaults to a **unique** title/description (derived from source_id)
    so rows are not collapsed by the SimHash deduper unless the test explicitly
    passes identical values.
    """
    def _make(
        source: str = "remoteok",
        source_id: str = "1",
        title: str | None = None,
        company: str | None = None,
        description: str | None = None,
        *,
        url: str | None = None,
        location: str | None = "Remote",
        remote: bool | None = True,
        experience_level: str | None = None,
        salary_min: int | None = None,
        salary_max: int | None = None,
        salary_currency: str | None = None,
        tags: list[str] | None = None,
        posted_at: datetime | None = None,
    ) -> NormalizedJob:
        return NormalizedJob(
            source=source,
            source_id=source_id,
            url=url or f"https://example.com/{source}/{source_id}",
            title=title if title is not None else f"Job {source_id} Title",
            company=company if company is not None else f"Company {source_id}",
            description=description if description is not None
                else f"unique body {source_id} — role {source_id}",
            location=location,
            remote=remote,
            experience_level=experience_level,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=salary_currency,
            tags=tags or [],
            posted_at=posted_at or datetime.now(timezone.utc),
        )
    return _make


@pytest.fixture
def recent_timestamps():
    """Timestamps from most to least recent for ordering tests."""
    now = datetime.now(timezone.utc)
    return {
        "now": now,
        "hour_ago": now - timedelta(hours=1),
        "day_ago": now - timedelta(days=1),
        "week_ago": now - timedelta(days=7),
        "month_ago": now - timedelta(days=30),
    }
