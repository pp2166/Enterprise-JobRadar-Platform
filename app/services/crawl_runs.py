"""Crawl run persistence operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CrawlRun


ACTIVE_CRAWL_RUN_STATUSES = (
    "queued",
    "running",
    "retrying",
)

STALE_CRAWL_RUN_STATUSES = (
    "queued",
    "running",
    "retrying",
)


@dataclass(frozen=True)
class CrawlRunPage:
    total: int
    records: list[CrawlRun]


class CrawlRunNotFoundError(LookupError):
    """Raised when a crawl run does not exist."""


class CrawlRunNotRetryableError(ValueError):
    def __init__(self, *, run_id: int, status: str) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(f"crawl run is not retryable: {run_id}")


class ActiveCrawlRunExistsError(ValueError):
    def __init__(self, *, source: str, active_run_id: int) -> None:
        self.source = source
        self.active_run_id = active_run_id
        super().__init__(f"active crawl run exists for source: {source}")


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


async def create_retry_crawl_run(
    session: AsyncSession,
    *,
    run_id: int,
    celery_task_id: str,
) -> CrawlRun:
    parent = await _get_crawl_run(session, run_id)

    if parent.status != "failed":
        raise CrawlRunNotRetryableError(run_id=parent.id, status=parent.status)

    return await create_crawl_run_if_inactive(
        session,
        source=parent.source,
        celery_task_id=celery_task_id,
        trigger_type="manual",
        retry_of_run_id=parent.id,
    )


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


async def create_crawl_run_if_inactive(
    session: AsyncSession,
    *,
    source: str,
    celery_task_id: str,
    trigger_type: str = "api",
    retry_of_run_id: int | None = None,
) -> CrawlRun:
    active_run = await find_active_crawl_run(session, source=source)

    if active_run is not None:
        raise ActiveCrawlRunExistsError(
            source=source,
            active_run_id=active_run.id,
        )

    return await create_crawl_run(
        session,
        source=source,
        celery_task_id=celery_task_id,
        trigger_type=trigger_type,
        retry_of_run_id=retry_of_run_id,
    )


async def find_crawl_run_by_task_id(
    session: AsyncSession,
    *,
    celery_task_id: str,
) -> CrawlRun | None:
    stmt = select(CrawlRun).where(CrawlRun.celery_task_id == celery_task_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_active_crawl_run(
    session: AsyncSession,
    *,
    source: str,
) -> CrawlRun | None:
    stmt = (
        select(CrawlRun)
        .where(
            CrawlRun.source == source,
            CrawlRun.status.in_(ACTIVE_CRAWL_RUN_STATUSES),
        )
        .order_by(CrawlRun.created_at.desc(), CrawlRun.id.desc())
        .limit(1)
    )
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


async def recover_stale_crawl_runs(
    session: AsyncSession,
    *,
    stale_before: datetime,
    recovered_at: datetime,
    error_message: str,
) -> list[int]:
    stale_condition = or_(
        and_(
            CrawlRun.status == "queued",
            CrawlRun.created_at < stale_before,
        ),
        and_(
            CrawlRun.status.in_(("running", "retrying")),
            func.coalesce(
                CrawlRun.started_at,
                CrawlRun.created_at,
            )
            < stale_before,
        ),
    )
    stmt = (
        update(CrawlRun)
        .where(stale_condition)
        .values(
            status="failed",
            error_message=error_message,
            finished_at=recovered_at,
        )
        .returning(CrawlRun.id)
        .execution_options(synchronize_session=False)
    )

    recovered_ids = (await session.scalars(stmt)).all()
    await session.commit()

    return sorted(recovered_ids)


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
