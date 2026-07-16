from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import uuid4

from billiard.exceptions import SoftTimeLimitExceeded

from app.crawlers import registry
from app.database import AsyncSessionLocal
from app.schema import init_schema
from app.services.crawl_runs import (
    ActiveCrawlRunExistsError,
    create_crawl_run,
    create_crawl_run_if_inactive,
    find_crawl_run_by_task_id,
    mark_crawl_run_failed,
    mark_crawl_run_retrying,
    mark_crawl_run_running,
    mark_crawl_run_succeeded,
)
from app.services.ingest import IngestStats, ingest_jobs
from app.services.normalize import NormalizedJob
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

CRAWL_SOFT_TIME_LIMIT_SECONDS = 120
CRAWL_TIME_LIMIT_SECONDS = 150

_schema_ready = False


@dataclass(frozen=True)
class _RetryCrawl:
    exc: Exception


async def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    await init_schema()
    _schema_ready = True


def _stats_result(name: str, stats: IngestStats) -> dict:
    return {
        "source": name,
        "received": stats.received,
        "inserted": stats.inserted,
        "updated": stats.updated,
        "duplicates": stats.duplicates,
    }


async def _crawl_and_ingest(name: str, session) -> dict:
    crawler = registry.get(name)
    jobs: list[NormalizedJob] = []
    async for nj in crawler.fetch():
        jobs.append(nj)

    log.info("crawler %s produced %d jobs", name, len(jobs))

    stats = await ingest_jobs(session, jobs)
    return _stats_result(name, stats)


async def _run_crawler(name: str) -> dict:
    await _ensure_schema()
    async with AsyncSessionLocal() as session:
        return await _crawl_and_ingest(name, session)


async def _dispatch_crawl_source(
    *,
    source: str,
    trigger_type: str = "scheduled",
) -> dict:
    await _ensure_schema()

    task_id = str(uuid4())
    async with AsyncSessionLocal() as session:
        try:
            run = await create_crawl_run_if_inactive(
                session,
                source=source,
                celery_task_id=task_id,
                trigger_type=trigger_type,
            )
        except ActiveCrawlRunExistsError as exc:
            log.info(
                "skipping crawl dispatch for %s; active run %s exists",
                source,
                exc.active_run_id,
            )
            return {
                "source": source,
                "status": "skipped",
                "reason": "active crawl run exists",
                "active_run_id": exc.active_run_id,
            }

        try:
            crawl_source.apply_async(
                args=[source],
                task_id=task_id,
            )
        except Exception as exc:
            log.exception("failed to dispatch crawl %s", source)
            await mark_crawl_run_failed(
                session,
                run_id=run.id,
                error_message=f"dispatch failed: {exc}",
            )
            return {
                "source": source,
                "status": "dispatch_failed",
                "run_id": run.id,
            }

    return {
        "source": source,
        "status": "dispatched",
        "run_id": run.id,
        "celery_task_id": task_id,
    }


async def _run_crawler_attempt(
    name: str,
    *,
    task_id: str | None,
    retries: int,
    max_retries: int,
) -> dict | _RetryCrawl:
    await _ensure_schema()

    async with AsyncSessionLocal() as session:
        run = None
        run_id = None
        if task_id is not None:
            run = await find_crawl_run_by_task_id(
                session,
                celery_task_id=task_id,
            )
            if run is None:
                run = await create_crawl_run(
                    session,
                    source=name,
                    celery_task_id=task_id,
                    trigger_type="direct",
                )

        if run is not None:
            run_id = run.id
            run = await mark_crawl_run_running(
                session,
                run_id=run_id,
            )

        try:
            result = await _crawl_and_ingest(name, session)
        except SoftTimeLimitExceeded:
            log.exception("crawl %s exceeded soft time limit", name)
            await session.rollback()

            if run_id is not None:
                error_message = (
                    "crawl soft time limit exceeded after "
                    f"{CRAWL_SOFT_TIME_LIMIT_SECONDS} seconds"
                )
                await mark_crawl_run_failed(
                    session,
                    run_id=run_id,
                    error_message=error_message,
                )
            raise
        except Exception as exc:
            log.exception("crawl %s failed", name)
            await session.rollback()

            if retries < max_retries:
                if run_id is not None:
                    await mark_crawl_run_retrying(
                        session,
                        run_id=run_id,
                        error_message=str(exc),
                    )
                return _RetryCrawl(exc=exc)

            if run_id is not None:
                await mark_crawl_run_failed(
                    session,
                    run_id=run_id,
                    error_message=str(exc),
                )
            raise

        if run_id is not None:
            await mark_crawl_run_succeeded(
                session,
                run_id=run_id,
                received=result["received"],
                inserted=result["inserted"],
                updated=result["updated"],
                duplicates=result["duplicates"],
            )

        return result


@celery_app.task(
    name="app.workers.tasks.crawl_source",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=CRAWL_SOFT_TIME_LIMIT_SECONDS,
    time_limit=CRAWL_TIME_LIMIT_SECONDS,
)
def crawl_source(self, source: str) -> dict:
    try:
        result = asyncio.run(
            _run_crawler_attempt(
                source,
                task_id=self.request.id,
                retries=self.request.retries,
                max_retries=self.max_retries,
            )
        )
    except SoftTimeLimitExceeded:
        log.exception("crawl %s soft time limit exceeded", source)
        raise
    except Exception as exc:
        log.exception("crawl %s attempt failed", source)

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)

        raise

    if isinstance(result, _RetryCrawl):
        raise self.retry(exc=result.exc)
    return result


@celery_app.task(name="app.workers.tasks.dispatch_crawl_source")
def dispatch_crawl_source(
    source: str,
    trigger_type: str = "scheduled",
) -> dict:
    return asyncio.run(
        _dispatch_crawl_source(
            source=source,
            trigger_type=trigger_type,
        )
    )


@celery_app.task(name="app.workers.tasks.crawl_all")
def crawl_all() -> list[str]:
    dispatched = []
    for name in registry.names():
        dispatch_crawl_source.delay(name)
        dispatched.append(name)
    return dispatched
