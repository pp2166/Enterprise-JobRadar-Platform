"""CrawlRun model and service persistence tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import CrawlRun
from app.services.crawl_runs import (
    CrawlRunNotFoundError,
    create_crawl_run,
    mark_crawl_run_failed,
    mark_crawl_run_retrying,
    mark_crawl_run_running,
    mark_crawl_run_succeeded,
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
    assert stored.started_at is not None
    assert stored.finished_at is not None


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


@pytest.mark.asyncio
async def test_mark_crawl_run_succeeded_persists_stats(session):
    run = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-succeeded-001",
    )
    await mark_crawl_run_running(
        session,
        run_id=run.id,
    )

    succeeded = await mark_crawl_run_succeeded(
        session,
        run_id=run.id,
        received=100,
        inserted=90,
        updated=5,
        duplicates=5,
    )

    assert succeeded.status == "succeeded"
    assert succeeded.attempt_count == 1
    assert succeeded.received == 100
    assert succeeded.inserted == 90
    assert succeeded.updated == 5
    assert succeeded.duplicates == 5
    assert succeeded.error_message is None
    assert succeeded.started_at is not None
    assert succeeded.finished_at is not None

    stored = await session.get(CrawlRun, run.id)

    assert stored is not None
    assert stored.status == "succeeded"
    assert stored.received == 100
    assert stored.inserted == 90
    assert stored.updated == 5
    assert stored.duplicates == 5
    assert stored.finished_at is not None


@pytest.mark.asyncio
async def test_mark_crawl_run_succeeded_rejects_missing_run(session):
    with pytest.raises(
        CrawlRunNotFoundError,
        match="crawl run not found: 999999",
    ):
        await mark_crawl_run_succeeded(
            session,
            run_id=999999,
            received=0,
            inserted=0,
            updated=0,
            duplicates=0,
        )


@pytest.mark.asyncio
async def test_mark_crawl_run_retrying_records_error(session):
    run = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-retrying-001",
    )
    await mark_crawl_run_running(
        session,
        run_id=run.id,
    )

    retrying = await mark_crawl_run_retrying(
        session,
        run_id=run.id,
        error_message="temporary network error",
    )

    assert retrying.status == "retrying"
    assert retrying.attempt_count == 1
    assert retrying.error_message == "temporary network error"
    assert retrying.started_at is not None
    assert retrying.finished_at is None

    original_started_at = retrying.started_at

    running_again = await mark_crawl_run_running(
        session,
        run_id=run.id,
    )

    assert running_again.status == "running"
    assert running_again.attempt_count == 2
    assert running_again.started_at == original_started_at


@pytest.mark.asyncio
async def test_mark_crawl_run_retrying_rejects_missing_run(session):
    with pytest.raises(
        CrawlRunNotFoundError,
        match="crawl run not found: 999999",
    ):
        await mark_crawl_run_retrying(
            session,
            run_id=999999,
            error_message="temporary failure",
        )


@pytest.mark.asyncio
async def test_mark_crawl_run_failed_records_final_error(session):
    run = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-failed-001",
    )
    await mark_crawl_run_running(
        session,
        run_id=run.id,
    )

    failed = await mark_crawl_run_failed(
        session,
        run_id=run.id,
        error_message="maximum retries exceeded",
    )

    assert failed.status == "failed"
    assert failed.attempt_count == 1
    assert failed.error_message == "maximum retries exceeded"
    assert failed.started_at is not None
    assert failed.finished_at is not None

    stored = await session.get(CrawlRun, run.id)

    assert stored is not None
    assert stored.status == "failed"
    assert stored.error_message == "maximum retries exceeded"
    assert stored.finished_at is not None


@pytest.mark.asyncio
async def test_mark_crawl_run_failed_rejects_missing_run(session):
    with pytest.raises(
        CrawlRunNotFoundError,
        match="crawl run not found: 999999",
    ):
        await mark_crawl_run_failed(
            session,
            run_id=999999,
            error_message="final failure",
        )