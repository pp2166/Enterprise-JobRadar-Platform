from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers import registry
from app.database import get_session
from app.schemas import CrawlRequest, CrawlResponse, CrawlRunListResponse, CrawlRunOut
from app.services.crawl_runs import (
    CrawlRunNotFoundError,
    CrawlRunNotRetryableError,
    create_crawl_run,
    create_retry_crawl_run,
    get_crawl_run,
    list_crawl_runs,
    mark_crawl_run_failed,
)
from app.workers.tasks import crawl_source

router = APIRouter()


@router.get("/sources")
async def list_sources() -> dict:
    return {"sources": registry.names()}


@router.get("/crawl-runs", response_model=CrawlRunListResponse)
async def list_admin_crawl_runs(
    source: str | None = None,
    status: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    session: AsyncSession = Depends(get_session),
) -> CrawlRunListResponse:
    result = await list_crawl_runs(
        session,
        source=source,
        status=status,
        page=page,
        page_size=page_size,
    )

    return CrawlRunListResponse(
        total=result.total,
        page=page,
        page_size=page_size,
        runs=[CrawlRunOut.model_validate(run) for run in result.records],
    )


@router.get("/crawl-runs/{run_id}", response_model=CrawlRunOut)
async def get_admin_crawl_run(
    run_id: int,
    session: AsyncSession = Depends(get_session),
) -> CrawlRunOut:
    try:
        run = await get_crawl_run(
            session,
            run_id=run_id,
        )
    except CrawlRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return CrawlRunOut.model_validate(run)


@router.post("/crawl-runs/{run_id}/retry", response_model=CrawlRunOut)
async def retry_admin_crawl_run(
    run_id: int,
    session: AsyncSession = Depends(get_session),
) -> CrawlRunOut:
    task_id = str(uuid4())
    try:
        retry_run = await create_retry_crawl_run(
            session,
            run_id=run_id,
            celery_task_id=task_id,
        )
    except CrawlRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CrawlRunNotRetryableError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CRAWL_RUN_NOT_RETRYABLE",
                "message": str(exc),
                "run_id": exc.run_id,
                "status": exc.status,
            },
        ) from exc

    try:
        crawl_source.apply_async(
            args=[retry_run.source],
            task_id=task_id,
        )
    except Exception as exc:
        await mark_crawl_run_failed(
            session,
            run_id=retry_run.id,
            error_message=f"dispatch failed: {exc}",
        )
        raise HTTPException(
            status_code=503,
            detail=f"failed to dispatch retry crawl run: {retry_run.id}",
        ) from exc

    return CrawlRunOut.model_validate(retry_run)


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
            trigger_type="api",
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
