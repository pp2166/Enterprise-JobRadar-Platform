"""End-to-end ingest + search test against an in-memory SQLite DB.

Postgres-specific bits (tsvector, ts_rank_cd, generated column, on_conflict)
don't work on SQLite, so this test exercises the non-Postgres paths:
    - ingest into a fresh DB, including (source, source_id) upsert semantics
    - SimHash-based dedup of near-duplicates across sources
    - filter-only search (q=None), which uses recency ordering only

For the full tsvector-based ranking path, run the integration stack with
docker compose and query /search.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Job
from app.services.ingest import ingest_jobs
from app.services.normalize import NormalizedJob
from app.services.search import SearchFilters, search_jobs


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Job.__table__.create(c, checkfirst=True))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


def _job(
    source: str, sid: str, title: str, company: str, desc: str, posted_at=None
) -> NormalizedJob:
    return NormalizedJob(
        source=source,
        source_id=sid,
        url=f"https://example.com/{source}/{sid}",
        title=title,
        company=company,
        description=desc,
        location="Remote",
        remote=True,
        posted_at=posted_at or datetime.now(timezone.utc),
    )


async def test_ingest_inserts_then_upserts(session: AsyncSession):
    j1 = _job("remoteok", "A1", "Senior Go Engineer", "Acme", "go kubernetes backend")
    stats = await ingest_jobs(session, [j1])
    assert stats.inserted == 1 and stats.duplicates == 0

    # Same (source, source_id) with updated title — should upsert, not duplicate.
    j1_updated = _job("remoteok", "A1", "Staff Go Engineer", "Acme", "go kubernetes backend")
    stats = await ingest_jobs(session, [j1_updated])
    assert stats.updated == 1

    rows = (await session.execute(text("select count(*) from jobs"))).scalar_one()
    assert rows == 1


async def test_simhash_dedups_across_sources(session: AsyncSession):
    j1 = _job(
        "remoteok",
        "A1",
        "Senior Python Engineer",
        "Acme",
        "Build async backends with fastapi, postgres, and redis at scale.",
    )
    j2 = _job(
        "weworkremotely",
        "B1",
        "Senior Python Engineer",
        "Acme",
        "Build async backends with fastapi, postgres, and redis at scale!",
    )
    stats = await ingest_jobs(session, [j1])
    assert stats.inserted == 1
    stats = await ingest_jobs(session, [j2])
    assert stats.duplicates == 1
    assert stats.inserted == 0


async def test_search_filters_and_recency(session: AsyncSession):
    now = datetime.now(timezone.utc)
    await ingest_jobs(
        session,
        [
            _job(
                "remoteok",
                "1",
                "Rust Engineer",
                "Foo",
                "systems rust",
                posted_at=now - timedelta(days=30),
            ),
            _job(
                "remoteok",
                "2",
                "Python Engineer",
                "Bar",
                "backend python",
                posted_at=now - timedelta(days=1),
            ),
            _job(
                "remoteok",
                "3",
                "Frontend Engineer",
                "Baz",
                "react ui",
                posted_at=now - timedelta(hours=1),
            ),
        ],
    )

    total, jobs = await search_jobs(session, SearchFilters(page_size=10))
    assert total == 3
    # Ordered by recency desc — newest first.
    assert [j.title for j in jobs] == ["Frontend Engineer", "Python Engineer", "Rust Engineer"]

    # Filter by company substring.
    _, jobs = await search_jobs(session, SearchFilters(company="bar"))
    assert len(jobs) == 1 and jobs[0].company == "Bar"

    # Filter by source.
    total, _ = await search_jobs(session, SearchFilters(source="weworkremotely"))
    assert total == 0
