from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers import registry
from app.database import get_session
from app.schemas import CrawlRequest, CrawlResponse, CrawlRunOut
from app.services.crawl_runs import create_crawl_run, mark_crawl_run_failed
from app.workers.tasks import crawl_source

router = APIRouter()


@router.get("/sources")
async def list_sources() -> dict:
    return {"sources": registry.names()}


@router.post("/crawl", response_model=CrawlResponse)
async def trigger_crawl(
    req: CrawlRequest,
    session: AsyncSession = Depends(get_session),
) -> CrawlResponse:
    if req.source:
        if req.source not in registry.names():
            raise HTTPException(status_code=400, detail=f"unknown source: {req.source}")
        sources = [req.source]
    else:
        sources = registry.names()

    dispatched = []
    runs = []
    for source in sources:
        task_id = str(uuid4())
        run = await create_crawl_run(
            session,
            source=source,
            celery_task_id=task_id,
        )

        try:
            crawl_source.apply_async(
                args=[source],
                task_id=task_id,
            )
        except Exception as exc:
            await mark_crawl_run_failed(
                session,
                run_id=run.id,
                error_message=f"dispatch failed: {exc}",
            )
            raise HTTPException(
                status_code=503,
                detail=f"failed to dispatch source: {source}",
            ) from exc

        dispatched.append(source)
        runs.append(CrawlRunOut.model_validate(run))

    return CrawlResponse(dispatched=dispatched, runs=runs)
