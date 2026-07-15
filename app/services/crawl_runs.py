"""Crawl run persistence operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CrawlRun


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