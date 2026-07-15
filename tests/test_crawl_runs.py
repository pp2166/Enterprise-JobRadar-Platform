"""CrawlRun model and service persistence tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import CrawlRun
from app.services.crawl_runs import (
    CrawlRunNotFoundError,
    create_crawl_run,
    mark_crawl_run_running,
)


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


@pytest.mark.asyncio
async def test_create_crawl_run_persists_queued_record(session):
    run = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-service-create-001",
    )

    assert run.id is not None
    assert run.source == "remoteok"
    assert run.status == "queued"
    assert run.celery_task_id == "task-service-create-001"
    assert run.attempt_count == 0
    assert run.created_at is not None

    stored = (
        await session.execute(
            select(CrawlRun).where(CrawlRun.id == run.id)
        )
    ).scalar_one()

    assert stored.source == "remoteok"
    assert stored.status == "queued"
    assert stored.celery_task_id == "task-service-create-001"


@pytest.mark.asyncio
async def test_mark_crawl_run_running_updates_state(session):
    run = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-running-001",
    )

    running = await mark_crawl_run_running(
        session,
        run_id=run.id,
    )

    assert running.status == "running"
    assert running.attempt_count == 1
    assert running.started_at is not None
    assert running.finished_at is None

    stored = await session.get(CrawlRun, run.id)

    assert stored is not None
    assert stored.status == "running"
    assert stored.attempt_count == 1
    assert stored.started_at is not None


@pytest.mark.asyncio
async def test_mark_crawl_run_running_rejects_missing_run(session):
    with pytest.raises(
        CrawlRunNotFoundError,
        match="crawl run not found: 999999",
    ):
        await mark_crawl_run_running(
            session,
            run_id=999999,
        )