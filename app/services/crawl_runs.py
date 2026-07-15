"""Crawl run persistence operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CrawlRun


@dataclass(frozen=True)
class CrawlRunPage:
    total: int
    records: list[CrawlRun]


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


async def get_crawl_run(
    session: AsyncSession,
    *,
    run_id: int,
) -> CrawlRun:
    return await _get_crawl_run(session, run_id)


async def create_crawl_run(
    session: AsyncSession,
    *,
    source: str,
    celery_task_id: str,
    trigger_type: str = "api",
    retry_of_run_id: int | None = None,
) -> CrawlRun:
    run = CrawlRun(
        source=source,
        status="queued",
        celery_task_id=celery_task_id,
        trigger_type=trigger_type,
        retry_of_run_id=retry_of_run_id,
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


async def list_crawl_runs(
    session: AsyncSession,
    *,
    source: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> CrawlRunPage:
    filters = []
    if source is not None:
        filters.append(CrawlRun.source == source)
    if status is not None:
        filters.append(CrawlRun.status == status)

    total_stmt = select(func.count()).select_from(CrawlRun)
    records_stmt = (
        select(CrawlRun)
        .order_by(CrawlRun.created_at.desc(), CrawlRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    if filters:
        total_stmt = total_stmt.where(*filters)
        records_stmt = records_stmt.where(*filters)

    total = await session.scalar(total_stmt)
    records = (await session.scalars(records_stmt)).all()

    return CrawlRunPage(
        total=total or 0,
        records=list(records),
    )


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
