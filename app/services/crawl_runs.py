"""Crawl run persistence operations."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CrawlRun


class CrawlRunNotFoundError(LookupError):
    """Raised when a crawl run does not exist."""


async def _get_crawl_run(
    session: AsyncSession,
    run_id: int,
) -> CrawlRun:
    run = await session.get(CrawlRun, run_id)

    if run is None:
        raise CrawlRunNotFoundError(f"crawl run not found: {run_id}")

    return run


async def create_crawl_run(
    session: AsyncSession,
    *,
    source: str,
    celery_task_id: str,
) -> CrawlRun:
    run = CrawlRun(
        source=source,
        status="queued",
        celery_task_id=celery_task_id,
    )

    session.add(run)
    await session.commit()
    await session.refresh(run)

    return run


async def find_crawl_run_by_task_id(
    session: AsyncSession,
    *,
    celery_task_id: str,
) -> CrawlRun | None:
    stmt = select(CrawlRun).where(CrawlRun.celery_task_id == celery_task_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_crawl_run_running(
    session: AsyncSession,
    *,
    run_id: int,
) -> CrawlRun:
    run = await _get_crawl_run(session, run_id)

    run.status = "running"
    run.attempt_count += 1

    if run.started_at is None:
        run.started_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(run)

    return run


async def mark_crawl_run_succeeded(
    session: AsyncSession,
    *,
    run_id: int,
    received: int,
    inserted: int,
    updated: int,
    duplicates: int,
) -> CrawlRun:
    run = await _get_crawl_run(session, run_id)

    run.status = "succeeded"
    run.received = received
    run.inserted = inserted
    run.updated = updated
    run.duplicates = duplicates
    run.error_message = None
    run.finished_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(run)

    return run


async def mark_crawl_run_retrying(
    session: AsyncSession,
    *,
    run_id: int,
    error_message: str,
) -> CrawlRun:
    run = await _get_crawl_run(session, run_id)

    run.status = "retrying"
    run.error_message = error_message
    run.finished_at = None

    await session.commit()
    await session.refresh(run)

    return run


async def mark_crawl_run_failed(
    session: AsyncSession,
    *,
    run_id: int,
    error_message: str,
) -> CrawlRun:
    run = await _get_crawl_run(session, run_id)

    run.status = "failed"
    run.error_message = error_message
    run.finished_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(run)

    return run
