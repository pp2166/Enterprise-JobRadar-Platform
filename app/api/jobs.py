from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Job
from app.schemas import JobOut, SearchResult
from app.services.search import SearchFilters, search_jobs

router = APIRouter()


@router.get("/search", response_model=SearchResult)
async def search(
    q: str | None = Query(None, description="Free-text query (supports quotes/AND/OR/-)"),
    location: str | None = None,
    remote: bool | None = None,
    experience: str | None = Query(None, pattern="^(junior|mid|senior)$"),
    company: str | None = None,
    source: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> SearchResult:
    filters = SearchFilters(
        q=q,
        location=location,
        remote=remote,
        experience=experience,
        company=company,
        source=source,
        page=page,
        page_size=page_size,
    )
    total, jobs = await search_jobs(session, filters)
    return SearchResult(
        total=total,
        page=page,
        page_size=page_size,
        results=[JobOut.model_validate(j) for j in jobs],
    )


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)) -> JobOut:
    row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobOut.model_validate(row)
