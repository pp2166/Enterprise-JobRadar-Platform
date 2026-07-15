"""CrawlRun model persistence tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import CrawlRun


@pytest.mark.asyncio
async def test_crawl_run_persists_default_values(session):
    run = CrawlRun(
        source="remoteok",
        celery_task_id="task-defaults-001",
    )

    session.add(run)
    await session.commit()
    await session.refresh(run)

    assert run.id is not None
    assert run.source == "remoteok"
    assert run.status == "queued"
    assert run.attempt_count == 0
    assert run.received == 0
    assert run.inserted == 0
    assert run.updated == 0
    assert run.duplicates == 0
    assert run.error_message is None
    assert run.created_at is not None
    assert run.started_at is None
    assert run.finished_at is None


@pytest.mark.asyncio
async def test_crawl_run_persists_success_result(session):
    started_at = datetime.now(timezone.utc)
    finished_at = datetime.now(timezone.utc)

    run = CrawlRun(
        source="remoteok",
        status="succeeded",
        celery_task_id="task-success-001",
        attempt_count=1,
        received=100,
        inserted=90,
        updated=5,
        duplicates=5,
        started_at=started_at,
        finished_at=finished_at,
    )

    session.add(run)
    await session.commit()

    stored = (
        await session.execute(
            select(CrawlRun).where(
                CrawlRun.celery_task_id == "task-success-001"
            )
        )
    ).scalar_one()

    assert stored.status == "succeeded"
    assert stored.attempt_count == 1
    assert stored.received == 100
    assert stored.inserted == 90
    assert stored.updated == 5
    assert stored.duplicates == 5
    assert stored.error_message is None