"""CrawlRun model and service persistence tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import CrawlRun
from app.services.crawl_runs import (
    ACTIVE_CRAWL_RUN_STATUSES,
    ActiveCrawlRunExistsError,
    CrawlRunNotFoundError,
    CrawlRunNotRetryableError,
    create_crawl_run,
    create_crawl_run_if_inactive,
    create_retry_crawl_run,
    find_active_crawl_run,
    find_crawl_run_by_task_id,
    get_crawl_run,
    list_crawl_runs,
    mark_crawl_run_failed,
    mark_crawl_run_retrying,
    mark_crawl_run_running,
    mark_crawl_run_succeeded,
    recover_stale_crawl_runs,
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


async def _create_recovery_run(
    session,
    *,
    status: str,
    celery_task_id: str,
    created_at: datetime,
    source: str = "remoteok",
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    trigger_type: str = "api",
    retry_of_run_id: int | None = None,
    attempt_count: int = 0,
    received: int = 0,
    inserted: int = 0,
    updated: int = 0,
    duplicates: int = 0,
) -> CrawlRun:
    run = CrawlRun(
        source=source,
        status=status,
        celery_task_id=celery_task_id,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        trigger_type=trigger_type,
        retry_of_run_id=retry_of_run_id,
        attempt_count=attempt_count,
        received=received,
        inserted=inserted,
        updated=updated,
        duplicates=duplicates,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def _create_run_with_status(
    session,
    *,
    status: str,
    celery_task_id: str,
) -> CrawlRun:
    run = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id=celery_task_id,
    )

    if status == "queued":
        return run

    await mark_crawl_run_running(
        session,
        run_id=run.id,
    )

    if status == "running":
        await session.refresh(run)
        return run

    if status == "retrying":
        await mark_crawl_run_retrying(
            session,
            run_id=run.id,
            error_message="temporary error",
        )
    elif status == "succeeded":
        await mark_crawl_run_succeeded(
            session,
            run_id=run.id,
            received=10,
            inserted=7,
            updated=2,
            duplicates=1,
        )
    elif status == "failed":
        await mark_crawl_run_failed(
            session,
            run_id=run.id,
            error_message="final error",
        )
    else:
        raise ValueError(f"unknown status: {status}")

    await session.refresh(run)
    return run


def _crawl_run_snapshot(run: CrawlRun) -> dict[str, object]:
    return {
        "source": run.source,
        "status": run.status,
        "celery_task_id": run.celery_task_id,
        "retry_of_run_id": run.retry_of_run_id,
        "trigger_type": run.trigger_type,
        "attempt_count": run.attempt_count,
        "received": run.received,
        "inserted": run.inserted,
        "updated": run.updated,
        "duplicates": run.duplicates,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


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
        await session.execute(select(CrawlRun).where(CrawlRun.celery_task_id == "task-success-001"))
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

    stored = (await session.execute(select(CrawlRun).where(CrawlRun.id == run.id))).scalar_one()

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
@pytest.mark.parametrize("status", ACTIVE_CRAWL_RUN_STATUSES)
async def test_find_active_crawl_run_returns_active_statuses(session, status):
    run = await _create_list_run(
        session,
        source="remoteok",
        status=status,
        celery_task_id=f"task-active-{status}-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    found = await find_active_crawl_run(session, source="remoteok")

    assert found is not None
    assert found.id == run.id
    assert found.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_find_active_crawl_run_ignores_inactive_statuses(session, status):
    await _create_list_run(
        session,
        source="remoteok",
        status=status,
        celery_task_id=f"task-inactive-{status}-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    found = await find_active_crawl_run(session, source="remoteok")

    assert found is None


@pytest.mark.asyncio
async def test_find_active_crawl_run_matches_source_exactly(session):
    await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-active-source-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    found = await find_active_crawl_run(session, source="weworkremotely")

    assert found is None


@pytest.mark.asyncio
async def test_find_active_crawl_run_returns_none_without_active_records(session):
    found = await find_active_crawl_run(session, source="remoteok")

    assert found is None


@pytest.mark.asyncio
async def test_find_active_crawl_run_orders_by_created_at_then_id_desc(session):
    await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-active-order-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="remoteok",
        status="running",
        celery_task_id="task-active-order-002",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    newest = await _create_list_run(
        session,
        source="remoteok",
        status="retrying",
        celery_task_id="task-active-order-003",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    found = await find_active_crawl_run(session, source="remoteok")

    assert found is not None
    assert found.id == newest.id


@pytest.mark.asyncio
async def test_find_active_crawl_run_prefers_active_over_historical_runs(session):
    active = await _create_list_run(
        session,
        source="remoteok",
        status="running",
        celery_task_id="task-active-history-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="remoteok",
        status="failed",
        celery_task_id="task-active-history-002",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="remoteok",
        status="succeeded",
        celery_task_id="task-active-history-003",
        created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )

    found = await find_active_crawl_run(session, source="remoteok")

    assert found is not None
    assert found.id == active.id


@pytest.mark.asyncio
async def test_find_active_crawl_run_does_not_modify_records(session):
    run = await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-active-readonly-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    original = {
        "source": run.source,
        "status": run.status,
        "celery_task_id": run.celery_task_id,
        "retry_of_run_id": run.retry_of_run_id,
        "trigger_type": run.trigger_type,
        "attempt_count": run.attempt_count,
        "received": run.received,
        "inserted": run.inserted,
        "updated": run.updated,
        "duplicates": run.duplicates,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }

    found = await find_active_crawl_run(session, source="remoteok")

    assert found is not None
    assert found.id == run.id
    await session.refresh(run)
    assert {
        "source": run.source,
        "status": run.status,
        "celery_task_id": run.celery_task_id,
        "retry_of_run_id": run.retry_of_run_id,
        "trigger_type": run.trigger_type,
        "attempt_count": run.attempt_count,
        "received": run.received,
        "inserted": run.inserted,
        "updated": run.updated,
        "duplicates": run.duplicates,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    } == original


@pytest.mark.asyncio
async def test_create_crawl_run_if_inactive_creates_queued_record(session):
    run = await create_crawl_run_if_inactive(
        session,
        source="remoteok",
        celery_task_id="task-inactive-create-001",
    )

    assert run.id is not None
    assert run.source == "remoteok"
    assert run.status == "queued"
    assert run.celery_task_id == "task-inactive-create-001"
    assert run.trigger_type == "api"
    assert run.retry_of_run_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ACTIVE_CRAWL_RUN_STATUSES)
async def test_create_crawl_run_if_inactive_rejects_active_statuses(session, status):
    active = await _create_run_with_status(
        session,
        status=status,
        celery_task_id=f"task-inactive-block-{status}-001",
    )

    with pytest.raises(
        ActiveCrawlRunExistsError,
        match="active crawl run exists for source: remoteok",
    ) as exc_info:
        await create_crawl_run_if_inactive(
            session,
            source="remoteok",
            celery_task_id=f"task-inactive-block-child-{status}-001",
        )

    assert exc_info.value.source == "remoteok"
    assert exc_info.value.active_run_id == active.id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "succeeded"])
async def test_create_crawl_run_if_inactive_allows_inactive_statuses(session, status):
    await _create_run_with_status(
        session,
        status=status,
        celery_task_id=f"task-inactive-allow-{status}-001",
    )

    run = await create_crawl_run_if_inactive(
        session,
        source="remoteok",
        celery_task_id=f"task-inactive-allow-child-{status}-001",
    )

    assert run.status == "queued"
    assert run.source == "remoteok"


@pytest.mark.asyncio
async def test_create_crawl_run_if_inactive_ignores_different_source_activity(session):
    await create_crawl_run(
        session,
        source="weworkremotely",
        celery_task_id="task-inactive-different-source-001",
    )

    run = await create_crawl_run_if_inactive(
        session,
        source="remoteok",
        celery_task_id="task-inactive-different-source-002",
    )

    assert run.source == "remoteok"
    assert run.status == "queued"


@pytest.mark.asyncio
async def test_create_crawl_run_if_inactive_uses_latest_active_run_in_error(session):
    await _create_list_run(
        session,
        source="remoteok",
        status="queued",
        celery_task_id="task-inactive-latest-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _create_list_run(
        session,
        source="remoteok",
        status="running",
        celery_task_id="task-inactive-latest-002",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    newest = await _create_list_run(
        session,
        source="remoteok",
        status="retrying",
        celery_task_id="task-inactive-latest-003",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    with pytest.raises(ActiveCrawlRunExistsError) as exc_info:
        await create_crawl_run_if_inactive(
            session,
            source="remoteok",
            celery_task_id="task-inactive-latest-child-001",
        )

    assert exc_info.value.active_run_id == newest.id


@pytest.mark.asyncio
async def test_create_crawl_run_if_inactive_conflict_does_not_modify_database(session):
    active = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-inactive-readonly-001",
    )
    original = _crawl_run_snapshot(active)
    before = (await session.scalars(select(CrawlRun))).all()

    with pytest.raises(ActiveCrawlRunExistsError):
        await create_crawl_run_if_inactive(
            session,
            source="remoteok",
            celery_task_id="task-inactive-readonly-child-001",
        )

    after = (await session.scalars(select(CrawlRun))).all()
    await session.refresh(active)

    assert len(after) == len(before)
    assert _crawl_run_snapshot(active) == original


@pytest.mark.asyncio
async def test_create_crawl_run_if_inactive_passes_trigger_type_and_retry_parent(session):
    parent = await _create_run_with_status(
        session,
        status="failed",
        celery_task_id="task-inactive-params-parent-001",
    )

    child = await create_crawl_run_if_inactive(
        session,
        source="remoteok",
        celery_task_id="task-inactive-params-child-001",
        trigger_type="manual",
        retry_of_run_id=parent.id,
    )

    assert child.trigger_type == "manual"
    assert child.retry_of_run_id == parent.id


@pytest.mark.asyncio
async def test_recover_stale_crawl_runs_recovers_queued_by_created_at(session):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    recovered_at = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    run = await _create_recovery_run(
        session,
        status="queued",
        celery_task_id="task-recover-queued-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    recovered_ids = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=recovered_at,
        error_message="stale crawl run recovered after 20 minutes",
    )

    await session.refresh(run)
    assert recovered_ids == [run.id]
    assert run.status == "failed"


@pytest.mark.asyncio
async def test_recover_stale_crawl_runs_recovers_running_by_started_at(session):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    run = await _create_recovery_run(
        session,
        status="running",
        celery_task_id="task-recover-running-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
    )

    recovered_ids = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        error_message="stale crawl run recovered after 20 minutes",
    )

    await session.refresh(run)
    assert recovered_ids == [run.id]
    assert run.status == "failed"


@pytest.mark.asyncio
async def test_recover_stale_crawl_runs_recovers_retrying_by_started_at(session):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    run = await _create_recovery_run(
        session,
        status="retrying",
        celery_task_id="task-recover-retrying-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
    )

    recovered_ids = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        error_message="stale crawl run recovered after 20 minutes",
    )

    await session.refresh(run)
    assert recovered_ids == [run.id]
    assert run.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "retrying"])
async def test_recover_stale_crawl_runs_falls_back_to_created_at(session, status):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    run = await _create_recovery_run(
        session,
        status=status,
        celery_task_id=f"task-recover-fallback-{status}-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=None,
    )

    recovered_ids = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        error_message="stale crawl run recovered after 20 minutes",
    )

    await session.refresh(run)
    assert recovered_ids == [run.id]
    assert run.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "retrying"])
async def test_recover_stale_crawl_runs_prefers_started_at(session, status):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    run = await _create_recovery_run(
        session,
        status=status,
        celery_task_id=f"task-recover-started-at-{status}-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, 0, 21, tzinfo=timezone.utc),
    )

    recovered_ids = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        error_message="stale crawl run recovered after 20 minutes",
    )

    await session.refresh(run)
    assert recovered_ids == []
    assert run.status == status


@pytest.mark.asyncio
async def test_recover_stale_crawl_runs_uses_strict_stale_boundary(session):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    boundary = await _create_recovery_run(
        session,
        status="queued",
        celery_task_id="task-recover-boundary-001",
        created_at=stale_before,
    )
    fresh = await _create_recovery_run(
        session,
        status="queued",
        celery_task_id="task-recover-fresh-001",
        created_at=datetime(2026, 1, 1, 0, 21, tzinfo=timezone.utc),
    )

    recovered_ids = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        error_message="stale crawl run recovered after 20 minutes",
    )

    await session.refresh(boundary)
    await session.refresh(fresh)
    assert recovered_ids == []
    assert boundary.status == "queued"
    assert fresh.status == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_recover_stale_crawl_runs_does_not_recover_terminal_statuses(
    session,
    status,
):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    run = await _create_recovery_run(
        session,
        status=status,
        celery_task_id=f"task-recover-terminal-{status}-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
    )
    original = _crawl_run_snapshot(run)

    recovered_ids = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        error_message="stale crawl run recovered after 20 minutes",
    )

    await session.refresh(run)
    assert recovered_ids == []
    assert _crawl_run_snapshot(run) == original


@pytest.mark.asyncio
async def test_recover_stale_crawl_runs_returns_sorted_recovered_ids(session):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    stale_queued = await _create_recovery_run(
        session,
        status="queued",
        celery_task_id="task-recover-mixed-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _create_recovery_run(
        session,
        status="queued",
        celery_task_id="task-recover-mixed-002",
        created_at=datetime(2026, 1, 1, 0, 21, tzinfo=timezone.utc),
    )
    stale_running = await _create_recovery_run(
        session,
        status="running",
        celery_task_id="task-recover-mixed-003",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
    )
    await _create_recovery_run(
        session,
        status="succeeded",
        celery_task_id="task-recover-mixed-004",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    recovered_ids = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        error_message="stale crawl run recovered after 20 minutes",
    )

    assert recovered_ids == sorted([stale_queued.id, stale_running.id])


@pytest.mark.asyncio
async def test_recover_stale_crawl_runs_only_updates_recovery_fields(session):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    recovered_at = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    parent = await _create_recovery_run(
        session,
        status="failed",
        celery_task_id="task-recover-preserve-parent-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    run = await _create_recovery_run(
        session,
        status="running",
        celery_task_id="task-recover-preserve-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        trigger_type="manual",
        retry_of_run_id=parent.id,
        attempt_count=2,
        received=10,
        inserted=7,
        updated=2,
        duplicates=1,
    )
    original = _crawl_run_snapshot(run)

    recovered_ids = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=recovered_at,
        error_message="stale crawl run recovered after 20 minutes",
    )

    await session.refresh(run)
    assert recovered_ids == [run.id]
    assert run.status == "failed"
    assert run.error_message == "stale crawl run recovered after 20 minutes"
    assert _as_utc(run.finished_at) == recovered_at
    assert run.source == original["source"]
    assert run.celery_task_id == original["celery_task_id"]
    assert run.trigger_type == original["trigger_type"]
    assert run.retry_of_run_id == original["retry_of_run_id"]
    assert run.attempt_count == original["attempt_count"]
    assert run.started_at == original["started_at"]
    assert run.created_at == original["created_at"]
    assert run.received == original["received"]
    assert run.inserted == original["inserted"]
    assert run.updated == original["updated"]
    assert run.duplicates == original["duplicates"]


@pytest.mark.asyncio
async def test_recover_stale_crawl_runs_is_idempotent(session):
    stale_before = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    first_recovered_at = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    second_recovered_at = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
    run = await _create_recovery_run(
        session,
        status="queued",
        celery_task_id="task-recover-idempotent-001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    first = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=first_recovered_at,
        error_message="stale crawl run recovered after 20 minutes",
    )
    await session.refresh(run)
    first_finished_at = run.finished_at
    second = await recover_stale_crawl_runs(
        session,
        stale_before=stale_before,
        recovered_at=second_recovered_at,
        error_message="stale crawl run recovered after 20 minutes",
    )

    await session.refresh(run)
    assert first == [run.id]
    assert second == []
    assert _as_utc(first_finished_at) == first_recovered_at
    assert run.finished_at == first_finished_at


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
async def test_create_retry_crawl_run_creates_manual_child_from_failed_parent(session):
    parent = await _create_run_with_status(
        session,
        status="failed",
        celery_task_id="task-retry-parent-001",
    )
    original = {
        "id": parent.id,
        "source": parent.source,
        "status": parent.status,
        "celery_task_id": parent.celery_task_id,
        "trigger_type": parent.trigger_type,
        "retry_of_run_id": parent.retry_of_run_id,
        "attempt_count": parent.attempt_count,
        "error_message": parent.error_message,
        "finished_at": parent.finished_at,
    }

    retry = await create_retry_crawl_run(
        session,
        run_id=parent.id,
        celery_task_id="task-retry-child-001",
    )

    assert retry.id != parent.id
    assert retry.source == parent.source
    assert retry.status == "queued"
    assert retry.trigger_type == "manual"
    assert retry.retry_of_run_id == parent.id
    assert retry.celery_task_id == "task-retry-child-001"

    await session.refresh(parent)
    assert {
        "id": parent.id,
        "source": parent.source,
        "status": parent.status,
        "celery_task_id": parent.celery_task_id,
        "trigger_type": parent.trigger_type,
        "retry_of_run_id": parent.retry_of_run_id,
        "attempt_count": parent.attempt_count,
        "error_message": parent.error_message,
        "finished_at": parent.finished_at,
    } == original

    records = (await session.scalars(select(CrawlRun))).all()
    assert {run.id for run in records} == {parent.id, retry.id}


@pytest.mark.asyncio
async def test_create_retry_crawl_run_rejects_same_source_active_run(session):
    parent = await _create_run_with_status(
        session,
        status="failed",
        celery_task_id="task-retry-active-parent-001",
    )
    active = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-retry-active-current-001",
    )
    parent_original = _crawl_run_snapshot(parent)
    active_original = _crawl_run_snapshot(active)
    before = (await session.scalars(select(CrawlRun))).all()

    with pytest.raises(
        ActiveCrawlRunExistsError,
        match="active crawl run exists for source: remoteok",
    ) as exc_info:
        await create_retry_crawl_run(
            session,
            run_id=parent.id,
            celery_task_id="task-retry-active-child-001",
        )

    after = (await session.scalars(select(CrawlRun))).all()
    await session.refresh(parent)
    await session.refresh(active)

    assert exc_info.value.source == "remoteok"
    assert exc_info.value.active_run_id == active.id
    assert len(after) == len(before)
    assert _crawl_run_snapshot(parent) == parent_original
    assert _crawl_run_snapshot(active) == active_original


@pytest.mark.asyncio
async def test_create_retry_crawl_run_allows_different_source_active_run(session):
    parent = await _create_run_with_status(
        session,
        status="failed",
        celery_task_id="task-retry-different-active-parent-001",
    )
    await create_crawl_run(
        session,
        source="weworkremotely",
        celery_task_id="task-retry-different-active-current-001",
    )

    retry = await create_retry_crawl_run(
        session,
        run_id=parent.id,
        celery_task_id="task-retry-different-active-child-001",
    )

    assert retry.source == parent.source
    assert retry.status == "queued"
    assert retry.trigger_type == "manual"
    assert retry.retry_of_run_id == parent.id


@pytest.mark.asyncio
async def test_create_retry_crawl_run_checks_retryable_status_before_activity(session):
    parent = await create_crawl_run(
        session,
        source="remoteok",
        celery_task_id="task-retry-priority-parent-001",
    )

    with pytest.raises(
        CrawlRunNotRetryableError,
        match=f"crawl run is not retryable: {parent.id}",
    ) as exc_info:
        await create_retry_crawl_run(
            session,
            run_id=parent.id,
            celery_task_id="task-retry-priority-child-001",
        )

    assert exc_info.value.run_id == parent.id
    assert exc_info.value.status == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "retrying", "succeeded"])
async def test_create_retry_crawl_run_rejects_non_failed_statuses(session, status):
    run = await _create_run_with_status(
        session,
        status=status,
        celery_task_id=f"task-retry-not-allowed-{status}",
    )

    with pytest.raises(
        CrawlRunNotRetryableError,
        match=f"crawl run is not retryable: {run.id}",
    ) as exc_info:
        await create_retry_crawl_run(
            session,
            run_id=run.id,
            celery_task_id=f"task-retry-not-allowed-child-{status}",
        )

    assert exc_info.value.run_id == run.id
    assert exc_info.value.status == status


@pytest.mark.asyncio
async def test_create_retry_crawl_run_rejects_missing_parent(session):
    with pytest.raises(
        CrawlRunNotFoundError,
        match="crawl run not found: 999999",
    ):
        await create_retry_crawl_run(
            session,
            run_id=999999,
            celery_task_id="task-retry-missing-parent-001",
        )


@pytest.mark.asyncio
async def test_create_retry_crawl_run_uses_direct_parent_for_retry_chain(session):
    original = await _create_run_with_status(
        session,
        status="failed",
        celery_task_id="task-retry-chain-original-001",
    )
    first_retry = await create_retry_crawl_run(
        session,
        run_id=original.id,
        celery_task_id="task-retry-chain-first-001",
    )
    await mark_crawl_run_running(
        session,
        run_id=first_retry.id,
    )
    await mark_crawl_run_failed(
        session,
        run_id=first_retry.id,
        error_message="manual retry failed",
    )

    second_retry = await create_retry_crawl_run(
        session,
        run_id=first_retry.id,
        celery_task_id="task-retry-chain-second-001",
    )

    assert first_retry.retry_of_run_id == original.id
    assert second_retry.retry_of_run_id == first_retry.id
    assert second_retry.retry_of_run_id != original.id

    stored = (await session.scalars(select(CrawlRun))).all()
    assert {run.id for run in stored} == {
        original.id,
        first_retry.id,
        second_retry.id,
    }


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
