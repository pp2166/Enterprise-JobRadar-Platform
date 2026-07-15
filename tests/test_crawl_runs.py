"""CrawlRun model and service persistence tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import CrawlRun
from app.services.crawl_runs import (
    CrawlRunNotFoundError,
    create_crawl_run,
    find_crawl_run_by_task_id,
    get_crawl_run,
    list_crawl_runs,
    mark_crawl_run_failed,
    mark_crawl_run_retrying,
    mark_crawl_run_running,
    mark_crawl_run_succeeded,
)


async def _create_list_run(
    session,
    *,
    source: str,
    status: str,
    celery_task_id: str,
    created_at: datetime,
) -> CrawlRun:
    run = CrawlRun(
        source=source,
        status=status,
        celery_task_id=celery_task_id,
        created_at=created_at,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


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
    assert run.retry_of_run_id is None
    assert run.trigger_type == "api"
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
    assert run.trigger_type == "api"
    assert run.retry_of_run_id is None
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
    assert stored.trigger_type == "api"
    assert stored.retry_of_run_id is None


@pytest.mark.asyncio
async def test_create_crawl_run_persists_explicit_trigger_type(session):
    run = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-service-direct-001",
        trigger_type="direct",
    )

    assert run.trigger_type == "direct"

    stored = await session.get(CrawlRun, run.id)

    assert stored is not None
    assert stored.trigger_type == "direct"


@pytest.mark.asyncio
async def test_create_crawl_run_persists_retry_parent(session):
    parent = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-service-parent-001",
    )

    child = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-service-child-001",
        trigger_type="manual",
        retry_of_run_id=parent.id,
    )

    assert child.retry_of_run_id == parent.id
    assert child.trigger_type == "manual"

    await session.refresh(parent)
    assert parent.retry_of_run_id is None
    assert parent.status == "queued"


def test_crawl_run_metadata_includes_failure_management_fields():
    table = CrawlRun.__table__

    assert "retry_of_run_id" in table.c
    assert "trigger_type" in table.c

    fk = next(
        constraint
        for constraint in table.foreign_key_constraints
        if constraint.name == "fk_crawl_runs_retry_of_run_id"
    )
    assert fk.ondelete == "NO ACTION"
    assert list(fk.elements)[0].target_fullname == "crawl_runs.id"

    assert "ix_crawl_runs_retry_of_run_id" in {index.name for index in table.indexes}


@pytest.mark.asyncio
async def test_find_crawl_run_by_task_id_returns_record(session):
    run = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-find-001",
    )

    found = await find_crawl_run_by_task_id(
        session,
        celery_task_id="task-find-001",
    )

    assert found is not None
    assert found.id == run.id
    assert found.celery_task_id == "task-find-001"


@pytest.mark.asyncio
async def test_find_crawl_run_by_task_id_returns_none_for_missing_task(session):
    found = await find_crawl_run_by_task_id(
        session,
        celery_task_id="task-missing-001",
    )

    assert found is None


@pytest.mark.asyncio
async def test_get_crawl_run_returns_record_by_id(session):
    run = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-get-001",
    )

    found = await get_crawl_run(
        session,
        run_id=run.id,
    )

    assert found.id == run.id
    assert found.celery_task_id == "task-get-001"


@pytest.mark.asyncio
async def test_get_crawl_run_rejects_missing_run(session):
    with pytest.raises(
        CrawlRunNotFoundError,
        match="crawl run not found: 999999",
    ):
        await get_crawl_run(
            session,
            run_id=999999,
        )


@pytest.mark.asyncio
async def test_list_crawl_runs_orders_by_created_at_then_id_desc(session):
    older = await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-list-order-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer_first = await _create_list_run(
        session,
        source="remoteok",
        status="running",
        celery_task_id="task-list-order-002",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    newer_second = await _create_list_run(
        session,
        source="weworkremotely",
        status="succeeded",
        celery_task_id="task-list-order-003",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    page = await list_crawl_runs(session)

    assert page.total == 3
    assert [run.id for run in page.records] == [
        newer_second.id,
        newer_first.id,
        older.id,
    ]


@pytest.mark.asyncio
async def test_list_crawl_runs_filters_by_source(session):
    remoteok = await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-list-source-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="weworkremotely",
        status="queued",
        celery_task_id="task-list-source-002",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    page = await list_crawl_runs(session, source="remoteok")

    assert page.total == 1
    assert [run.id for run in page.records] == [remoteok.id]


@pytest.mark.asyncio
async def test_list_crawl_runs_filters_by_status(session):
    succeeded = await _create_list_run(
        session,
        source="remoteok",
        status="succeeded",
        celery_task_id="task-list-status-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="remoteok",
        status="failed",
        celery_task_id="task-list-status-002",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    page = await list_crawl_runs(session, status="succeeded")

    assert page.total == 1
    assert [run.id for run in page.records] == [succeeded.id]


@pytest.mark.asyncio
async def test_list_crawl_runs_filters_by_source_and_status(session):
    matching = await _create_list_run(
        session,
        source="remoteok",
        status="failed",
        celery_task_id="task-list-combined-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="remoteok",
        status="succeeded",
        celery_task_id="task-list-combined-002",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="weworkremotely",
        status="failed",
        celery_task_id="task-list-combined-003",
        created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )

    page = await list_crawl_runs(
        session,
        source="remoteok",
        status="failed",
    )

    assert page.total == 1
    assert [run.id for run in page.records] == [matching.id]


@pytest.mark.asyncio
async def test_list_crawl_runs_paginates_records(session):
    oldest = await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-list-page-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-list-page-002",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-list-page-003",
        created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )

    page = await list_crawl_runs(session, page=2, page_size=2)

    assert page.total == 3
    assert [run.id for run in page.records] == [oldest.id]


@pytest.mark.asyncio
async def test_list_crawl_runs_total_is_filtered_count(session):
    await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-list-total-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="remoteok",
        status="failed",
        celery_task_id="task-list-total-002",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="weworkremotely",
        status="queued",
        celery_task_id="task-list-total-003",
        created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )

    page = await list_crawl_runs(
        session,
        source="remoteok",
        page=1,
        page_size=1,
    )

    assert page.total == 2
    assert len(page.records) == 1


@pytest.mark.asyncio
async def test_list_crawl_runs_returns_empty_page(session):
    page = await list_crawl_runs(session, source="not-real")

    assert page.total == 0
    assert page.records == []


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
