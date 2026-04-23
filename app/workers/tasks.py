from __future__ import annotations

import asyncio
import logging

from app.crawlers import registry
from app.database import AsyncSessionLocal
from app.schema import init_schema
from app.services.ingest import ingest_jobs
from app.services.normalize import NormalizedJob
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

_schema_ready = False


async def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    await init_schema()
    _schema_ready = True


async def _run_crawler(name: str) -> dict:
    await _ensure_schema()
    crawler = registry.get(name)
    jobs: list[NormalizedJob] = []
    async for nj in crawler.fetch():
        jobs.append(nj)

    log.info("crawler %s produced %d jobs", name, len(jobs))

    async with AsyncSessionLocal() as session:
        stats = await ingest_jobs(session, jobs)

    return {
        "source": name,
        "received": stats.received,
        "inserted": stats.inserted,
        "updated": stats.updated,
        "duplicates": stats.duplicates,
    }


@celery_app.task(name="app.workers.tasks.crawl_source", bind=True, max_retries=3, default_retry_delay=60)
def crawl_source(self, source: str) -> dict:
    try:
        return asyncio.run(_run_crawler(source))
    except Exception as exc:
        log.exception("crawl %s failed", source)
        raise self.retry(exc=exc)


@celery_app.task(name="app.workers.tasks.crawl_all")
def crawl_all() -> list[str]:
    dispatched = []
    for name in registry.names():
        crawl_source.delay(name)
        dispatched.append(name)
    return dispatched
