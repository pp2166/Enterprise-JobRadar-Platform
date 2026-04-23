"""Worker / Celery task coverage.

We don't boot Celery: we exercise the underlying async coroutine (`_run_crawler`)
and assert the task module's retry / dispatch glue. Redis is never contacted.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawlers import registry
from app.crawlers.base import BaseCrawler
from app.services.normalize import NormalizedJob


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


class TestCeleryTaskWrappers:
    def test_crawl_source_retries_on_exception(self, monkeypatch):
        """The bound task must delegate to self.retry on any failure."""
        from app.workers import tasks

        # Replace _run_crawler with a synchronous function that raises so no
        # un-awaited coroutine is left dangling.
        def _boom(_source):
            raise RuntimeError("boom")

        monkeypatch.setattr(tasks, "_run_crawler", _boom)
        # Bypass asyncio.run since _boom is sync now.
        monkeypatch.setattr(tasks.asyncio, "run", lambda coro: coro(None))

        mock_retry = MagicMock(side_effect=RuntimeError("retry-called"))
        monkeypatch.setattr(tasks.crawl_source, "retry", mock_retry)

        with pytest.raises(RuntimeError, match="retry-called"):
            tasks.crawl_source.run("remoteok")

        mock_retry.assert_called_once()

    def test_crawl_all_dispatches_every_registered_source(self, monkeypatch):
        from app.workers import tasks

        calls: list[str] = []
        fake_task = MagicMock()
        fake_task.delay = lambda name: calls.append(name)
        monkeypatch.setattr(tasks, "crawl_source", fake_task)

        # crawl_all is not bound (bind=False); .run is the plain function.
        dispatched = tasks.crawl_all.run()
        assert set(dispatched) == set(registry.names())
        assert set(calls) == set(registry.names())


class TestCeleryConfig:
    def test_beat_schedule_includes_both_sources(self):
        from app.workers.celery_app import celery_app

        entries = celery_app.conf.beat_schedule
        assert "crawl-remoteok" in entries
        assert "crawl-weworkremotely" in entries
        for e in entries.values():
            assert e["task"] == "app.workers.tasks.crawl_source"

    def test_core_reliability_flags(self):
        from app.workers.celery_app import celery_app

        assert celery_app.conf.task_acks_late is True
        assert celery_app.conf.task_reject_on_worker_lost is True
        assert celery_app.conf.worker_prefetch_multiplier == 1
        assert celery_app.conf.timezone == "UTC"
