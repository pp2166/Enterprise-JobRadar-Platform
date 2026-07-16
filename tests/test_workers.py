"""Worker / Celery task coverage.

We don't boot Celery: we exercise the underlying async coroutine (`_run_crawler`)
and assert the task module's retry / dispatch glue. Redis is never contacted.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawlers import registry
from app.crawlers.base import BaseCrawler
from app.models import CrawlRun
from app.services.crawl_runs import create_crawl_run
from app.services.normalize import NormalizedJob


async def _create_worker_crawl_run(
    session: AsyncSession,
    *,
    source: str,
    status: str,
    celery_task_id: str,
) -> CrawlRun:
    run = CrawlRun(
        source=source,
        status=status,
        celery_task_id=celery_task_id,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


class _StubCrawler(BaseCrawler):
    name = "stub"

    def __init__(self, jobs: list[NormalizedJob] | None = None, raise_exc: Exception | None = None):
        super().__init__()
        self._jobs = jobs or []
        self._raise_exc = raise_exc

    async def fetch(self) -> AsyncIterator[NormalizedJob]:
        if self._raise_exc is not None:
            raise self._raise_exc
        for j in self._jobs:
            yield j


@pytest.fixture
def stub_registry(monkeypatch, make_job):
    """Replace the shared crawler registry with a deterministic stub."""
    jobs = [make_job(source="stub", source_id=str(i)) for i in range(3)]
    stub = _StubCrawler(jobs=jobs)
    monkeypatch.setitem(registry._items, "stub", stub)
    yield stub
    registry._items.pop("stub", None)


@pytest.fixture
async def patched_tasks(monkeypatch, sqlite_engine):
    """Wire tasks._run_crawler to use the sqlite fixture instead of asyncpg."""
    from app.workers import tasks

    # Skip Postgres trigger DDL in the worker.
    monkeypatch.setattr(tasks, "_ensure_schema", AsyncMock(return_value=None))
    # Point the worker's session factory at SQLite.
    sqlite_session_factory = async_sessionmaker(
        sqlite_engine, expire_on_commit=False, class_=AsyncSession,
    )
    monkeypatch.setattr(tasks, "AsyncSessionLocal", sqlite_session_factory)
    return tasks


@pytest.mark.asyncio
class TestRunCrawler:
    async def test_happy_path_returns_ingest_stats(self, patched_tasks, stub_registry):
        result = await patched_tasks._run_crawler("stub")
        assert result["source"] == "stub"
        assert result["received"] == 3
        assert result["inserted"] == 3
        assert result["duplicates"] == 0

    async def test_unknown_source_raises_keyerror(self, patched_tasks):
        with pytest.raises(KeyError):
            await patched_tasks._run_crawler("nope")

    async def test_empty_crawler_yields_zero_stats(self, patched_tasks, monkeypatch):
        empty = _StubCrawler(jobs=[])
        monkeypatch.setitem(registry._items, "empty", empty)
        try:
            result = await patched_tasks._run_crawler("empty")
            assert result == {
                "source": "empty", "received": 0, "inserted": 0,
                "updated": 0, "duplicates": 0,
            }
        finally:
            registry._items.pop("empty", None)

    async def test_crawler_exception_propagates(self, patched_tasks, monkeypatch):
        boom = _StubCrawler(raise_exc=RuntimeError("network dead"))
        monkeypatch.setitem(registry._items, "boom", boom)
        try:
            with pytest.raises(RuntimeError, match="network dead"):
                await patched_tasks._run_crawler("boom")
        finally:
            registry._items.pop("boom", None)


@pytest.mark.asyncio
class TestCrawlSourceRunTracking:
    async def test_success_marks_crawl_run_succeeded(
        self,
        patched_tasks,
        stub_registry,
        session: AsyncSession,
    ):
        run = await create_crawl_run(
            session,
            source="stub",
            celery_task_id="task-worker-success-001",
        )

        result = await patched_tasks._run_crawler_attempt(
            "stub",
            task_id="task-worker-success-001",
            retries=0,
            max_retries=3,
        )

        assert result == {
            "source": "stub",
            "received": 3,
            "inserted": 3,
            "updated": 0,
            "duplicates": 0,
        }

        await session.refresh(run)
        assert run.status == "succeeded"
        assert run.attempt_count == 1
        assert run.started_at is not None
        assert run.finished_at is not None
        assert run.received == 3
        assert run.inserted == 3
        assert run.updated == 0
        assert run.duplicates == 0

    async def test_retryable_failure_marks_crawl_run_retrying(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
    ):
        run = await create_crawl_run(
            session,
            source="boom",
            celery_task_id="task-worker-retry-001",
        )
        boom = _StubCrawler(raise_exc=RuntimeError("temporary crawler error"))
        monkeypatch.setitem(registry._items, "boom", boom)
        try:
            result = await patched_tasks._run_crawler_attempt(
                "boom",
                task_id="task-worker-retry-001",
                retries=0,
                max_retries=3,
            )
        finally:
            registry._items.pop("boom", None)

        assert isinstance(result, patched_tasks._RetryCrawl)
        assert "temporary crawler error" in str(result.exc)

        await session.refresh(run)
        assert run.status == "retrying"
        assert run.attempt_count == 1
        assert run.error_message is not None
        assert "temporary crawler error" in run.error_message
        assert run.finished_at is None

    async def test_final_failure_marks_crawl_run_failed(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
    ):
        run = await create_crawl_run(
            session,
            source="boom",
            celery_task_id="task-worker-failed-001",
        )
        boom = _StubCrawler(raise_exc=RuntimeError("permanent crawler error"))
        monkeypatch.setitem(registry._items, "boom", boom)
        try:
            with pytest.raises(RuntimeError, match="permanent crawler error"):
                await patched_tasks._run_crawler_attempt(
                    "boom",
                    task_id="task-worker-failed-001",
                    retries=3,
                    max_retries=3,
                )
        finally:
            registry._items.pop("boom", None)

        await session.refresh(run)
        assert run.status == "failed"
        assert run.attempt_count == 1
        assert run.error_message is not None
        assert "permanent crawler error" in run.error_message
        assert run.finished_at is not None

    async def test_second_attempt_preserves_started_at_and_succeeds(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
        make_job,
    ):
        run = await create_crawl_run(
            session,
            source="flaky",
            celery_task_id="task-worker-flaky-001",
        )
        failing = _StubCrawler(raise_exc=RuntimeError("temporary crawler error"))
        monkeypatch.setitem(registry._items, "flaky", failing)

        first = await patched_tasks._run_crawler_attempt(
            "flaky",
            task_id="task-worker-flaky-001",
            retries=0,
            max_retries=3,
        )
        assert isinstance(first, patched_tasks._RetryCrawl)

        await session.refresh(run)
        original_started_at = run.started_at
        assert original_started_at is not None

        succeeding = _StubCrawler(
            jobs=[make_job(source="flaky", source_id="second-attempt")]
        )
        monkeypatch.setitem(registry._items, "flaky", succeeding)
        try:
            result = await patched_tasks._run_crawler_attempt(
                "flaky",
                task_id="task-worker-flaky-001",
                retries=1,
                max_retries=3,
            )
        finally:
            registry._items.pop("flaky", None)

        assert result["inserted"] == 1

        await session.refresh(run)
        assert run.status == "succeeded"
        assert run.attempt_count == 2
        assert run.started_at == original_started_at
        assert run.error_message is None
        assert run.finished_at is not None

    async def test_missing_crawl_run_task_auto_creates_record(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
        make_job,
    ):
        legacy = _StubCrawler(jobs=[make_job(source="legacy", source_id="1")])
        monkeypatch.setitem(registry._items, "legacy", legacy)
        try:
            result = await patched_tasks._run_crawler_attempt(
                "legacy",
                task_id="task-worker-missing-001",
                retries=0,
                max_retries=3,
            )
        finally:
            registry._items.pop("legacy", None)

        assert result == {
            "source": "legacy",
            "received": 1,
            "inserted": 1,
            "updated": 0,
            "duplicates": 0,
        }

        run = (
            await session.scalars(
                select(CrawlRun).where(
                    CrawlRun.celery_task_id == "task-worker-missing-001"
                )
            )
        ).one()
        assert run.source == "legacy"
        assert run.status == "succeeded"
        assert run.celery_task_id == "task-worker-missing-001"
        assert run.trigger_type == "direct"
        assert run.attempt_count == 1
        assert run.started_at is not None
        assert run.finished_at is not None
        assert run.received == 1
        assert run.inserted == 1
        assert run.updated == 0
        assert run.duplicates == 0

    async def test_existing_task_id_reuses_crawl_run_without_duplicate(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
        make_job,
    ):
        run = await create_crawl_run(
            session,
            source="manual",
            celery_task_id="task-worker-existing-001",
        )
        manual = _StubCrawler(jobs=[make_job(source="manual", source_id="1")])
        monkeypatch.setitem(registry._items, "manual", manual)
        try:
            result = await patched_tasks._run_crawler_attempt(
                "manual",
                task_id="task-worker-existing-001",
                retries=0,
                max_retries=3,
            )
        finally:
            registry._items.pop("manual", None)

        assert result["source"] == "manual"
        assert result["inserted"] == 1

        runs = (
            await session.scalars(
                select(CrawlRun).where(
                    CrawlRun.celery_task_id == "task-worker-existing-001"
                )
            )
        ).all()
        assert len(runs) == 1
        assert runs[0].id == run.id
        await session.refresh(run)
        assert run.status == "succeeded"
        assert run.attempt_count == 1

    @pytest.mark.parametrize("retries", [0, 3])
    async def test_soft_time_limit_marks_crawl_run_failed_without_retry(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
        retries: int,
    ):
        async def fake_crawl_and_ingest(name, session):
            raise SoftTimeLimitExceeded()

        run = await create_crawl_run(
            session,
            source="timeout",
            celery_task_id=f"task-worker-soft-timeout-{retries}",
        )
        monkeypatch.setattr(
            patched_tasks,
            "_crawl_and_ingest",
            fake_crawl_and_ingest,
        )

        with pytest.raises(SoftTimeLimitExceeded):
            await patched_tasks._run_crawler_attempt(
                "timeout",
                task_id=f"task-worker-soft-timeout-{retries}",
                retries=retries,
                max_retries=3,
            )

        await session.refresh(run)
        assert run.status == "failed"
        assert run.error_message == (
            "crawl soft time limit exceeded after 120 seconds"
        )
        assert run.finished_at is not None
        assert run.attempt_count == 1

        runs = (
            await session.scalars(
                select(CrawlRun).where(
                    CrawlRun.celery_task_id == f"task-worker-soft-timeout-{retries}"
                )
            )
        ).all()
        assert len(runs) == 1
        assert runs[0].id == run.id


@pytest.mark.asyncio
class TestCrawlSourceDispatch:
    async def test_dispatcher_success_creates_scheduled_run_and_dispatches_source(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
    ):
        calls: list[tuple[list[str], str]] = []
        monkeypatch.setattr(patched_tasks, "uuid4", lambda: "task-dispatch-success-001")
        monkeypatch.setattr(
            patched_tasks.crawl_source,
            "apply_async",
            lambda args, task_id: calls.append((args, task_id)),
        )

        result = await patched_tasks._dispatch_crawl_source(source="remoteok")

        assert result == {
            "source": "remoteok",
            "status": "dispatched",
            "run_id": result["run_id"],
            "celery_task_id": "task-dispatch-success-001",
        }
        assert calls == [(["remoteok"], "task-dispatch-success-001")]

        run = await session.get(CrawlRun, result["run_id"])
        assert run is not None
        assert run.source == "remoteok"
        assert run.status == "queued"
        assert run.trigger_type == "scheduled"
        assert run.celery_task_id == "task-dispatch-success-001"

    async def test_dispatcher_task_id_is_not_written_to_crawl_run(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
    ):
        monkeypatch.setattr(patched_tasks, "uuid4", lambda: "task-dispatch-real-001")
        monkeypatch.setattr(
            patched_tasks.crawl_source,
            "apply_async",
            lambda args, task_id: None,
        )

        patched_tasks.dispatch_crawl_source.push_request(id="task-dispatcher-wrapper-001")
        try:
            result = await patched_tasks._dispatch_crawl_source(source="remoteok")
        finally:
            patched_tasks.dispatch_crawl_source.pop_request()

        run = await session.get(CrawlRun, result["run_id"])
        assert run is not None
        assert run.celery_task_id == "task-dispatch-real-001"
        assert run.celery_task_id != "task-dispatcher-wrapper-001"

    @pytest.mark.parametrize("status", ["queued", "running", "retrying"])
    async def test_dispatcher_skips_same_source_active_run(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
        status: str,
    ):
        calls: list[tuple[list[str], str]] = []
        active = await _create_worker_crawl_run(
            session,
            source="remoteok",
            status=status,
            celery_task_id=f"task-dispatch-active-{status}-001",
        )
        before = (await session.scalars(select(CrawlRun))).all()
        monkeypatch.setattr(
            patched_tasks.crawl_source,
            "apply_async",
            lambda args, task_id: calls.append((args, task_id)),
        )

        result = await patched_tasks._dispatch_crawl_source(source="remoteok")

        after = (await session.scalars(select(CrawlRun))).all()
        assert result == {
            "source": "remoteok",
            "status": "skipped",
            "reason": "active crawl run exists",
            "active_run_id": active.id,
        }
        assert calls == []
        assert len(after) == len(before)

    @pytest.mark.parametrize("status", ["failed", "succeeded"])
    async def test_dispatcher_allows_inactive_history(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
        status: str,
    ):
        calls: list[tuple[list[str], str]] = []
        await _create_worker_crawl_run(
            session,
            source="remoteok",
            status=status,
            celery_task_id=f"task-dispatch-inactive-{status}-001",
        )
        monkeypatch.setattr(patched_tasks, "uuid4", lambda: f"task-dispatch-{status}-002")
        monkeypatch.setattr(
            patched_tasks.crawl_source,
            "apply_async",
            lambda args, task_id: calls.append((args, task_id)),
        )

        result = await patched_tasks._dispatch_crawl_source(source="remoteok")

        assert result["status"] == "dispatched"
        assert calls == [(["remoteok"], f"task-dispatch-{status}-002")]
        runs = (await session.scalars(select(CrawlRun))).all()
        assert len(runs) == 2

    async def test_dispatcher_allows_different_source_active_run(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
    ):
        calls: list[tuple[list[str], str]] = []
        await _create_worker_crawl_run(
            session,
            source="weworkremotely",
            status="queued",
            celery_task_id="task-dispatch-different-source-001",
        )
        monkeypatch.setattr(patched_tasks, "uuid4", lambda: "task-dispatch-different-source-002")
        monkeypatch.setattr(
            patched_tasks.crawl_source,
            "apply_async",
            lambda args, task_id: calls.append((args, task_id)),
        )

        result = await patched_tasks._dispatch_crawl_source(source="remoteok")

        assert result["status"] == "dispatched"
        assert calls == [(["remoteok"], "task-dispatch-different-source-002")]

    async def test_dispatcher_marks_run_failed_when_apply_async_fails(
        self,
        patched_tasks,
        monkeypatch,
        session: AsyncSession,
    ):
        def fake_apply_async(args, task_id):
            raise RuntimeError("broker unavailable")

        monkeypatch.setattr(patched_tasks, "uuid4", lambda: "task-dispatch-failed-001")
        monkeypatch.setattr(
            patched_tasks.crawl_source,
            "apply_async",
            fake_apply_async,
        )

        result = await patched_tasks._dispatch_crawl_source(source="remoteok")

        assert result["source"] == "remoteok"
        assert result["status"] == "dispatch_failed"

        run = await session.get(CrawlRun, result["run_id"])
        assert run is not None
        assert run.status == "failed"
        assert run.error_message is not None
        assert "dispatch failed: broker unavailable" in run.error_message
        assert run.finished_at is not None

    async def test_worker_reuses_dispatcher_created_crawl_run(
        self,
        patched_tasks,
        stub_registry,
        monkeypatch,
        session: AsyncSession,
    ):
        monkeypatch.setattr(patched_tasks, "uuid4", lambda: "task-dispatch-reuse-001")
        monkeypatch.setattr(
            patched_tasks.crawl_source,
            "apply_async",
            lambda args, task_id: None,
        )
        dispatched = await patched_tasks._dispatch_crawl_source(source="stub")

        result = await patched_tasks._run_crawler_attempt(
            "stub",
            task_id=dispatched["celery_task_id"],
            retries=0,
            max_retries=3,
        )

        assert result["source"] == "stub"
        assert result["inserted"] == 3

        runs = (
            await session.scalars(
                select(CrawlRun).where(
                    CrawlRun.celery_task_id == "task-dispatch-reuse-001"
                )
            )
        ).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "succeeded"
        assert run.trigger_type == "scheduled"
        assert run.attempt_count == 1


class TestCeleryTaskWrappers:
    def test_schema_failure_still_retries(self, monkeypatch):
        from app.workers import tasks

        async def fake_ensure_schema():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(tasks, "_ensure_schema", fake_ensure_schema)
        mock_retry = MagicMock(side_effect=RuntimeError("retry-called"))
        monkeypatch.setattr(tasks.crawl_source, "retry", mock_retry)

        tasks.crawl_source.push_request(id="task-schema-retry-001", retries=0)
        try:
            with pytest.raises(RuntimeError, match="retry-called"):
                tasks.crawl_source.run("remoteok")
        finally:
            tasks.crawl_source.pop_request()

        retry_exc = mock_retry.call_args.kwargs["exc"]
        assert isinstance(retry_exc, RuntimeError)
        assert str(retry_exc) == "database unavailable"

    def test_final_schema_failure_does_not_retry(self, monkeypatch):
        from app.workers import tasks

        async def fake_ensure_schema():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(tasks, "_ensure_schema", fake_ensure_schema)
        mock_retry = MagicMock()
        monkeypatch.setattr(tasks.crawl_source, "retry", mock_retry)

        tasks.crawl_source.push_request(id="task-schema-final-001", retries=3)
        try:
            with pytest.raises(RuntimeError, match="database unavailable"):
                tasks.crawl_source.run("remoteok")
        finally:
            tasks.crawl_source.pop_request()

        mock_retry.assert_not_called()

    def test_crawl_source_retries_when_attempt_requests_retry(self, monkeypatch):
        """The bound task must delegate to self.retry for retryable failures."""
        from app.workers import tasks

        exc = RuntimeError("boom")

        def fake_run(coro):
            coro.close()
            return tasks._RetryCrawl(exc=exc)

        monkeypatch.setattr(tasks.asyncio, "run", fake_run)
        mock_retry = MagicMock(side_effect=RuntimeError("retry-called"))
        monkeypatch.setattr(tasks.crawl_source, "retry", mock_retry)

        with pytest.raises(RuntimeError, match="retry-called"):
            tasks.crawl_source.run("remoteok")

        mock_retry.assert_called_once_with(exc=exc)

    def test_crawl_source_reraises_final_failure_without_retry(self, monkeypatch):
        from app.workers import tasks

        exc = RuntimeError("final failure")

        def fake_run(coro):
            coro.close()
            raise exc

        monkeypatch.setattr(tasks.asyncio, "run", fake_run)
        mock_retry = MagicMock()
        monkeypatch.setattr(tasks.crawl_source, "retry", mock_retry)

        tasks.crawl_source.push_request(id="task-final-failure-001", retries=3)
        try:
            with pytest.raises(RuntimeError, match="final failure"):
                tasks.crawl_source.run("remoteok")
        finally:
            tasks.crawl_source.pop_request()

        mock_retry.assert_not_called()

    def test_crawl_source_reraises_soft_time_limit_without_retry(self, monkeypatch):
        from app.workers import tasks

        def fake_run(coro):
            coro.close()
            raise SoftTimeLimitExceeded()

        monkeypatch.setattr(tasks.asyncio, "run", fake_run)
        mock_retry = MagicMock()
        monkeypatch.setattr(tasks.crawl_source, "retry", mock_retry)

        tasks.crawl_source.push_request(id="task-soft-timeout-wrapper-001", retries=0)
        try:
            with pytest.raises(SoftTimeLimitExceeded):
                tasks.crawl_source.run("remoteok")
        finally:
            tasks.crawl_source.pop_request()

        mock_retry.assert_not_called()

    def test_crawl_all_dispatches_every_registered_source(self, monkeypatch):
        from app.workers import tasks

        calls: list[str] = []
        fake_dispatcher = MagicMock()
        fake_dispatcher.delay = lambda name: calls.append(name)
        fake_crawl_source = MagicMock()
        fake_crawl_source.delay = MagicMock(
            side_effect=AssertionError("crawl_source.delay should not be called")
        )
        monkeypatch.setattr(tasks, "dispatch_crawl_source", fake_dispatcher)
        monkeypatch.setattr(tasks, "crawl_source", fake_crawl_source)

        # crawl_all is not bound (bind=False); .run is the plain function.
        dispatched = tasks.crawl_all.run()
        assert dispatched == registry.names()
        assert calls == registry.names()
        fake_crawl_source.delay.assert_not_called()


class TestCeleryConfig:
    def test_crawl_source_time_limit_configuration(self):
        from app.workers.tasks import (
            CRAWL_SOFT_TIME_LIMIT_SECONDS,
            CRAWL_TIME_LIMIT_SECONDS,
            crawl_source,
        )

        assert crawl_source.soft_time_limit == CRAWL_SOFT_TIME_LIMIT_SECONDS
        assert crawl_source.time_limit == CRAWL_TIME_LIMIT_SECONDS
        assert crawl_source.soft_time_limit == 120
        assert crawl_source.time_limit == 150
        assert crawl_source.max_retries == 3
        assert crawl_source.default_retry_delay == 60

    def test_only_crawl_source_has_time_limits(self):
        from app.workers.tasks import crawl_all, dispatch_crawl_source

        assert dispatch_crawl_source.soft_time_limit is None
        assert dispatch_crawl_source.time_limit is None
        assert crawl_all.soft_time_limit is None
        assert crawl_all.time_limit is None

    def test_beat_schedule_includes_both_sources(self):
        from app.workers.celery_app import celery_app

        entries = celery_app.conf.beat_schedule
        assert "crawl-remoteok" in entries
        assert "crawl-weworkremotely" in entries
        assert entries["crawl-remoteok"]["task"] == (
            "app.workers.tasks.dispatch_crawl_source"
        )
        assert entries["crawl-remoteok"]["args"] == ("remoteok",)
        assert entries["crawl-remoteok"]["schedule"]._orig_minute == "*/30"
        assert entries["crawl-weworkremotely"]["task"] == (
            "app.workers.tasks.dispatch_crawl_source"
        )
        assert entries["crawl-weworkremotely"]["args"] == ("weworkremotely",)
        assert entries["crawl-weworkremotely"]["schedule"]._orig_minute == "5-59/30"

    def test_core_reliability_flags(self):
        from app.workers.celery_app import celery_app

        assert celery_app.conf.task_acks_late is True
        assert celery_app.conf.task_reject_on_worker_lost is True
        assert celery_app.conf.worker_prefetch_multiplier == 1
        assert celery_app.conf.timezone == "UTC"
